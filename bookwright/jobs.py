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
from dataclasses import dataclass, field
from typing import Any

from . import config, prompts
from .llm import LLMError, Provider, Usage
from .schema import Plan, PlanError, parse_plan

log = logging.getLogger("bookwright")


@dataclass
class BookSpec:
    topic: str
    audience: str
    tone: str
    length_id: str

    @property
    def length(self) -> config.LengthPreset:
        return config.LENGTHS[self.length_id]

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "audience": self.audience,
            "tone": self.tone,
            "length": self.length_id,
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
                }
                for index, state in sorted(self.chapters.items())
            ],
            "stats": self.stats(),
            "error": self.error,
            "started_at": self.started_at,
        }

    def cancel(self) -> None:
        self.cancel_event.set()
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
            topic=spec.topic, audience=spec.audience, tone=spec.tone, length=length
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
                    topic=spec.topic, audience=spec.audience, tone=spec.tone, length=length
                ),
                str(exc),
            )
            _merge(job, usage)
            plan = parse_plan(
                raw, topic=spec.topic, audience=spec.audience, tone=spec.tone,
                words_per_chapter=length.words_per_chapter, max_chapters=config.MAX_CHAPTERS,
            )

        job.plan = plan
        for chapter in plan.chapters:
            job.chapters[chapter.index] = {
                "title": chapter.title,
                "text": "",
                "status": "pending",
                "summary": "",
            }
        job.emit({"type": "plan", "plan": plan.to_dict(), "stats": job.stats()})

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
            bible = prompts.build_bible(plan, summaries, terms)

            results = await asyncio.gather(
                *(
                    _write_chapter(job, provider, plan, chapter, part_of[chapter.index], bible)
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
    job: Job, provider: Provider, plan: Plan, chapter, part_title: str, bible: str
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
        plan=plan, chapter=chapter, part_title=part_title, bible=bible
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
    state["status"] = "done"
    job.emit({
        "type": "chapter_written",
        "index": chapter.index,
        "words": len(text.split()),
        "stats": job.stats(),
    })


def estimate(spec: BookSpec) -> dict[str, Any]:
    """Rough pre-flight estimate, deliberately conservative."""
    length = spec.length
    chapters = length.chapters
    # Per chapter: prompt grows with the bible window; completion ~1.35 tokens/word.
    per_chapter_in = 620 + 110 * min(chapters, config.BIBLE_SUMMARY_WINDOW)
    per_chapter_out = int(length.words_per_chapter * 1.35)
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
