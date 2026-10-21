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

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from bookwright import config
from bookwright.exporters import export
from bookwright.jobs import BookSpec, JobStore, estimate, run_book
from bookwright.llm import GroqProvider
from bookwright.mock import MockProvider

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

    @field_validator("tone")
    @classmethod
    def _known_tone(cls, value: str) -> str:
        if value not in config.TONES:
            raise ValueError(f"unknown tone: {value}")
        return value


def _client_key(request: Request, body_key: str | None = None) -> str:
    header = request.headers.get("x-groq-key", "").strip()
    return header or (body_key or "").strip() or os.environ.get("GROQ_API_KEY", "").strip()


def _provider(api_key: str):
    if api_key:
        return GroqProvider(api_key)
    return MockProvider()


def _running_jobs() -> int:
    return sum(1 for job_id in list(store._jobs) if not store._jobs[job_id].terminal)


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

    spec = BookSpec(
        topic=payload.topic, audience=payload.audience, tone=payload.tone, length_id=payload.length
    )
    provider = _provider(api_key)
    job = store.create(spec, provider)
    job.task = asyncio.create_task(run_book(job, spec, provider))
    log.info("job %s queued (%s): %s", job.id, provider.name, payload.topic)

    return JSONResponse(
        {
            "id": job.id,
            "mode": provider.name,
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
async def export_book(job_id: str, fmt: str) -> Response:
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

    try:
        # PDF layout is CPU-bound; keep it off the event loop.
        data, filename, media_type = await asyncio.to_thread(export, job.plan, chapters, fmt)
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
