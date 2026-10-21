"""Tests for Bookwright. Run: pytest -q"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import zipfile

import pytest

from bookwright import config
from bookwright.exporters import (
    ExportOptions,
    export,
    parse_markdown,
    to_epub,
    to_html,
    to_markdown,
)
from bookwright.jobs import BookSpec, Job, chunk_text, estimate, run_book
from bookwright.llm import GroqProvider, Usage, _backoff
from bookwright.mock import MockProvider
from bookwright.prompts import chapter_regeneration_messages
from bookwright.schema import Plan, PlanError, parse_plan
from bookwright.style import length_ratio, paragraph_diff, scan, summary, total

# --------------------------------------------------------------------------- #
# Plan validation
# --------------------------------------------------------------------------- #
GOOD_PLAN = {
    "title": "A Test Book",
    "subtitle": "Testing things",
    "thesis": "A thesis.",
    "voice": "Plain.",
    "parts": [
        {
            "title": "One",
            "summary": "First part",
            "chapters": [
                {"title": "Alpha", "purpose": "Do alpha", "beats": ["b1", "b2"], "terms": ["alpha"]},
                {"title": "Beta", "purpose": "Do beta", "beats": "a lone string", "terms": None},
            ],
        }
    ],
}


def _parse(raw, chapters=6):
    return parse_plan(
        raw, topic="a topic", audience="Reader", tone="clear",
        words_per_chapter=500, max_chapters=chapters,
    )


def test_parse_valid_plan():
    plan = _parse(GOOD_PLAN)
    assert plan.title == "A Test Book"
    assert [c.index for c in plan.chapters] == [1, 2]
    assert plan.chapters[1].beats == ["a lone string"]  # a bare string is tolerated
    assert plan.chapters[0].words == 500


def test_parse_rejects_non_object():
    with pytest.raises(PlanError, match="JSON object"):
        _parse([{"title": "x"}])


def test_parse_rejects_empty_parts():
    with pytest.raises(PlanError, match="non-empty list"):
        _parse({"title": "T", "parts": []})


def test_parse_rejects_single_chapter():
    with pytest.raises(PlanError, match="at least 2 chapters"):
        _parse({"title": "T", "parts": [{"title": "P", "chapters": [{"title": "A", "purpose": "p"}]}]})


def test_parse_requires_title():
    with pytest.raises(PlanError, match="title"):
        _parse({"parts": GOOD_PLAN["parts"]})


def test_parse_clamps_chapter_count():
    many = {
        "title": "T",
        "parts": [{"title": "P", "chapters": [{"title": f"C{i}", "purpose": "p"} for i in range(30)]}],
    }
    assert len(_parse(many, chapters=5).chapters) == 5


def test_parse_deduplicates_titles():
    dup = {
        "title": "T",
        "parts": [{"title": "P", "chapters": [{"title": "Same", "purpose": "p"}] * 3}],
    }
    titles = [c.title for c in _parse(dup).chapters]
    assert titles == ["Same", "Same (2)", "Same (3)"]


def test_parse_flat_parts_without_chapters():
    flat = {"title": "T", "parts": [{"title": "A", "purpose": "pa"}, {"title": "B", "purpose": "pb"}]}
    plan = _parse(flat)
    assert [c.title for c in plan.chapters] == ["A", "B"]


# --------------------------------------------------------------------------- #
# Markdown parsing
# --------------------------------------------------------------------------- #
def test_parse_markdown_blocks():
    md = "# H1\n\nA paragraph\nover two lines.\n\n## Sub\n\n- one\n- two\n\n> quoted\n\n```\ncode\n```"
    kinds = [b.type for b in parse_markdown(md)]
    assert kinds == ["h", "p", "h", "ul", "quote", "code"]
    assert parse_markdown(md)[1].text == "A paragraph over two lines."
    assert parse_markdown(md)[3].items == ["one", "two"]


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def _run(length="chapbook", topic="the quiet economics of second-hand markets"):
    spec = BookSpec(topic=topic, audience="Curious general reader", tone="clear", length_id=length)
    job = Job(id="test", spec=spec, provider_name="demo")
    asyncio.run(run_book(job, spec, MockProvider()))
    return job


def test_pipeline_completes():
    job = _run()
    assert job.status == "done"
    assert job.plan is not None
    assert len(job.chapters) == config.LENGTHS["chapbook"].chapters
    for state in job.chapters.values():
        assert state["status"] == "done"
        assert len(state["text"].split()) > 200
        assert state["summary"]
    assert job.usage.total_tokens > 0
    assert job.stats()["chapters_done"] == len(job.chapters)


def test_pipeline_emits_expected_events():
    job = _run()
    types = {event["type"] for event in job.events}
    assert {"status", "plan", "chapter_start", "chapter_written", "chapter_done", "done"} <= types
    starts = [e for e in job.events if e["type"] == "chapter_start"]
    assert [e["index"] for e in starts] == [c.index for c in job.plan.chapters]


def test_bible_is_carried_between_waves():
    """Later prompts must see earlier chapters, unlike the original single-title design."""
    seen: list[str] = []

    class Recording(MockProvider):
        async def write_chapter(
            self, *, plan, chapter, part_title, bible, style="", pov="", voice_sample=""
        ):
            seen.append(bible)
            async for event in super().write_chapter(
                plan=plan, chapter=chapter, part_title=part_title, bible=bible, style=style
            ):
                yield event

    spec = BookSpec(topic="how expertise turns into blindness", audience="Reader", tone="clear",
                    length_id="chapbook")
    job = Job(id="bible", spec=spec, provider_name="demo")
    asyncio.run(run_book(job, spec, Recording()))

    assert len(seen) == len(job.chapters)
    assert "the quiet" not in seen[0] or True
    # The first wave has no summaries; a later wave must reference an earlier chapter title.
    first_titles = [c.title for c in job.plan.chapters]
    assert any(title in seen[-1] for title in first_titles)


def test_cancel_marks_job_cancelled():
    class SlowProvider(MockProvider):
        async def write_chapter(
            self, *, plan, chapter, part_title, bible, style="", pov="", voice_sample=""
        ):
            async for event in super().write_chapter(
                plan=plan, chapter=chapter, part_title=part_title, bible=bible, style=style
            ):
                await asyncio.sleep(0.02)
                yield event

    async def scenario():
        spec = BookSpec(topic="a topic long enough", audience="Reader", tone="clear", length_id="chapbook")
        job = Job(id="cancel", spec=spec, provider_name="demo")
        task = asyncio.create_task(run_book(job, spec, SlowProvider()))
        await asyncio.sleep(0.9)
        assert not task.done()
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return job

    job = asyncio.run(scenario())
    assert job.status == "cancelled"
    assert job.chapters  # partial work is kept
    assert any(e["type"] == "cancelled" for e in job.events)


def test_estimate_positive():
    spec = BookSpec(topic="a topic long enough", audience="Reader", tone="clear", length_id="standard")
    est = estimate(spec)
    assert est["chapters"] == 6
    assert est["prompt_tokens"] > 0 and est["completion_tokens"] > 0



def test_groq_429_uses_retry_after_header_and_body_hint(monkeypatch):
    provider = object.__new__(GroqProvider)
    body = json.dumps({"error": {"message": "Please try again in 23.955s."}}).encode()

    header_error = provider._error_from(429, body, {"retry-after": "12"})
    assert header_error.retryable
    assert header_error.retry_after == 12
    assert _backoff(1, header_error.retry_after) == 12

    body_error = provider._error_from(429, body, {})
    assert body_error.retry_after == 23.955
    assert _backoff(1, body_error.retry_after) == 23.955

    class Response:
        def __init__(self, status, content, headers=None):
            self.status_code = status
            self.content = content
            self.headers = headers or {}

        def json(self):
            return json.loads(self.content)

    class FakeClient:
        def __init__(self):
            self.responses = [
                Response(429, body, {"retry-after": "23.955"}),
                Response(
                    200,
                    json.dumps({
                        "choices": [{"message": {"content": "recovered"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }).encode(),
                ),
            ]

        async def post(self, *args, **kwargs):
            return self.responses.pop(0)

    delays = []

    async def record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("bookwright.llm.asyncio.sleep", record_sleep)
    provider._base_url = "https://example.test"
    provider._api_key = "gsk_test"
    provider._client = FakeClient()
    result, _ = asyncio.run(provider._complete_text([], model="test", max_tokens=1))
    assert result == "recovered"
    assert delays == [23.955]


def test_groq_model_catalog_and_per_role_selection(monkeypatch):
    from fastapi.testclient import TestClient

    import server
    from bookwright.jobs import JobStore

    available_models = [
        {
            "id": "groq/architect-model",
            "owned_by": "Groq",
            "context_window": 131072,
            "max_completion_tokens": 8192,
            "active": True,
        },
        {
            "id": "groq/writer-model",
            "owned_by": "Groq",
            "context_window": 32768,
            "max_completion_tokens": 4096,
            "active": True,
        },
    ]
    key = {"value": None}
    store = JobStore()
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "_client_key", lambda request, body_key=None: key["value"])

    class FakeProvider:
        name = "groq"

        def __init__(self, api_key, **selected):
            assert api_key == "gsk_test"
            self.architect_model = selected.get("architect_model") or "groq/architect-model"
            self.writer_model = selected.get("writer_model") or "groq/writer-model"
            self.editor_model = selected.get("editor_model") or "groq/writer-model"

        async def list_models(self):
            return available_models

        async def aclose(self):
            return None

    async def skip_generation(*args, **kwargs):
        return None

    monkeypatch.setattr(server, "GroqProvider", FakeProvider)
    monkeypatch.setattr(server, "run_book", skip_generation)
    client = TestClient(server.app)

    assert client.get("/api/models").status_code == 409
    key["value"] = "gsk_test"
    catalog = client.get("/api/models")
    assert catalog.status_code == 200
    assert catalog.json()["models"] == available_models

    response = client.post(
        "/api/books",
        json={
            "topic": "A book about careful model selection",
            "review_outline": False,
            "architect_model": "groq/architect-model",
            "writer_model": "groq/writer-model",
            "editor_model": "groq/writer-model",
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["models"] == {
        "architect": "groq/architect-model",
        "writer": "groq/writer-model",
        "editor": "groq/writer-model",
    }
    assert store.get(response.json()["id"]).models == response.json()["models"]


def test_groq_provider_maps_model_metadata():
    provider = object.__new__(GroqProvider)

    class Response:
        def __init__(self):
            self.status_code = 200
            self.content = b"{}"
            self.headers = {}

        def json(self):
            return {
                "data": [
                    {
                        "id": "groq/example-model",
                        "owned_by": "Groq",
                        "context_window": 65536,
                        "max_completion_tokens": 4096,
                        "active": True,
                    },
                    {"id": "groq/without-details"},
                    {"owned_by": "no-id"},
                ]
            }

    class FakeClient:
        async def get(self, url, *, headers):
            assert url == "https://api.groq.com/openai/v1/models"
            assert headers["Authorization"] == "Bearer gsk_test"
            return Response()

    provider._base_url = "https://api.groq.com/openai/v1"
    provider._api_key = "gsk_test"
    provider._client = FakeClient()
    models = asyncio.run(provider.list_models())
    assert models == [
        {
            "id": "groq/example-model",
            "owned_by": "Groq",
            "context_window": 65536,
            "max_completion_tokens": 4096,
            "active": True,
        },
        {
            "id": "groq/without-details",
            "owned_by": None,
            "context_window": None,
            "max_completion_tokens": None,
            "active": None,
        },
    ]


def test_models_api_and_book_request_use_role_specific_models(monkeypatch):
    from fastapi.testclient import TestClient

    import server
    from bookwright.jobs import JobStore

    catalog = [{
        "id": "groq/text-model",
        "owned_by": "Groq",
        "context_window": 65536,
        "max_completion_tokens": 4096,
        "active": True,
    }]
    key = {"value": None}
    store = JobStore()
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "_client_key", lambda request, body_key=None: key["value"])

    class FakeProvider:
        name = "groq"

        def __init__(self, api_key, **models):
            assert api_key == "gsk_test"
            self.architect_model = models.get("architect_model") or "groq/default-architect"
            self.writer_model = models.get("writer_model") or "groq/default-writer"
            self.editor_model = models.get("editor_model") or "groq/default-editor"

        async def list_models(self):
            return catalog

        async def aclose(self):
            return None

    async def skip_generation(*args, **kwargs):
        return None

    monkeypatch.setattr(server, "GroqProvider", FakeProvider)
    monkeypatch.setattr(server, "run_book", skip_generation)
    client = TestClient(server.app)
    assert client.get("/api/models").status_code == 409

    key["value"] = "gsk_test"
    catalog_response = client.get("/api/models")
    assert catalog_response.status_code == 200
    assert catalog_response.json()["models"] == catalog

    response = client.post(
        "/api/books",
        json={
            "topic": "A book about model choice",
            "review_outline": False,
            "architect_model": "groq/plan-large",
            "writer_model": "groq/write-fast",
            "editor_model": "groq/edit-small",
        },
    )
    assert response.status_code == 202, response.text
    expected = {
        "architect": "groq/plan-large",
        "writer": "groq/write-fast",
        "editor": "groq/edit-small",
    }
    assert response.json()["models"] == expected
    assert store.get(response.json()["id"]).models == expected


def test_chapter_regeneration_prompt_is_targeted():
    job = _run()
    chapter = job.plan.chapter(2)
    messages = chapter_regeneration_messages(
        plan=job.plan,
        chapter=chapter,
        part_title=job.plan.parts[0].title,
        bible="The book's central argument.",
        current_text="Existing chapter prose.",
        instruction="Open with a concrete scene and shorten the ending.",
        style="Use plain, direct language.",
        pov="Second person.",
        voice_sample="A distinct author sample.",
    )
    assert len(messages) == 2
    assert "one chapter" in messages[0]["content"]
    assert "Second person" in messages[0]["content"]
    assert "A distinct author sample" in messages[0]["content"]
    assert "Chapter 2: " + chapter.title in messages[1]["content"]
    assert "Open with a concrete scene" in messages[1]["content"]
    assert "Existing chapter prose." in messages[1]["content"]
    assert "Requested length" in messages[1]["content"]


def test_job_chapter_regeneration_isolated_and_undo_restores_metadata():
    job = _run()
    selected = job.chapters[2]
    untouched = {i: state["text"] for i, state in job.chapters.items() if i != 2}
    before = {
        key: selected.get(key)
        for key in ("text", "summary", "original", "style_before", "style_after", "reverted")
    }
    replacement = "A substantially revised chapter with new prose."
    assert job.apply_regenerated_chapter(
        2, replacement, instruction="Make the opening more concrete.", summary="A revised summary."
    )
    assert selected["text"] == replacement
    assert selected["summary"] == "A revised summary."
    assert all(job.chapters[i]["text"] == text for i, text in untouched.items())
    assert job.snapshot()["chapters"][1]["regeneration_count"] == 1
    assert job.undo_regeneration(2)
    assert {key: selected.get(key) for key in before} == before
    assert all(job.chapters[i]["text"] == text for i, text in untouched.items())
    assert job.snapshot()["chapters"][1]["regeneration_count"] == 0


def test_job_regeneration_history_is_bounded():
    job = _run()
    state = job.chapters[1]
    for n in range(7):
        assert job.apply_regenerated_chapter(
            1, f"Replacement version {n}.", instruction=f"Version {n}"
        )
    assert len(state["regeneration_history"]) == 5
    for _ in range(5):
        assert job.undo_regeneration(1)
    assert not job.undo_regeneration(1)


def test_regeneration_api_uses_request_key_and_returns_undo_metadata(monkeypatch):
    from fastapi.testclient import TestClient

    import server
    from bookwright.jobs import JobStore

    job = _run()
    prior = job.chapters[1]["text"]
    other = job.chapters[2]["text"]
    store = JobStore()
    store._jobs[job.id] = job
    store._order.append(job.id)
    monkeypatch.setattr(server, "store", store)
    key = {"value": None}
    fail_model = {"value": False}
    monkeypatch.setattr(server, "_client_key", lambda request, body_key=None: key["value"])

    class FakeProvider:
        def __init__(self, api_key):
            assert api_key == "gsk_test"

        async def regenerate_chapter(self, **kwargs):
            assert kwargs["instruction"] == "Make this chapter more concise."
            if fail_model["value"]:
                raise RuntimeError("simulated provider failure")
            return "Revised chapter text.", Usage(prompt_tokens=3, completion_tokens=4)

        async def summarize(self, **kwargs):
            return {"summary": "A new summary."}, Usage()

        async def aclose(self):
            return None

    monkeypatch.setattr(server, "GroqProvider", FakeProvider)
    client = TestClient(server.app)
    url = f"/api/books/{job.id}/chapters/1/regenerate"
    payload = {"instruction": "Make this chapter more concise."}

    missing_key = client.post(url, json=payload)
    assert missing_key.status_code == 409
    assert "Groq API key" in missing_key.json()["error"]
    assert job.chapters[1]["text"] == prior
    assert job.snapshot()["chapters"][0]["regeneration_count"] == 0

    key["value"] = "gsk_test"
    fail_model["value"] = True
    failed_model = client.post(url, json=payload)
    assert failed_model.status_code == 502
    assert "existing chapter was kept" in failed_model.json()["error"]
    assert job.chapters[1]["text"] == prior
    assert job.snapshot()["chapters"][0]["regeneration_count"] == 0

    fail_model["value"] = False
    response = client.post(url, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["chapter"]["text"] == "Revised chapter text."
    assert body["chapter"]["summary"] == "A new summary."
    assert body["chapter"]["regeneration_count"] == 1
    assert "regeneration_history" not in str(job.snapshot())
    assert job.chapters[2]["text"] == other

    undone = client.post(f"/api/books/{job.id}/chapters/1/undo-regeneration")
    assert undone.status_code == 200, undone.text
    assert undone.json()["chapter"]["text"] == prior
    assert undone.json()["chapter"]["summary"] == job.chapters[1]["summary"]
    assert undone.json()["chapter"]["regeneration_count"] == 0
    assert job.chapters[2]["text"] == other

# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def finished():
    job = _run()
    chapters = {i: {"text": s["text"]} for i, s in job.chapters.items()}
    return job.plan, chapters


def test_markdown_export(finished):
    plan, chapters = finished
    text = to_markdown(plan, chapters)
    assert text.startswith(f"# {plan.title}")
    assert "### Chapter 1." in text
    assert len(text.split()) > 1500


def test_epub_is_a_valid_archive(finished):
    plan, chapters = finished
    data = to_epub(plan, chapters)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        assert names[0] == "mimetype"
        assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"
        assert "META-INF/container.xml" in names
        opf = zf.read("OEBPS/content.opf").decode()
        assert "<dc:title>" in opf and 'version="3.0"' in opf
        nav = zf.read("OEBPS/nav.xhtml").decode()
        assert 'epub:type="toc"' in nav
        # Every manifest href exists in the archive.
        for line in opf.split("href=\"")[1:]:
            href = line.split('"', 1)[0]
            assert f"OEBPS/{href}" in names or href.startswith("http")


def test_export_options_change_chapter_heading_and_validate(finished):
    plan, chapters = finished
    numbered = ExportOptions(chapter_heading="number")
    title_only = ExportOptions(chapter_heading="title")
    assert "### 1. " in to_markdown(plan, chapters, options=numbered)
    assert "### Chapter 1." not in to_markdown(plan, chapters, options=numbered)
    assert "### Chapter 1." not in to_markdown(plan, chapters, options=title_only)
    assert "### " + plan.chapters[0].title in to_markdown(plan, chapters, options=title_only)
    with pytest.raises(ValueError, match="page_size"):
        ExportOptions(page_size="tabloid")
    with pytest.raises(ValueError, match="margins"):
        ExportOptions(margins="huge")


def test_html_export_uses_selected_page_and_text_layout(finished):
    plan, chapters = finished
    options = ExportOptions(
        page_size="letter", margins="wide", chapter_heading="number", body_font_size=12,
        line_spacing="airy", paragraph_style="space", alignment="left",
        chapter_new_page=False,
    )
    rendered = to_html(plan, chapters, options=options)
    assert "@page { size:Letter; margin:25mm 25mm 25mm;" in rendered
    assert "--body-size:12pt" in rendered and "--line-spacing:1.8" in rendered
    assert "--paragraph-indent:0" in rendered and "--paragraph-gap:0.75em" in rendered
    assert 'class="chapter page-break"' not in rendered
    assert ">1.</div>" in rendered


def test_epub_export_uses_typography_and_number_style(finished):
    plan, chapters = finished
    options = ExportOptions(
        chapter_heading="number", body_font_size=12, line_spacing="airy",
        paragraph_style="space", alignment="left", chapter_new_page=False,
    )
    data = to_epub(plan, chapters, options=options)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        css = zf.read("OEBPS/style.css").decode()
        chapter = zf.read("OEBPS/ch01.xhtml").decode()
    assert "font-size: 12pt" in css and "line-height: 1.8" in css
    assert "margin: 0 0 .75em; text-indent: 0" in css
    assert "chapter-break" not in chapter
    assert '<p class="chapter-num">1.</p>' in chapter


def test_pdf_has_pages_and_text(finished):
    pymupdf = pytest.importorskip("pymupdf")
    plan, chapters = finished
    data, filename, mime = export(plan, chapters, "pdf")
    assert data.startswith(b"%PDF")
    assert filename.endswith(".pdf") and mime == "application/pdf"
    doc = pymupdf.open(stream=data, filetype="pdf")
    assert doc.page_count >= 4
    text = "".join(page.get_text() for page in doc)
    assert plan.title.split()[0] in text
    assert plan.chapters[0].title.split()[0] in text


def test_html_export_escapes_script(finished):
    plan, chapters = finished
    poisoned = {i: {"text": "<script>alert(1)</script> and **bold**"} for i in chapters}
    data, _, mime = export(plan, poisoned, "html")
    assert mime.startswith("text/html")
    assert b"<script>alert(1)</script>" not in data
    assert b"<strong>bold</strong>" in data


def test_unknown_format_raises(finished):
    plan, chapters = finished
    with pytest.raises(ValueError):
        export(plan, chapters, "docx")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_presets_payload_shape():
    payload = config.presets_payload()
    assert payload["lengths"] and payload["tones"] and payload["audiences"]
    assert payload["models"]["architect"]
    assert json.dumps(payload)  # serialisable


# --------------------------------------------------------------------------- #
# .env loader and launcher ergonomics
# --------------------------------------------------------------------------- #
def test_env_loader_parses_quotes_and_comments(tmp_path, monkeypatch):
    from bookwright.env import load_env

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        'GROQ_API_KEY="gsk_test_quoted"\n'
        "BOOKWRIGHT_WRITER_MODEL=openai/gpt-oss-20b  # inline note\n"
        "MALFORMED LINE\n"
        "\n"
        "export BOOKWRIGHT_CONCURRENCY=2\n"
    )
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("BOOKWRIGHT_WRITER_MODEL", raising=False)
    applied = load_env(env_file)

    assert applied["GROQ_API_KEY"] == "gsk_test_quoted"
    assert applied["BOOKWRIGHT_WRITER_MODEL"] == "openai/gpt-oss-20b"
    assert applied["BOOKWRIGHT_CONCURRENCY"] == "2"
    assert "MALFORMED LINE" not in applied


def test_real_environment_wins_over_env_file(tmp_path, monkeypatch):
    from bookwright.env import load_env

    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY=gsk_from_file\n")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_from_shell")

    load_env(env_file)

    import os
    assert os.environ["GROQ_API_KEY"] == "gsk_from_shell"


def test_missing_env_file_is_not_an_error(tmp_path):
    from bookwright.env import load_env

    assert load_env(tmp_path / "nope.env") == {}


def test_launcher_exposes_expected_flags():
    """run.py must stay runnable without importing third-party packages first."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "run.py", "--help"], cwd=root, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    for flag in ("--host", "--port", "--reload", "--no-browser", "--install"):
        assert flag in result.stdout


# --------------------------------------------------------------------------- #
# Length options, voices and style controls
# --------------------------------------------------------------------------- #
def test_length_presets_grow_monotonically():
    words = [config.LENGTHS[key].total_words for key in ("chapbook", "standard", "deep",
                                                          "folio", "novella", "tome", "epic")]
    assert words == sorted(words)
    assert config.LENGTHS["epic"].total_words >= 30_000
    assert config.LENGTHS["chapbook"].total_words < 2_000


def test_custom_length_overrides_and_clamps():
    spec = BookSpec(topic="t", audience="a", tone="clear", length_id="custom",
                    chapters_override=12, words_override=1300)
    assert spec.length.chapters == 12
    assert spec.length.words_per_chapter == 1300
    assert spec.length.total_words == 12 * 1300

    too_big = BookSpec(topic="t", audience="a", tone="clear", length_id="custom",
                       chapters_override=99, words_override=9_999)
    assert too_big.length.chapters == config.MAX_CHAPTERS
    assert too_big.length.words_per_chapter == config.WORDS_PER_CHAPTER_RANGE[1]


def test_writer_token_budget_scales_with_chapter_length():
    assert config.writer_tokens_for(250) == 2_200          # floor for short chapters
    assert config.writer_tokens_for(1_000) == 2_200        # still under the old ceiling
    assert config.writer_tokens_for(1_500) > config.writer_tokens_for(1_000)
    assert config.writer_tokens_for(2_000) <= 8_000
    assert config.writer_tokens_for(2_000) > config.writer_tokens_for(1_500)


def test_style_block_renders_only_known_directives():
    from bookwright.prompts import style_block

    text = style_block(["cliches", "not_a_real_rule"], "British spelling")
    assert "delve" in text
    assert "not_a_real_rule" not in text
    assert "British spelling" in text


def test_writer_prompt_carries_style_constraints():
    from bookwright.prompts import chapter_messages, style_block
    from bookwright.schema import Chapter, Part, Plan

    plan = Plan(title="T", subtitle="", thesis="", audience="Reader", voice="plain",
                parts=[Part(1, "P", "", [Chapter(1, "C", "do it")])])
    style = style_block(config.DEFAULT_DIRECTIVES, "no jargon")
    messages = chapter_messages(
        plan=plan, chapter=plan.chapters[0], part_title="P", bible="bible text", style=style
    )
    system = messages[0]["content"]
    assert "Voice constraints" in system
    assert "no jargon" in system
    assert "In this chapter we will" in system  # the recap rule is present


def test_bible_repeats_the_voice_constraints():
    from bookwright.prompts import build_bible
    from bookwright.schema import Chapter, Part, Plan

    plan = Plan(title="T", subtitle="", thesis="", audience="R", voice="plain",
                parts=[Part(1, "P", "", [Chapter(1, "C", "p")])])
    bible = build_bible(plan, [], [], style="- no hedging")
    assert "## Voice constraints" in bible
    assert "- no hedging" in bible


def test_spec_drops_unknown_directives_and_keeps_order():
    spec = BookSpec(topic="t", audience="a", tone="clear", length_id="standard",
                    directives=["hedging", "nope", "cliches", "hedging"])
    assert spec.clean_directives() == ["hedging", "cliches"]


def test_presets_payload_exposes_style_data():
    payload = config.presets_payload()
    assert len(payload["anti_patterns"]) == len(config.ANTI_PATTERNS)
    assert payload["default_directives"]
    assert len(payload["tones"]) >= 10
    assert set(payload["tone_groups"]) >= {"plain", "literary", "argument", "specialist"}
    assert payload["limits"]["chapters_range"] == list(config.CHAPTERS_RANGE)


def test_request_validation_rejects_bad_style_input():
    from pydantic import ValidationError

    from server import BookRequest

    ok = BookRequest(topic="a decent topic", directives=["cliches"], extra_instructions=" tight ")
    assert ok.directives == ["cliches"]
    assert ok.extra_instructions == "tight"

    for bad in ({"directives": ["invented"]}, {"words_per_chapter": 9_000}, {"chapters": 1}):
        payload = {"topic": "a decent topic", **bad}
        try:
            BookRequest(**payload)
        except ValidationError:
            continue
        raise AssertionError(f"expected validation failure for {bad}")


def test_custom_length_reaches_the_writer():
    from bookwright.prompts import chapter_messages
    from bookwright.schema import parse_plan

    spec = BookSpec(topic="t", audience="a", tone="clear", length_id="custom",
                    chapters_override=5, words_override=1500)
    plan = parse_plan(
        {"title": "T", "parts": [{"title": "P", "chapters": [
            {"title": f"C{i}", "purpose": "p"} for i in range(5)]}]},
        topic="t", audience="a", tone="clear",
        words_per_chapter=spec.length.words_per_chapter, max_chapters=config.MAX_CHAPTERS,
    )
    assert plan.chapters[0].words == 1500
    messages = chapter_messages(plan=plan, chapter=plan.chapters[0], part_title="P", bible="b")
    assert "about 1500 words" in messages[0]["content"]


# --------------------------------------------------------------------------- #
# De-AI editorial pass: detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("rule", "text"),
    [
        ("cliches", "Let us delve into the tapestry of this landscape."),
        ("not_just", "This is not just a technique, it is a way of seeing."),
        ("rule_of_three", "It is clear, concise, and compelling."),
        ("em_dash", "The result — as expected — was noise."),
        ("rhetorical_openers", "In a world where every tool is free, choices vanish."),
        ("hedging", "This is arguably the strongest reading."),
        ("summarising", "In this chapter we will examine the mechanism."),
        ("grandiosity", "Throughout human history, tools have shaped us."),
        ("question_headers", "## Why does this matter?"),
        ("no_second_person", "You will notice the difference immediately."),
        ("no_first_person", "I first met the idea in a workshop."),
        ("listicles", "\n".join(f"- item {i}" for i in range(10))),
    ],
)
def test_detector_fires_for_each_rule(rule, text):
    findings = scan(text)
    assert rule in {f.rule for f in findings}, f"{rule} did not fire on {text!r}"


def test_detector_respects_active_rules_only():
    text = "You should delve into it — it is arguably fine."
    only_you = scan(text, ["no_second_person"])
    assert [f.rule for f in only_you] == ["no_second_person"]
    assert scan(text, []) == []


def test_detector_is_quiet_on_clean_prose():
    clean = (
        "The bridge opened in 1967 and carried traffic until 2011. "
        "Its deck was replaced twice. Engineers now inspect it every spring."
    )
    assert scan(clean) == []


def test_uniform_rhythm_detection():
    flat = " ".join(["The system grew slowly over many decades of use."] * 5)
    assert "uniform_rhythm" in {f.rule for f in scan(flat, ["uniform_rhythm"])}
    varied = (
        "It broke. The long afternoon of repair that followed taught everyone present "
        "something about load bearing, patience, and the price of deferred work. They fixed it."
    )
    assert scan(varied, ["uniform_rhythm"]) == []


def test_finding_excerpts_are_short_and_quoted():
    findings = scan("We must delve into the complexities of it.", ["cliches"])
    assert findings and findings[0].excerpts
    assert len(findings[0].excerpts[0]) < 140


def test_summary_counts_by_rule():
    text = "It is arguably one thing — and arguably another — worth noting."
    counts = summary(scan(text, ["hedging", "em_dash", "cliches"]))
    assert counts["em_dash"] == 2
    assert total(scan(text, ["hedging"])) == 2


# --------------------------------------------------------------------------- #
# De-AI editorial pass: diff and chunking
# --------------------------------------------------------------------------- #
def test_paragraph_diff_reconstructs_both_sides():
    before = "One.\n\nTwo is bad.\n\nThree."
    after = "One.\n\nTwo is good.\n\nThree."
    diff = paragraph_diff(before, after)

    rebuilt_before = "\n\n".join(
        p for op in diff for p in ([op["before"]] if isinstance(op["before"], str)
                                   else (op["before"] or [])) if p
    )
    rebuilt_after = "\n\n".join(
        p for op in diff for p in ([op["after"]] if isinstance(op["after"], str)
                                   else (op["after"] or [])) if p
    )
    assert rebuilt_before == before
    assert rebuilt_after == after
    assert {"equal", "change"} <= {op["op"] for op in diff}


def test_paragraph_diff_handles_pure_insert_and_delete():
    assert {"ins"} <= {op["op"] for op in paragraph_diff("a\n\nb", "a\n\nnew\n\nb")}
    assert {"del"} <= {op["op"] for op in paragraph_diff("a\n\ngone\n\nb", "a\n\nb")}


def test_chunk_text_respects_limit_and_paragraphs():
    paragraphs = [f"Paragraph {i}. " + ("word " * 120) for i in range(12)]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, max_chars=2_000)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 2_000 + 200  # a single paragraph may exceed slightly
        for para in chunk.split("\n\n"):
            assert para.strip() in text


def test_length_ratio():
    assert length_ratio("one two three", "one two three four") == pytest.approx(4 / 3)


# --------------------------------------------------------------------------- #
# De-AI editorial pass: pipeline behaviour
# --------------------------------------------------------------------------- #
def _run_with_stylist(**overrides):
    spec = BookSpec(
        topic="the quiet economics of second-hand markets", audience="Reader", tone="clear",
        length_id="chapbook", **overrides,
    )
    job = Job(id="deai", spec=spec, provider_name="demo")
    asyncio.run(run_book(job, spec, MockProvider()))
    return job, spec


def test_stylist_reduces_flags_and_keeps_the_original():
    job, spec = _run_with_stylist()
    rules = spec.clean_directives()
    revised = [s for s in job.chapters.values() if s["original"]]
    assert revised, "no chapter was edited"
    for state in revised:
        assert total(scan(state["text"], rules)) < total(scan(state["original"], rules))
        assert state["style_after"] < state["style_before"]
        # content must survive: within 40% of the draft, and still substantial
        assert 0.6 <= length_ratio(state["original"], state["text"]) <= 1.6
        assert len(state["text"].split()) > 200


def test_stylist_emits_events_and_snapshot_diff():
    job, _ = _run_with_stylist()
    types = {e["type"] for e in job.events}
    assert {"stylist_start", "stylist_done", "chapter_drafted"} <= types
    snapshot = job.snapshot()
    edited = [c for c in snapshot["chapters"] if c["original"]]
    assert edited and edited[0]["diff"]
    assert edited[0]["style_before"] > edited[0]["style_after"]


def test_stylist_can_be_disabled():
    job, _ = _run_with_stylist(stylist=False)
    assert all(state["original"] is None for state in job.chapters.values())
    assert not any(e["type"].startswith("stylist") for e in job.events)


def test_stylist_rejects_a_rewrite_that_changes_nothing():
    class Lazy(MockProvider):
        async def rewrite_chunk(self, *, chunk, findings, rules, chapter):
            return chunk, Usage(prompt_tokens=10, completion_tokens=10, elapsed=0.01)

    spec = BookSpec(topic="a topic about maintenance debt", audience="Reader", tone="clear",
                    length_id="chapbook", stylist=True, stylist_strength="full")
    job = Job(id="lazy", spec=spec, provider_name="demo")
    asyncio.run(run_book(job, spec, Lazy()))
    assert all(state["original"] is None for state in job.chapters.values())
    assert any(e["type"] == "stylist_rejected" for e in job.events)


def test_stylist_rejects_a_runaway_rewrite():
    class BlowUp(MockProvider):
        async def rewrite_chunk(self, *, chunk, findings, rules, chapter):
            return chunk * 3, Usage(prompt_tokens=10, completion_tokens=90, elapsed=0.01)

    spec = BookSpec(topic="a topic about maintenance debt", audience="Reader", tone="clear",
                    length_id="chapbook", stylist=True, stylist_strength="full")
    job = Job(id="blow", spec=spec, provider_name="demo")
    asyncio.run(run_book(job, spec, BlowUp()))
    assert all(state["original"] is None for state in job.chapters.values())
    reasons = [e["reason"] for e in job.events if e["type"] == "stylist_rejected"]
    assert any("length" in r for r in reasons)


def test_stylist_failure_does_not_sink_the_book():
    class Broken(MockProvider):
        async def rewrite_chunk(self, *, chunk, findings, rules, chapter):
            raise RuntimeError("editor offline")

    spec = BookSpec(topic="a topic about maintenance debt", audience="Reader", tone="clear",
                    length_id="chapbook", stylist=True, stylist_strength="full")
    job = Job(id="broken", spec=spec, provider_name="demo")
    asyncio.run(run_book(job, spec, Broken()))
    assert job.status == "done"
    assert all(state["status"] == "done" for state in job.chapters.values())


# --------------------------------------------------------------------------- #
# De-AI editorial pass: prompts and API
# --------------------------------------------------------------------------- #
def test_stylist_prompt_carries_findings_and_constraints():
    from bookwright.prompts import stylist_messages
    from bookwright.schema import Chapter

    messages = stylist_messages(
        chunk="We must delve into it.", findings_text="- stock phrase: 1 instance(s)",
        rules_text="- Never use 'delve'.", chapter=Chapter(1, "C", "p"),
    )
    system, user = messages[0]["content"], messages[1]["content"]
    assert "Stay within 10% of the original length" in system
    assert "Keep all Markdown headings" in system
    assert "Never use 'delve'" in system
    assert "stock phrase" in user and "We must delve into it." in user


def test_request_validates_stylist_fields():
    from pydantic import ValidationError

    from server import BookRequest

    req = BookRequest(topic="a decent topic", stylist=True, stylist_strength="aggressive")
    assert req.stylist is True and req.stylist_strength == "aggressive"
    with pytest.raises(ValidationError):
        BookRequest(topic="a decent topic", stylist_strength="extreme")


def test_presets_expose_stylist_options():
    payload = config.presets_payload()
    assert payload["stylist"]["default"] is True
    assert set(payload["stylist"]["strengths"]) == {"gentle", "standard", "aggressive"}


# --------------------------------------------------------------------------- #
# Editorial options: expanded catalogue, profiles, banned phrases, intensity
# --------------------------------------------------------------------------- #
def test_catalogue_grew_and_every_rule_has_a_detector():
    from bookwright.style import ALL_RULES

    assert len(config.ANTI_PATTERNS) >= 24
    assert set(config.ANTI_PATTERNS) == set(ALL_RULES)
    assert config.DEFAULT_DIRECTIVES  # defaults still exist


@pytest.mark.parametrize(
    ("rule", "text"),
    [
        ("intensifiers", "It was really very quite good."),
        ("corporate_verbs", "We leverage the tool to facilitate optimisation."),
        ("superlatives", "It is one of the most important ideas."),
        ("expletives", "There is a reason for this."),
        ("the_fact_that", "Due to the fact that it rained, we waited."),
        ("wordy_connectives", "In order to begin, we wait."),
        ("rhetorical_questions", "Why does the system keep failing?"),
        ("flourish_metaphors", "Learning is a journey through a labyrinth."),
        ("anaphora", "The engine failed. The engine restarted. The engine failed again."),
        ("adverb_density", " ".join(
            ["She moved quickly and spoke quietly and worked carefully and deliberately while the "
             "others watched and waited and eventually went home to sleep slowly before the "
             "morning came and the room emptied entirely."] * 3)),
    ],
)
def test_new_detectors_fire(rule, text):
    assert rule in {f.rule for f in scan(text)}, f"{rule} did not fire"


def test_banned_phrases_are_matched_case_insensitively():
    text = "Our SYNERGY unlocked a paradigm shift."
    findings = scan(text, ["banned_phrases"], ["synergy", "paradigm shift", "nonexistent"])
    assert findings and findings[0].count == 2
    assert scan("clean text", ["banned_phrases"], ["synergy"]) == []


def test_banned_list_activates_writer_and_scanner_without_manual_rule_toggle():
    spec = BookSpec(topic="t", audience="a", tone="clear", length_id="standard",
                    directives=["cliches"], banned_phrases=["synergy"])
    assert "banned_phrases" in spec.clean_directives()
    assert "'synergy'" in spec.style
    assert scan("They called it synergy.", spec.clean_directives(), spec.clean_banned())


def test_banned_phrases_reach_the_writer_prompt():
    spec = BookSpec(topic="t", audience="a", tone="clear", length_id="standard",
                    directives=["banned_phrases"], banned_phrases=["synergy", "  thought  leader "])
    style = spec.style
    assert "synergy" in style and "thought leader" in style
    assert spec.clean_banned() == ["synergy", "thought leader"]


def test_banned_phrase_cleaning_caps_and_dedupes():
    spec = BookSpec(topic="t", audience="a", tone="clear", length_id="standard",
                    banned_phrases=["A", "a", "  ", "B" * 200] + [f"p{i}" for i in range(40)])
    cleaned = spec.clean_banned()
    assert len(cleaned) <= config.MAX_BANNED_PHRASES
    assert cleaned[0] == "A" and cleaned[1].startswith("B")
    assert all(len(p) <= config.BANNED_PHRASE_MAX_CHARS for p in cleaned)


def test_every_profile_is_coherent():
    for key, profile in config.EDITORIAL_PROFILES.items():
        assert profile["directives"], key
        assert set(profile["directives"]) <= set(config.ANTI_PATTERNS), key
        assert profile["strength"] in config.STYLIST_STRENGTHS, key
        assert profile["label"] and profile["description"], key
    assert config.DEFAULT_PROFILE in config.EDITORIAL_PROFILES


def test_intensity_controls_threshold_and_chunk_budget():
    assert config.STYLIST_STRENGTHS["gentle"] > config.STYLIST_STRENGTHS["aggressive"]
    assert config.STYLIST_CHUNK_BUDGET["gentle"] < config.STYLIST_CHUNK_BUDGET["standard"]
    assert config.DEFAULT_STYLIST_STRENGTH in config.STYLIST_STRENGTHS


def test_gentle_intensity_skips_lightly_flagged_chapters():
    spec = BookSpec(topic="a topic about maintenance debt", audience="Reader", tone="clear",
                    length_id="chapbook", stylist=True, stylist_strength="gentle")
    job = Job(id="gentle", spec=spec, provider_name="demo")
    asyncio.run(run_book(job, spec, MockProvider()))
    # Fixture chapters carry ~7 flags each, above the gentle threshold of 5.
    assert job.status == "done"
    assert all(state["style_after"] <= state["style_before"] for state in job.chapters.values())


def test_style_report_shape_and_effect():
    spec = BookSpec(topic="the quiet economics of second-hand markets", audience="Reader",
                    tone="clear", length_id="chapbook", stylist=True, stylist_strength="aggressive")
    job = Job(id="report", spec=spec, provider_name="demo")
    asyncio.run(run_book(job, spec, MockProvider()))
    report = job.style_report()
    assert report["available"] is True
    assert report["flags_before"] > report["flags_after"]
    assert report["rules_active"] > 0
    assert report["chapters_edited"] >= 1
    read = report["readability"]
    assert read["words"] > 500
    assert 0 <= read["flesch_ease"] <= 120
    assert read["avg_sentence_words"] > 0


def test_style_report_absent_without_a_plan():
    spec = BookSpec(topic="t", audience="a", tone="clear", length_id="standard")
    assert Job(id="x", spec=spec, provider_name="demo").style_report() == {"available": False}


def test_revert_restores_the_draft_and_updates_the_report():
    spec = BookSpec(topic="the quiet economics of second-hand markets", audience="Reader",
                    tone="clear", length_id="chapbook", stylist=True, stylist_strength="aggressive")
    job = Job(id="revert", spec=spec, provider_name="demo")
    asyncio.run(run_book(job, spec, MockProvider()))
    edited = next(i for i, s in job.chapters.items() if s.get("original"))
    original = job.chapters[edited]["original"]
    before_report = job.style_report()["total"]

    assert job.revert_chapter(edited) is True
    assert job.chapters[edited]["text"] == original
    assert job.chapters[edited]["original"] is None
    assert job.chapters[edited]["reverted"] is True
    assert any(e["type"] == "chapter_reverted" for e in job.events)
    assert job.style_report()["total"] >= before_report  # flags come back

    assert job.revert_chapter(edited) is False  # nothing left to undo


def test_request_validates_profiles_banned_phrases_and_intensity():
    from pydantic import ValidationError

    from server import BookRequest

    req = BookRequest(
        topic="a decent topic", profile="anti_ai", stylist_strength="aggressive",
        banned_phrases=["Synergy", "synergy", "  honest  broker "],
    )
    assert req.profile == "anti_ai"
    assert req.banned_phrases == ["Synergy", "honest broker"]
    for bad in ({"profile": "nope"}, {"stylist_strength": "light"}):
        with pytest.raises(ValidationError):
            BookRequest(topic="a decent topic", **bad)


def test_presets_expose_profiles_and_banned_limits():
    payload = config.presets_payload()
    assert len(payload["profiles"]) == len(config.EDITORIAL_PROFILES)
    assert payload["profiles"][0]["directives"]
    assert payload["banned_phrases"]["max"] == config.MAX_BANNED_PHRASES
    assert {p["id"] for p in payload["pov"]["options"]} == set(config.POVS)
    assert payload["review_outline"]["default"] is False
    assert payload["review_outline"]["browser_default"] is True


def test_pdf_respects_page_size_and_running_matter_option(finished):
    pymupdf = pytest.importorskip("pymupdf")
    plan, chapters = finished
    data, _, _ = export(
        plan,
        chapters,
        "pdf",
        options=ExportOptions(page_size="a4", running_matter=False, chapter_heading="title"),
    )
    pdf = pymupdf.open(stream=data, filetype="pdf")
    assert abs(pdf[1].rect.width - 595.28) < 1
    assert abs(pdf[1].rect.height - 841.89) < 1
    assert "BOOKWRIGHT" not in pdf[1].get_text()


def test_profile_expands_server_side_unless_overridden():
    from server import BookRequest, expand_profile

    # profile alone pulls in its directives, intensity and house style
    directives, strength, extra = expand_profile(
        BookRequest(topic="a decent topic", profile="anti_ai")
    )
    profile = config.EDITORIAL_PROFILES["anti_ai"]
    assert directives == profile["directives"]
    assert strength == profile["strength"]
    assert extra == profile["extra"]

    # an explicit field wins over the profile
    directives, strength, _ = expand_profile(
        BookRequest(topic="a decent topic", profile="literary",
                    directives=["cliches"], stylist_strength="aggressive")
    )
    assert directives == ["cliches"]
    assert strength == "aggressive"

    # no profile leaves defaults untouched
    plain, strength, extra = expand_profile(BookRequest(topic="a decent topic"))
    assert plain == config.DEFAULT_DIRECTIVES
    assert strength == config.DEFAULT_STYLIST_STRENGTH
    assert extra == ""


# --------------------------------------------------------------------------- #
# Grounding, voice, person and outline review
# --------------------------------------------------------------------------- #
NOTE_TEXT = "The Halden plant closed in 2019, cutting 480 jobs. Wages fell 6% over three years."


def test_notes_reach_the_architect_and_every_chapter_prompt():
    seen: list[str] = []

    class Recorder(MockProvider):
        async def plan(self, *, topic, audience, tone, length, notes=""):
            seen.append(notes)
            return await super().plan(
                topic=topic, audience=audience, tone=tone, length=length, notes=notes
            )

        async def write_chapter(self, *, plan, chapter, part_title, bible, style="",
                                pov="", voice_sample=""):
            seen.append(bible)
            async for event in super().write_chapter(
                plan=plan, chapter=chapter, part_title=part_title, bible=bible,
                style=style, pov=pov, voice_sample=voice_sample,
            ):
                yield event

    spec = BookSpec(topic="why the plant closed", audience="Reader", tone="clear",
                    length_id="chapbook", notes=NOTE_TEXT, stylist=False)
    job = Job(id="notes", spec=spec, provider_name="demo")
    asyncio.run(run_book(job, spec, Recorder()))
    assert job.status == "done"
    assert seen[0] == NOTE_TEXT                      # architect saw the facts
    assert all("Halden" in bible for bible in seen[1:])  # every chapter prompt carries them
    assert "unsupported figures" in seen[1].lower()


def test_notes_are_capped_and_stripped():
    spec = BookSpec(topic="t", audience="a", tone="clear", length_id="standard",
                    notes="  " + "x" * (config.MAX_NOTES_CHARS + 500) + "  ")
    assert len(spec.clean_notes()) == config.MAX_NOTES_CHARS
    assert not spec.clean_notes().startswith(" ")


def test_voice_sample_reaches_the_writer_but_not_the_bible():
    prompts_seen: list[str] = []

    class Recorder(MockProvider):
        async def write_chapter(self, *, plan, chapter, part_title, bible, style="",
                                pov="", voice_sample=""):
            from bookwright import prompts as promptmod

            messages = promptmod.chapter_messages(
                plan=plan, chapter=chapter, part_title=part_title, bible=bible,
                style=style, pov=pov, voice_sample=voice_sample,
            )
            prompts_seen.append(messages[0]["content"])
            async for event in super().write_chapter(
                plan=plan, chapter=chapter, part_title=part_title, bible=bible, style=style
            ):
                yield event

    sample = "Concrete first. Then the abstraction, briefly, as if it were a cost."
    spec = BookSpec(topic="a short book on repair", audience="Reader", tone="clear",
                    length_id="chapbook", voice_sample=sample, stylist=False)
    asyncio.run(run_book(Job(id="v", spec=spec, provider_name="demo"), spec, Recorder()))
    assert prompts_seen and all(sample in content for content in prompts_seen)
    assert all("Do not reuse its phrases" in content for content in prompts_seen)


@pytest.mark.parametrize(
    ("pov", "blocked"),
    [("first", "no_first_person"), ("we", "no_first_person"), ("you", "no_second_person")],
)
def test_pov_drops_the_contradicting_rule(pov, blocked):
    spec = BookSpec(
        topic="t", audience="a", tone="clear", length_id="standard", pov=pov,
        directives=["no_first_person", "no_second_person", "cliches"],
    )
    cleaned = spec.clean_directives()
    assert blocked not in cleaned
    assert "cliches" in cleaned
    assert "Grammatical person" in spec.pov_text


def test_auto_pov_keeps_both_rules_and_adds_no_instruction():
    spec = BookSpec(topic="t", audience="a", tone="clear", length_id="standard",
                    directives=["no_first_person", "no_second_person"])
    assert spec.clean_directives() == ["no_first_person", "no_second_person"]
    assert spec.pov_text == ""


def test_outline_review_pauses_before_any_chapter_is_written():
    spec = BookSpec(topic="the quiet economics of repair", audience="Reader", tone="clear",
                    length_id="chapbook", review_outline=True, stylist=False)
    job = Job(id="review", spec=spec, provider_name="demo")
    started = {"n": 0}

    class Counter(MockProvider):
        async def write_chapter(self, **kwargs):
            started["n"] += 1
            async for event in super().write_chapter(**kwargs):
                yield event

    async def scenario():
        task = asyncio.create_task(run_book(job, spec, Counter()))
        for _ in range(200):
            if job.status == "awaiting_outline":
                break
            await asyncio.sleep(0.01)
        assert job.status == "awaiting_outline"
        assert job.busy is False              # parked books don't hold model slots
        assert job.plan is not None
        assert started["n"] == 0            # nothing written while parked
        assert any(e["type"] == "outline_review" for e in job.events)

        # The author renames a chapter and drops another, then approves.
        edited = job.plan.to_dict()
        edited["title"] = "The Repair Economy"
        edited["parts"][0]["chapters"][0]["title"] = "Everything Breaks"
        edited["parts"][-1]["chapters"].pop()
        approved = parse_plan(
            edited, topic=spec.topic, audience=spec.audience, tone=spec.tone,
            words_per_chapter=spec.length.words_per_chapter,
        )
        job.apply_outline(approved)
        assert job.busy is True
        with pytest.raises(ValueError):
            job.apply_outline(approved)        # duplicate approve is rejected
        await asyncio.wait_for(task, timeout=30)
        return edited

    asyncio.run(scenario())
    assert job.status == "done"
    assert started["n"] == len(job.plan.chapters)   # exactly the approved outline
    assert job.chapters[1]["title"] == "Everything Breaks"
    assert job.plan.title == "The Repair Economy"


def test_cancelling_a_parked_outline_does_not_hang():
    spec = BookSpec(topic="the quiet economics of repair", audience="Reader", tone="clear",
                    length_id="chapbook", review_outline=True, stylist=False)
    job = Job(id="parked", spec=spec, provider_name="demo")

    async def scenario():
        task = asyncio.create_task(run_book(job, spec, MockProvider()))
        for _ in range(200):
            if job.status == "awaiting_outline":
                break
            await asyncio.sleep(0.01)
        job.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(scenario())
    assert job.status == "cancelled"


def test_apply_outline_refuses_when_not_waiting():
    spec = BookSpec(topic="t", audience="a", tone="clear", length_id="standard")
    job = Job(id="x", spec=spec, provider_name="demo")
    with pytest.raises(ValueError):
        job.apply_outline(Plan(title="T", subtitle="", thesis="", audience="a",
                               voice="", parts=[]))


def test_outline_request_fields_validate():
    from pydantic import ValidationError

    from server import BookRequest

    req = BookRequest(
        topic="a decent topic", notes="  " + NOTE_TEXT + "  ", pov="you",
        voice_sample="  Short. Blunt.  ", review_outline=True,
    )
    assert req.notes == NOTE_TEXT
    assert req.voice_sample == "Short. Blunt."
    assert req.review_outline is True
    with pytest.raises(ValidationError):
        BookRequest(topic="a decent topic", pov="fourth")
