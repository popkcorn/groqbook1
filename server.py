"""FastAPI server: JSON API, SSE progress stream, and file exports.

Run:  uvicorn server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Literal

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from bookwright import config, prompts
from bookwright.exporters import ExportOptions, export
from bookwright.jobs import BookSpec, JobStore, estimate, run_book
from bookwright.llm import GroqProvider, LLMError
from bookwright.mock import MockProvider
from bookwright.schema import PlanError, parse_plan

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("bookwright.server")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Bookwright", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

store = JobStore(capacity=24)
MAX_RUNNING_JOBS = 3
EXPORT_FORMATS = {"md", "html", "epub", "pdf"}


class BookRequest(BaseModel):
    topic: str = Field(min_length=config.MIN_TOPIC_CHARS, max_length=config.MAX_TOPIC_CHARS)
    audience: str = Field(default=config.AUDIENCES[0], max_length=60)
    tone: str = Field(default=config.DEFAULT_TONE, max_length=32)
    length: str = Field(default=config.DEFAULT_LENGTH, max_length=32)
    chapters: int | None = Field(default=None, ge=config.CHAPTERS_RANGE[0], le=config.MAX_CHAPTERS)
    words_per_chapter: int | None = Field(
        default=None,
        ge=config.WORDS_PER_CHAPTER_RANGE[0],
        le=config.WORDS_PER_CHAPTER_RANGE[1],
    )
    directives: list[str] = Field(default_factory=lambda: list(config.DEFAULT_DIRECTIVES))
    extra_instructions: str = Field(default="", max_length=config.MAX_EXTRA_INSTRUCTIONS)
    stylist: bool = config.STYLIST_DEFAULT
    stylist_strength: str = config.DEFAULT_STYLIST_STRENGTH
    banned_phrases: list[str] = Field(
        default_factory=list, max_length=config.MAX_BANNED_PHRASES
    )
    profile: str | None = None
    notes: str = Field(default="", max_length=config.MAX_NOTES_CHARS)
    voice_sample: str = Field(default="", max_length=config.MAX_VOICE_SAMPLE_CHARS)
    pov: str = config.DEFAULT_POV
    review_outline: bool = config.DEFAULT_REVIEW_OUTLINE
    unit_label: Literal["chapter", "section", "essay", "entry"] = "chapter"
    part_label: Literal["part", "book", "volume"] = "part"
    subheading_style: Literal["none", "few", "standard", "detailed"] = "standard"
    group_count: int | None = Field(default=None, ge=1, le=config.MAX_PARTS)
    architect_model: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    writer_model: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    editor_model: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

    @field_validator("topic")
    @classmethod
    def _clean_topic(cls, value: str) -> str:
        value = re.sub(r"\s+", " ", value).strip()
        if len(value) < config.MIN_TOPIC_CHARS:
            raise ValueError(f"give me at least {config.MIN_TOPIC_CHARS} characters to work with")
        return value

    @field_validator("length")
    @classmethod
    def _known_length(cls, value: str) -> str:
        if value not in config.LENGTHS:
            raise ValueError(f"unknown length preset: {value}")
        return value

    @field_validator("directives")
    @classmethod
    def _known_directives(cls, value: list[str]) -> list[str]:
        unknown = [key for key in value if key not in config.ANTI_PATTERNS]
        if unknown:
            raise ValueError(f"unknown style directive(s): {', '.join(unknown)}")
        return list(dict.fromkeys(value))

    @field_validator("banned_phrases")
    @classmethod
    def _clean_banned(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for phrase in value:
            phrase = " ".join(str(phrase).split())[: config.BANNED_PHRASE_MAX_CHARS]
            if phrase and phrase.lower() not in {p.lower() for p in cleaned}:
                cleaned.append(phrase)
        return cleaned[: config.MAX_BANNED_PHRASES]

    @field_validator("profile")
    @classmethod
    def _known_profile(cls, value: str | None) -> str | None:
        if value is not None and value not in config.EDITORIAL_PROFILES:
            raise ValueError(f"unknown profile: {value}")
        return value

    @field_validator("notes")
    @classmethod
    def _clean_notes(cls, value: str) -> str:
        return value.strip()[: config.MAX_NOTES_CHARS]

    @field_validator("voice_sample")
    @classmethod
    def _clean_sample(cls, value: str) -> str:
        return value.strip()[: config.MAX_VOICE_SAMPLE_CHARS]

    @field_validator("pov")
    @classmethod
    def _known_pov(cls, value: str) -> str:
        if value not in config.POVS:
            raise ValueError(f"pov must be one of {sorted(config.POVS)}")
        return value

    @field_validator("stylist_strength")
    @classmethod
    def _known_strength(cls, value: str) -> str:
        if value not in config.STYLIST_STRENGTHS:
            raise ValueError(f"stylist_strength must be one of {sorted(config.STYLIST_STRENGTHS)}")
        return value

    @field_validator("extra_instructions")
    @classmethod
    def _clean_extra(cls, value: str) -> str:
        return " ".join(value.split())[: config.MAX_EXTRA_INSTRUCTIONS]

    @field_validator("tone")
    @classmethod
    def _known_tone(cls, value: str) -> str:
        if value not in config.TONES:
            raise ValueError(f"unknown tone: {value}")
        return value


class ChapterRegenerationRequest(BaseModel):
    instruction: str = Field(min_length=5, max_length=config.MAX_REGENERATION_INSTRUCTION)

    @field_validator("instruction")
    @classmethod
    def _clean_instruction(cls, value: str) -> str:
        value = " ".join(value.split()).strip()
        if len(value) < 5:
            raise ValueError("give the writer a specific revision command")
        return value[: config.MAX_REGENERATION_INSTRUCTION]



def expand_profile(payload: BookRequest) -> tuple[list[str], str, str]:
    """Apply an editorial profile unless the caller set a field explicitly.

    Without this, a profile sent by an API client would be accepted and then
    ignored, because the browser was doing the expansion.
    """
    directives = payload.directives
    strength = payload.stylist_strength
    extra = payload.extra_instructions
    if not payload.profile:
        return directives, strength, extra

    profile = config.EDITORIAL_PROFILES[payload.profile]
    explicit = payload.model_fields_set
    if "directives" not in explicit:
        directives = list(profile["directives"])
    if "stylist_strength" not in explicit:
        strength = profile["strength"]
    if "extra_instructions" not in explicit and not extra.strip():
        extra = profile["extra"]
    return directives, strength, extra


def _client_key(request: Request, body_key: str | None = None) -> str:
    header = request.headers.get("x-groq-key", "").strip()
    return header or (body_key or "").strip() or os.environ.get("GROQ_API_KEY", "").strip()


def _provider(api_key: str, **models):
    if api_key:
        return GroqProvider(api_key, **models)
    return MockProvider()


def _running_jobs() -> int:
    """Jobs actually using a model slot. A book awaiting outline review is idle."""
    return sum(1 for job_id in list(store._jobs) if store._jobs[job_id].busy)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "mode": "live" if config.has_server_key() else "demo",
        "models": {
            "architect": config.ARCHITECT_MODEL,
            "writer": config.WRITER_MODEL,
            "editor": config.EDITOR_MODEL,
        },
    }


@app.get("/api/models")
async def available_groq_models(request: Request) -> dict:
    """Fetch the active model catalog for this request's Groq key."""
    api_key = _client_key(request)
    if not api_key:
        raise HTTPException(409, "add a Groq API key to load models available to your account")
    if not api_key.startswith("gsk_"):
        raise HTTPException(400, "that does not look like a Groq API key (expected gsk_...)")
    provider = GroqProvider(api_key)
    try:
        models = await provider.list_models()
    except LLMError as exc:
        status = exc.status if exc.status and 400 <= exc.status < 500 else 502
        raise HTTPException(status, str(exc)) from exc
    except Exception as exc:
        log.warning("could not load Groq models: %s", exc)
        raise HTTPException(502, "could not load the Groq model list; try again") from exc
    finally:
        await provider.aclose()
    return {
        "models": models,
        "defaults": {
            "architect": config.ARCHITECT_MODEL,
            "writer": config.WRITER_MODEL,
            "editor": config.EDITOR_MODEL,
        },
    }


@app.get("/api/presets")
async def presets() -> dict:
    return config.presets_payload()


@app.post("/api/books")
async def create_book(payload: BookRequest, request: Request) -> JSONResponse:
    if _running_jobs() >= MAX_RUNNING_JOBS:
        raise HTTPException(429, "three books are already being written; wait for one to finish")

    api_key = _client_key(request)
    if api_key and not api_key.startswith("gsk_"):
        raise HTTPException(400, "that does not look like a Groq API key (expected gsk_...)")

    selected_models = {
        "architect_model": payload.architect_model,
        "writer_model": payload.writer_model,
        "editor_model": payload.editor_model,
    }
    if not api_key and any(selected_models.values()):
        raise HTTPException(409, "Groq model selection requires a Groq API key; demo mode uses fixed fixtures")

    directives, strength, extra = expand_profile(payload)
    spec = BookSpec(
        topic=payload.topic,
        audience=payload.audience,
        tone=payload.tone,
        length_id=payload.length,
        directives=directives,
        extra_instructions=extra,
        chapters_override=payload.chapters,
        words_override=payload.words_per_chapter,
        stylist=payload.stylist,
        stylist_strength=strength,
        banned_phrases=payload.banned_phrases,
        notes=payload.notes,
        voice_sample=payload.voice_sample,
        pov=payload.pov,
        review_outline=payload.review_outline,
        unit_label=payload.unit_label,
        part_label=payload.part_label,
        subheading_style=payload.subheading_style,
        group_count_override=payload.group_count,
    )
    provider = _provider(api_key, **selected_models)
    job = store.create(spec, provider)
    job.task = asyncio.create_task(run_book(job, spec, provider))
    log.info("job %s queued (%s): %s", job.id, provider.name, payload.topic)

    return JSONResponse(
        {
            "id": job.id,
            "mode": provider.name,
            "unit_label": spec.unit_label,
            "part_label": spec.part_label,
            "estimate": estimate(spec),
            "models": {
                "architect": provider.architect_model,
                "writer": provider.writer_model,
                "editor": provider.editor_model,
            },
        },
        status_code=202,
    )


@app.get("/api/books/{job_id}")
async def get_book(job_id: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "no such book")
    return job.snapshot()


@app.get("/api/books/{job_id}/style-report")
async def style_report(job_id: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "no such book")
    return job.style_report()


@app.post("/api/books/{job_id}/chapters/{index}/revert")
async def revert_chapter(job_id: str, index: int) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "no such book")
    if not job.revert_chapter(index):
        raise HTTPException(409, "that chapter has no editorial rewrite to undo")
    return {
        "ok": True,
        "text": job.chapters[index]["text"],
        "report": job.style_report(),
    }


@app.post("/api/books/{job_id}/outline")
async def approve_outline(job_id: str, payload: dict = Body(...)) -> dict:
    """Accept an author-edited outline and start writing.

    The body is the same JSON shape the architect returns; it is re-validated
    here, so an edit cannot smuggle in a structure the writer cannot use.
    """
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "no such book")
    if job.status != "awaiting_outline":
        raise HTTPException(409, "this book is not waiting for an outline review")
    spec = job.spec
    try:
        plan = parse_plan(
            payload,
            topic=spec.topic,
            audience=spec.audience,
            tone=spec.tone,
            words_per_chapter=spec.length.words_per_chapter,
            max_chapters=config.MAX_CHAPTERS,
        )
    except PlanError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        job.apply_outline(plan)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    log.info("job %s outline approved: %s chapters", job.id, len(plan.chapters))
    return {"ok": True, "plan": plan.to_dict(), "stats": job.stats()}


@app.post("/api/books/{job_id}/chapters/{index}/regenerate")
async def regenerate_chapter(
    job_id: str,
    index: int,
    payload: ChapterRegenerationRequest,
    request: Request,
) -> dict:
    """Rewrite one completed chapter under a user-supplied command."""
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "no such book")
    if job.status not in {"done", "partial"}:
        raise HTTPException(409, "wait until book writing is finished before regenerating a chapter")
    state = job.chapters.get(index)
    if not state or state.get("status") != "done":
        raise HTTPException(409, "that chapter is not available to regenerate")
    if state.get("regenerating"):
        raise HTTPException(409, "that chapter is already being regenerated")
    if _running_jobs() >= MAX_RUNNING_JOBS:
        raise HTTPException(429, "three model jobs are already running; wait for one to finish")

    api_key = _client_key(request)
    if not api_key:
        raise HTTPException(409, "custom chapter regeneration needs a Groq API key; demo prose is fixed")
    if not api_key.startswith("gsk_"):
        raise HTTPException(400, "that does not look like a Groq API key (expected gsk_...)")

    state["regenerating"] = True
    provider = None
    try:
        provider = GroqProvider(api_key)
        chapter = job.plan.chapter(index)
        part_title = next(
            (part.title for part in job.plan.parts if chapter in part.chapters), ""
        )
        previous_summaries = [
            (job.chapters[i]["title"], job.chapters[i].get("summary", ""))
            for i in sorted(job.chapters)
            if i < index and job.chapters[i].get("summary")
        ]
        bible = prompts.build_bible(
            job.plan,
            previous_summaries,
            [],
            style=job.spec.style,
            notes=job.spec.clean_notes(),
        )
        revised, usage = await provider.regenerate_chapter(
            plan=job.plan,
            chapter=chapter,
            part_title=part_title,
            bible=bible,
            current_text=state["text"],
            instruction=payload.instruction,
            style=job.spec.style,
            pov=job.spec.pov_text,
            voice_sample=job.spec.clean_voice_sample(),
        )
        if not revised.strip():
            raise HTTPException(502, "the model returned an empty chapter; the existing text was kept")
        job.usage.merge(usage)
        try:
            summary_data, summary_usage = await provider.summarize(chapter=chapter, text=revised)
            job.usage.merge(summary_usage)
            summary = str(summary_data.get("summary") or "").strip()
        except Exception as exc:  # noqa: BLE001 - summary is useful but never blocks the rewrite
            log.warning("summary failed after regenerating chapter %s: %s", index, exc)
            summary = ""
        if not job.apply_regenerated_chapter(
            index, revised, instruction=payload.instruction, summary=summary
        ):
            raise HTTPException(502, "the model returned no usable chapter; the existing text was kept")
        log.info("job %s chapter %s regenerated by author command", job.id, index)
        chapter_view = next(
            item for item in job.snapshot()["chapters"] if item["index"] == index
        )
        return {
            "ok": True,
            "chapter": chapter_view,
            "stats": job.stats(),
            "style_report": job.style_report(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("chapter regeneration failed for job %s chapter %s: %s", job.id, index, exc)
        raise HTTPException(502, "chapter regeneration failed; the existing chapter was kept") from exc
    finally:
        state["regenerating"] = False
        if provider is not None:
            await provider.aclose()


@app.post("/api/books/{job_id}/chapters/{index}/undo-regeneration")
async def undo_chapter_regeneration(job_id: str, index: int) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "no such book")
    if job.status not in {"done", "partial"}:
        raise HTTPException(409, "wait until book writing is finished before undoing a regeneration")
    if not job.undo_regeneration(index):
        raise HTTPException(409, "that chapter has no regeneration to undo")
    chapter_view = next(item for item in job.snapshot()["chapters"] if item["index"] == index)
    return {
        "ok": True,
        "chapter": chapter_view,
        "stats": job.stats(),
        "style_report": job.style_report(),
    }


@app.post("/api/books/{job_id}/cancel")
async def cancel_book(job_id: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "no such book")
    job.cancel()
    return {"ok": True, "status": job.status}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/books/{job_id}/events")
async def book_events(job_id: str) -> StreamingResponse:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "no such book")

    async def stream():
        queue = job.subscribe()
        try:
            # Always start with a full snapshot so a reconnecting client (or a
            # client that missed deltas) has an exact picture.
            yield _sse("snapshot", job.snapshot())
            if job.terminal and queue.empty():
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    if job.terminal and queue.empty():
                        return
                    continue
                if event["type"] == "resync":
                    yield _sse("snapshot", job.snapshot())
                    continue
                yield _sse(event["type"], event)
                if event["type"] in {"done", "error", "cancelled"}:
                    return
        finally:
            job.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/books/{job_id}/export/{fmt}")
async def export_book(
    job_id: str,
    fmt: str,
    page_size: Literal["a5", "a4", "letter"] = "a5",
    margins: Literal["narrow", "normal", "wide"] = "normal",
    chapter_heading: Literal["chapter", "number", "title"] = "chapter",
    body_font_size: int = Query(default=10, ge=9, le=14),
    line_spacing: Literal["compact", "normal", "airy"] = "normal",
    paragraph_style: Literal["indent", "space"] = "indent",
    alignment: Literal["justify", "left"] = "justify",
    chapter_new_page: bool = True,
    running_matter: bool = True,
    page_numbers: bool = True,
    unit_label: Literal["chapter", "section", "essay", "entry"] | None = None,
    part_label: Literal["part", "book", "volume"] | None = None,
    title: str = Query(default="", max_length=120),
    subtitle: str = Query(default="", max_length=180),
    show_title_page: bool = True,
    show_subtitle: bool = True,
    header_left: str = Query(default="", max_length=80),
    header_right: str = Query(default="BOOKWRIGHT", max_length=80),
) -> Response:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "no such book")
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(400, f"format must be one of {sorted(EXPORT_FORMATS)}")
    if not job.plan:
        raise HTTPException(409, "this book has no plan yet — nothing to export")
    chapters = {
        index: {"text": state["text"]}
        for index, state in job.chapters.items()
        if state["status"] in {"done", "writing"} and state["text"].strip()
    }
    if not chapters:
        raise HTTPException(409, "no chapters have been written yet")

    options = ExportOptions(
        page_size=page_size,
        margins=margins,
        chapter_heading=chapter_heading,
        body_font_size=body_font_size,
        line_spacing=line_spacing,
        paragraph_style=paragraph_style,
        alignment=alignment,
        chapter_new_page=chapter_new_page,
        running_matter=running_matter,
        page_numbers=page_numbers,
        unit_label=unit_label or job.spec.unit_label,
        part_label=part_label or job.spec.part_label,
        title_override=title,
        subtitle_override=subtitle,
        show_title_page=show_title_page,
        show_subtitle=show_subtitle,
        header_left=header_left,
        header_right=header_right,
    )
    try:
        # PDF layout is CPU-bound; keep it off the event loop.
        data, filename, media_type = await asyncio.to_thread(
            export, job.plan, chapters, fmt, options=options
        )
    except Exception as exc:
        log.exception("export failed (%s)", fmt)
        raise HTTPException(500, f"export failed: {exc}") from exc

    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Turn pydantic noise into one sentence a human can act on."""
    parts = []
    for error in exc.errors():
        field = ".".join(str(p) for p in error.get("loc", ()) if p != "body") or "request"
        msg = error.get("msg", "is invalid")
        if "Value error, " in msg:
            msg = msg.split("Value error, ", 1)[1]
        parts.append(f"{field}: {msg.lower() if msg[:1].isupper() else msg}")
    return JSONResponse({"error": "; ".join(parts) or "invalid request"}, status_code=422)
