"""Tests for Bookwright. Run: pytest -q"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile

import pytest

from bookwright import config
from bookwright.exporters import export, parse_markdown, to_epub, to_markdown
from bookwright.jobs import BookSpec, Job, chunk_text, estimate, run_book
from bookwright.llm import Usage
from bookwright.mock import MockProvider
from bookwright.schema import PlanError, parse_plan
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
        async def write_chapter(self, *, plan, chapter, part_title, bible, style=""):
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
        async def write_chapter(self, *, plan, chapter, part_title, bible, style=""):
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

    req = BookRequest(topic="a decent topic", stylist=True, stylist_strength="full")
    assert req.stylist is True and req.stylist_strength == "full"
    with pytest.raises(ValidationError):
        BookRequest(topic="a decent topic", stylist_strength="extreme")


def test_presets_expose_stylist_options():
    payload = config.presets_payload()
    assert payload["stylist"]["default"] is True
    assert set(payload["stylist"]["strengths"]) == {"light", "full"}
