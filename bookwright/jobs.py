"""Job state, event fan-out and the generation pipeline.

One Job == one book. Jobs live in memory, stream events to any number of
subscribers, and can be cancelled. The pipeline is deliberately wave-based:
chapters are drafted CONCURRENCY at a time, then their summaries are folded
into the continuity bible before the next wave starts. That trades a little
throughput for coherence — the trade the original single-title prompts never
had to make because they had no continuity at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from . import config, prompts
from . import style as stylemod
from .llm import LLMError, Provider, Usage
from .schema import Plan, PlanError, parse_plan

log = logging.getLogger("bookwright")


@dataclass
class BookSpec:
    topic: str
    audience: str
    tone: str
    length_id: str
    directives: list[str] = field(default_factory=lambda: list(config.DEFAULT_DIRECTIVES))
    extra_instructions: str = ""
    chapters_override: int | None = None
    words_override: int | None = None
    stylist: bool = config.STYLIST_DEFAULT
    stylist_strength: str = config.DEFAULT_STYLIST_STRENGTH
    banned_phrases: list[str] = field(default_factory=list)
    notes: str = ""
    voice_sample: str = ""
    pov: str = config.DEFAULT_POV
    review_outline: bool = config.DEFAULT_REVIEW_OUTLINE

    @property
    def length(self) -> config.LengthPreset:
        """The preset, with any custom chapter/word overrides applied."""
        preset = config.LENGTHS[self.length_id]
        chapters = self.chapters_override or preset.chapters
        words = self.words_override or preset.words_per_chapter
        chapters = max(config.CHAPTERS_RANGE[0], min(chapters, config.MAX_CHAPTERS))
        words = max(config.WORDS_PER_CHAPTER_RANGE[0], min(words, config.WORDS_PER_CHAPTER_RANGE[1]))
        if (chapters, words) == (preset.chapters, preset.words_per_chapter):
            return preset
        parts = max(1, min(round(chapters / 4), config.MAX_PARTS))
        return replace(
            preset,
            chapters=chapters,
            words_per_chapter=words,
            parts=parts,
            blurb=f"{chapters} chapters · ~{chapters * words:,} words",
            id="custom",
        )

    @property
    def style(self) -> str:
        from .prompts import style_block

        return style_block(self.clean_directives(), self.extra_instructions, self.clean_banned())

    def clean_directives(self) -> list[str]:
        """Drop unknown ids and de-duplicate, preserving order.

        A rule that contradicts the chosen grammatical person is dropped here
        rather than half-applied: asking for second person *and* "no you" is a
        contradiction the model cannot satisfy, and the editor would then fight
        the writer for the whole book.
        """
        blocked = set(config.POV_CONFLICTS.get(self.pov, ()))
        seen: list[str] = []
        for key in self.directives:
            if key in config.ANTI_PATTERNS and key not in seen and key not in blocked:
                seen.append(key)
        # A non-empty author ban list is itself an active rule, regardless of
        # which profile was chosen. It must reach both the writer and scanner.
        if self.clean_banned() and "banned_phrases" not in seen:
            seen.append("banned_phrases")
        return seen

    @property
    def pov_text(self) -> str:
        return config.POVS.get(self.pov, ("", ""))[1]

    def clean_notes(self) -> str:
        return (self.notes or "").strip()[: config.MAX_NOTES_CHARS]

    def clean_voice_sample(self) -> str:
        return (self.voice_sample or "").strip()[: config.MAX_VOICE_SAMPLE_CHARS]

    def clean_banned(self) -> list[str]:
        """Trim, drop empties and duplicates, cap length and count."""
        seen: list[str] = []
        for phrase in self.banned_phrases:
            cleaned = " ".join(str(phrase).split())[: config.BANNED_PHRASE_MAX_CHARS]
            if cleaned and cleaned.lower() not in {p.lower() for p in seen}:
                seen.append(cleaned)
        return seen[: config.MAX_BANNED_PHRASES]

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "audience": self.audience,
            "tone": self.tone,
            "length": self.length_id,
            "chapters": self.length.chapters,
            "words_per_chapter": self.length.words_per_chapter,
            "directives": self.clean_directives(),
            "extra_instructions": self.extra_instructions,
            "stylist": self.stylist,
            "stylist_strength": self.stylist_strength,
            "banned_phrases": self.clean_banned(),
            "notes": self.clean_notes(),
            "voice_sample": self.clean_voice_sample(),
            "pov": self.pov if self.pov in config.POVS else config.DEFAULT_POV,
            "review_outline": self.review_outline,
        }


@dataclass
class Job:
    id: str
    spec: BookSpec
    provider_name: str
    status: str = "queued"  # queued|planning|writing|done|partial|error|cancelled
    plan: Plan | None = None
    chapters: dict[int, dict] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    events: list[dict] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Set when the author approves (or edits) the outline; the pipeline waits.
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    # -- events ------------------------------------------------------------- #
    def emit(self, event: dict) -> None:
        event = {**event, "t": round(time.time() - self.started_at, 2)}
        if event.get("type") != "delta":
            self.events.append(event)
            if len(self.events) > 400:
                del self.events[:-400]
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow client: tell it to resync from the snapshot rather than
                # silently losing chapter text.
                while not queue.empty():
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait({"type": "resync", "t": event["t"]})

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=4000)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    # -- state -------------------------------------------------------------- #
    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def terminal(self) -> bool:
        return self.status in {"done", "partial", "error", "cancelled"}

    @property
    def busy(self) -> bool:
        """True while the job is holding a model slot.

        A book parked at outline review is idle: it is not calling the API, so
        it must not count against the concurrency cap.
        """
        return not self.terminal and self.status != "awaiting_outline"

    def stats(self) -> dict:
        elapsed = (self.finished_at or time.time()) - self.started_at
        done = sum(1 for c in self.chapters.values() if c.get("status") == "done")
        failed = [c["title"] for c in self.chapters.values() if c.get("status") == "failed"]
        return {
            **self.usage.to_dict(),
            "elapsed": round(elapsed, 1),
            "chapters_done": done,
            "chapters_total": len(self.plan.chapters) if self.plan else self.spec.length.chapters,
            "failed_chapters": failed,
        }

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "mode": self.provider_name,
            "status": self.status,
            "spec": self.spec.to_dict(),
            "plan": self.plan.to_dict() if self.plan else None,
            "chapters": [
                {
                    "index": index,
                    "title": state["title"],
                    "text": state["text"],
                    "status": state["status"],
                    "summary": state.get("summary", ""),
                    "words": len(state["text"].split()),
                    "original": state.get("original"),
                    "style_before": state.get("style_before"),
                    "style_after": state.get("style_after"),
                    "diff": stylemod.paragraph_diff(state["original"], state["text"])
                    if state.get("original")
                    else None,
                    "reverted": bool(state.get("reverted")),
                }
                for index, state in sorted(self.chapters.items())
            ],
            "stats": self.stats(),
            "error": self.error,
            "started_at": self.started_at,
        }

    def revert_chapter(self, index: int) -> bool:
        """Put the pre-editorial draft back for one chapter."""
        state = self.chapters.get(index)
        if not state or not state.get("original"):
            return False
        state["text"] = state["original"]
        state["original"] = None
        state["diff"] = None
        state["style_after"] = state["style_before"]
        state["reverted"] = True
        self.emit({
            "type": "chapter_reverted",
            "index": index,
            "text": state["text"],
            "words": len(state["text"].split()),
            "stats": self.stats(),
        })
        return True

    def style_report(self) -> dict:
        """Residue per rule plus readability, across every finished chapter."""
        text = "\n\n".join(
            state["text"] for _, state in sorted(self.chapters.items())
            if state.get("status") == "done" and state["text"]
        )
        if not self.plan:
            return {"available": False}
        rules = self.spec.clean_directives()
        banned = self.spec.clean_banned()
        report = stylemod.report(text, rules, banned)
        edited = [s for s in self.chapters.values() if s.get("style_before") is not None]
        report.update({
            "available": True,
            "rules_active": len(rules),
            "chapters_edited": sum(1 for s in edited if s.get("style_before") != s.get("style_after")),
            "flags_before": sum(s.get("style_before") or 0 for s in edited),
            "flags_after": sum(s.get("style_after") or 0 for s in edited),
        })
        return report

    def seed_chapters(self) -> None:
        """(Re)build the per-chapter state from the current plan."""
        self.chapters = {}
        for chapter in self.plan.chapters if self.plan else []:
            self.chapters[chapter.index] = {
                "title": chapter.title,
                "text": "",
                "status": "pending",
                "summary": "",
                "original": None,
                "style_before": None,
                "style_after": None,
            }

    def apply_outline(self, plan: Plan) -> None:
        """Swap in an author-edited plan and let the writer continue."""
        if self.status != "awaiting_outline":
            raise ValueError("this book is not waiting for an outline review")
        self.plan = plan
        self.seed_chapters()
        # Claim the review slot before signalling, so two fast approve requests
        # cannot race and replace one another's outline.
        self.status = "writing"
        self.emit({"type": "plan", "plan": plan.to_dict(), "stats": self.stats()})
        self.resume_event.set()

    def cancel(self) -> None:
        self.cancel_event.set()
        # Unblock a job parked at outline review so the task can unwind.
        self.resume_event.set()
        if self.task and not self.task.done():
            self.task.cancel()


class JobStore:
    def __init__(self, capacity: int = 24):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self.capacity = capacity

    def create(self, spec: BookSpec, provider: Provider) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], spec=spec, provider_name=provider.name)
        job.chapters = {}
        self._jobs[job.id] = job
        self._order.append(job.id)
        while len(self._order) > self.capacity:
            old = self._order.pop(0)
            existing = self._jobs.get(old)
            if existing and existing.terminal:
                self._jobs.pop(old, None)
            else:
                self._order.append(old)
                break
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return list(self._jobs.values())


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def _merge(job: Job, usage: Usage | None) -> None:
    if usage:
        job.usage.merge(usage)


async def run_book(job: Job, spec: BookSpec, provider: Provider) -> None:
    started = time.perf_counter()
    try:
        length = spec.length
        job.status = "planning"
        job.emit({
            "type": "status",
            "stage": "planning",
            "message": f"Architect is designing a {length.chapters}-chapter plan",
        })

        raw, usage = await provider.plan(
            topic=spec.topic, audience=spec.audience, tone=spec.tone, length=length,
            notes=spec.clean_notes(),
        )
        _merge(job, usage)
        job.emit({"type": "stats", "stats": job.stats()})

        try:
            plan = parse_plan(
                raw, topic=spec.topic, audience=spec.audience, tone=spec.tone,
                words_per_chapter=length.words_per_chapter, max_chapters=config.MAX_CHAPTERS,
            )
        except PlanError as exc:
            log.info("plan rejected (%s); requesting one repair", exc)
            job.emit({"type": "status", "stage": "planning",
                      "message": f"Plan rejected ({exc}) — asking the architect to repair it"})
            repair = getattr(provider, "repair_plan", None)
            if repair is None:
                raise
            raw, usage = await repair(
                prompts.plan_messages(
                    topic=spec.topic, audience=spec.audience, tone=spec.tone, length=length,
                    notes=spec.clean_notes(),
                ),
                str(exc),
            )
            _merge(job, usage)
            plan = parse_plan(
                raw, topic=spec.topic, audience=spec.audience, tone=spec.tone,
                words_per_chapter=length.words_per_chapter, max_chapters=config.MAX_CHAPTERS,
            )

        job.plan = plan
        job.emit({"type": "plan", "plan": plan.to_dict(), "stats": job.stats()})

        if spec.review_outline:
            job.status = "awaiting_outline"
            job.emit({
                "type": "outline_review",
                "plan": plan.to_dict(),
                "message": "Outline ready — edit it, then approve to start writing",
                "stats": job.stats(),
            })
            log.info("job %s waiting for outline review", job.id)
            await job.resume_event.wait()
            job.resume_event.clear()
            if job.cancelled:
                raise asyncio.CancelledError
            plan = job.plan or plan
            job.status = "writing"

        job.seed_chapters()

        if job.cancelled:
            raise asyncio.CancelledError

        part_of = {c.index: part.title for part in plan.parts for c in part.chapters}
        summaries: list[tuple[str, str]] = []
        terms: list[str] = []
        chapters = plan.chapters
        failed: list[str] = []

        for start in range(0, len(chapters), config.CONCURRENCY):
            if job.cancelled:
                raise asyncio.CancelledError
            wave = chapters[start : start + config.CONCURRENCY]
            job.status = "writing"
            job.emit({
                "type": "status",
                "stage": "writing",
                "message": f"Drafting chapter{'s' if len(wave) > 1 else ''} "
                           + ", ".join(str(c.index) for c in wave),
            })
            bible = prompts.build_bible(
                plan, summaries, terms, style=spec.style, notes=spec.clean_notes()
            )

            results = await asyncio.gather(
                *(
                    _write_chapter(
                        job, provider, plan, chapter, part_of[chapter.index], bible,
                        spec.style, spec,
                    )
                    for chapter in wave
                ),
                return_exceptions=True,
            )
            for chapter, result in zip(wave, results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, Exception):
                    failed.append(chapter.title)
                    state = job.chapters[chapter.index]
                    state["status"] = "failed"
                    message = str(result) if isinstance(result, LLMError) else f"{type(result).__name__}: {result}"
                    job.emit({"type": "chapter_error", "index": chapter.index, "message": message})
                    log.warning("chapter %s failed: %s", chapter.index, message)

            # Continuity pass: fold the wave into the bible in reading order.
            for chapter in wave:
                state = job.chapters[chapter.index]
                if state["status"] != "done" or not state["text"].strip():
                    continue
                try:
                    data, usage = await provider.summarize(chapter=chapter, text=state["text"])
                    _merge(job, usage)
                    summary = str(data.get("summary") or "").strip()
                    new_terms = [str(t).strip() for t in (data.get("terms") or []) if str(t).strip()]
                except Exception as exc:  # noqa: BLE001 - continuity is best-effort, never fatal
                    log.warning("summary failed for chapter %s: %s", chapter.index, exc)
                    summary, new_terms = "", []
                state["summary"] = summary
                if summary:
                    summaries.append((chapter.title, summary))
                for term in new_terms:
                    if term.lower() not in {t.lower() for t in terms}:
                        terms.append(term)
                job.emit({
                    "type": "chapter_done",
                    "index": chapter.index,
                    "summary": summary,
                    "words": len(state["text"].split()),
                    "stats": job.stats(),
                })

        job.finished_at = time.time()
        if job.cancelled:
            raise asyncio.CancelledError
        job.status = "partial" if failed else "done"
        job.emit({
            "type": "done",
            "status": job.status,
            "stats": job.stats(),
            "failed": failed,
            "elapsed": round(time.perf_counter() - started, 1),
            "style_report": job.style_report(),
        })

    except asyncio.CancelledError:
        job.finished_at = time.time()
        job.status = "cancelled"
        job.emit({"type": "cancelled", "stats": job.stats()})
        for state in job.chapters.values():
            if state["status"] in {"pending", "writing"}:
                state["status"] = "cancelled"
        raise
    except Exception as exc:
        job.finished_at = time.time()
        job.status = "error"
        job.error = str(exc)
        log.exception("generation failed for job %s", job.id)
        job.emit({"type": "error", "message": str(exc)})
    finally:
        closer = getattr(provider, "aclose", None)
        if closer:
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001 - shutdown must never raise
                log.debug("provider close failed: %s", exc)


async def _write_chapter(
    job: Job,
    provider: Provider,
    plan: Plan,
    chapter,
    part_title: str,
    bible: str,
    style: str = "",
    spec: BookSpec | None = None,
) -> None:
    state = job.chapters[chapter.index]
    state["status"] = "writing"
    job.emit({
        "type": "chapter_start",
        "index": chapter.index,
        "title": chapter.title,
        "part": part_title,
        "words_target": chapter.words,
    })

    buffer: list[str] = []
    async for event in provider.write_chapter(
        plan=plan, chapter=chapter, part_title=part_title, bible=bible, style=style,
        pov=spec.pov_text if spec else "",
        voice_sample=spec.clean_voice_sample() if spec else "",
    ):
        if job.cancelled:
            raise asyncio.CancelledError
        if event["type"] == "delta":
            buffer.append(event["text"])
            job.emit({"type": "delta", "index": chapter.index, "text": event["text"]})
        elif event["type"] == "usage":
            _merge(job, event.get("usage"))

    text = "".join(buffer).strip()
    if not text:
        raise LLMError("model returned an empty chapter")
    state["text"] = text
    job.emit({
        "type": "chapter_written",
        "index": chapter.index,
        "words": len(text.split()),
        "stats": job.stats(),
    })

    if spec is not None and spec.stylist:
        await _deai_chapter(job, provider, chapter, spec)

    state["status"] = "done"
    job.emit({
        "type": "chapter_drafted",
        "index": chapter.index,
        "words": len(state["text"].split()),
        "stats": job.stats(),
    })


def estimate(spec: BookSpec) -> dict[str, Any]:
    """Rough pre-flight estimate, deliberately conservative."""
    length = spec.length
    chapters = length.chapters
    # Per chapter: prompt grows with the bible window; completion ~1.35 tokens/word.
    per_chapter_in = 620 + 110 * min(chapters, config.BIBLE_SUMMARY_WINDOW)
    per_chapter_in += len(spec.clean_notes()) // 4  # notes ride with every chapter
    per_chapter_in += len(spec.clean_voice_sample()) // 4  # voice reference rides with each chapter
    per_chapter_out = int(length.words_per_chapter * 1.4)
    total_in = 1400 + chapters * per_chapter_in
    total_out = 900 + chapters * per_chapter_out
    return {
        "chapters": chapters,
        "words": length.total_words,
        "prompt_tokens": total_in,
        "completion_tokens": total_out,
        "total_tokens": total_in + total_out,
        "review_note": "estimate only; actual usage is reported per chapter",
    }


# --------------------------------------------------------------------------- #
# De-AI editorial pass
# --------------------------------------------------------------------------- #
def chunk_text(text: str, max_chars: int = config.STYLIST_CHUNK_CHARS) -> list[str]:
    """Split on blank lines into chunks small enough to rewrite safely."""
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in paragraphs:
        if size and size + len(paragraph) > max_chars:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(paragraph)
        size += len(paragraph) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks[: config.STYLIST_MAX_CHUNKS]


async def _deai_chapter(job: Job, provider: Provider, chapter, spec: BookSpec) -> None:
    """Scan the chapter, rewrite flagged passages, keep the result only if it improved.

    Three guards make this safe to run unattended:
      * nothing happens when the scan is clean, or below the strength threshold;
      * a rewrite outside the length tolerance is discarded;
      * a rewrite that does not reduce the flag count is discarded.
    The original is always kept so the UI can show a diff.
    """
    state = job.chapters[chapter.index]
    original = state["text"]
    rules = spec.clean_directives()
    if not rules:
        return

    banned = spec.clean_banned()
    before = stylemod.scan(original, rules, banned)
    before_total = stylemod.total(before)
    threshold = config.STYLIST_STRENGTHS.get(spec.stylist_strength, 3)
    if before_total < threshold:
        state["style_before"] = before_total
        state["style_after"] = before_total
        return

    job.emit({
        "type": "stylist_start",
        "index": chapter.index,
        "flags": before_total,
        "rules": [f.rule for f in before],
    })

    rules_text = prompts.style_block(rules)
    rewritten: list[str] = []
    usage = Usage()
    budget = config.STYLIST_CHUNK_BUDGET.get(spec.stylist_strength, 4)
    for chunk in chunk_text(original):
        if budget and budget <= 0:
            break
        flagged = stylemod.scan(chunk, rules, banned)
        if stylemod.total(flagged) < 1:
            rewritten.append(chunk)
            continue
        try:
            text, chunk_usage = await provider.rewrite_chunk(
                chunk=chunk,
                findings=stylemod.bullet_report(flagged),
                rules=rules_text,
                chapter=chapter,
            )
        except Exception as exc:  # noqa: BLE001 - an editorial pass must never sink a book
            log.warning("stylist failed on chapter %s: %s", chapter.index, exc)
            rewritten.append(chunk)
            continue
        usage.merge(chunk_usage)
        rewritten.append(text.strip())
        if budget:
            budget -= 1
    _merge(job, usage)

    candidate = "\n\n".join(part for part in rewritten if part).strip()
    if not candidate:
        return

    low, high = config.STYLIST_LENGTH_TOLERANCE
    ratio = stylemod.length_ratio(original, candidate)
    if not low <= ratio <= high:
        job.emit({
            "type": "stylist_rejected",
            "index": chapter.index,
            "reason": f"length changed by {round((ratio - 1) * 100)}%",
        })
        return

    after = stylemod.scan(candidate, rules, banned)
    after_total = stylemod.total(after)
    if after_total >= before_total:
        job.emit({
            "type": "stylist_rejected",
            "index": chapter.index,
            "reason": "rewrite did not reduce the flagged patterns",
        })
        return

    state["original"] = original
    state["text"] = candidate
    state["style_before"] = before_total
    state["style_after"] = after_total
    job.emit({
        "type": "stylist_done",
        "index": chapter.index,
        "text": candidate,
        "before": before_total,
        "after": after_total,
        "words": len(candidate.split()),
        "diff": stylemod.paragraph_diff(original, candidate),
        "stats": job.stats(),
    })
