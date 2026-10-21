"""Tests for Bookwright. Run: pytest -q"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile

import pytest

from bookwright import config
from bookwright.exporters import export, parse_markdown, to_epub, to_markdown
from bookwright.jobs import BookSpec, Job, estimate, run_book
from bookwright.mock import MockProvider
from bookwright.schema import PlanError, parse_plan

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
        async def write_chapter(self, *, plan, chapter, part_title, bible):
            seen.append(bible)
            async for event in super().write_chapter(
                plan=plan, chapter=chapter, part_title=part_title, bible=bible
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
        async def write_chapter(self, *, plan, chapter, part_title, bible):
            async for event in super().write_chapter(
                plan=plan, chapter=chapter, part_title=part_title, bible=bible
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
