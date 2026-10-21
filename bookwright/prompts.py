"""Prompt construction.

Kept separate from transport so prompts can be reviewed and tuned without
touching networking code. Every writer prompt receives the book bible — the
thing the original Groqbook lacked (it wrote each chapter from a title alone).
"""

from __future__ import annotations

import json

from . import config
from .schema import Chapter, Plan, plan_skeleton_hint

PLAN_JSON_SHAPE = """{
  "title": "Book title",
  "subtitle": "One line that says what the book does",
  "thesis": "The single argument the book defends, in one or two sentences",
  "voice": "Three sentences of style direction for the writer",
  "parts": [
    {
      "title": "Part title",
      "summary": "What this part accomplishes",
      "chapters": [
        {
          "title": "Chapter title",
          "purpose": "What this chapter must establish and why it comes here",
          "beats": [
            "A concrete beat: the specific idea, example or turn the chapter must contain",
            "Another beat"
          ],
          "terms": ["named concept introduced in this chapter"]
        }
      ]
    }
  ]
}"""


def plan_messages(*, topic: str, audience: str, tone: str, length) -> list[dict]:
    tone_desc = config.TONES.get(tone, tone)
    system = (
        "You are the architect of a short, well-argued nonfiction book. "
        "You design structure: no prose yet, only the plan.\n\n"
        "Return a single JSON object, no markdown fence, matching exactly this shape:\n"
        f"{PLAN_JSON_SHAPE}\n\n"
        "Rules:\n"
        "- Parts are movements of an argument, not a table of contents of topics.\n"
        "- Chapters must not overlap: each one owns a distinct claim or step.\n"
        "- Beats are instructions to a writer, specific enough to draft from. "
        "Never write 'discuss', 'explore', or 'cover' as a beat.\n"
        "- Avoid 'Introduction', 'Conclusion', 'Foreword' and 'Summary' chapters; "
        "the first chapter should open on substance and the last should land the thesis.\n"
        "- Titles are concrete and slightly surprising. No colons in every title.\n"
        "- All strings are plain text: no markdown, no numbering inside titles."
    )
    user = (
        f"Subject: {topic}\n"
        f"Reader: {audience}\n"
        f"Voice: {tone_desc}\n"
        f"Shape: {plan_skeleton_hint(length)}\n\n"
        "Design the plan."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def plan_repair_messages(original: list[dict], error: str) -> list[dict]:
    return [
        *original,
        {
            "role": "user",
            "content": (
                f"That plan failed validation: {error}. "
                "Return the corrected JSON object only — no commentary, no code fence."
            ),
        },
    ]


BIBLE_TEMPLATE = """# Book bible

Title: {title}
Thesis: {thesis}
Reader: {audience}
Voice: {voice}

## Standing glossary
{glossary}

## What has already been written
{summaries}
"""


def build_bible(plan: Plan, summaries: list[tuple[str, str]], terms: list[str]) -> str:
    glossary = "\n".join(f"- {t}" for t in terms) if terms else "- (nothing fixed yet)"
    if summaries:
        window = summaries[-config.BIBLE_SUMMARY_WINDOW :]
        lines = []
        for title, summary in window:
            text = summary.strip()
            if len(text) > config.BIBLE_SUMMARY_CHARS:
                text = text[: config.BIBLE_SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
            lines.append(f"- **{title}** — {text}")
        written = "\n".join(lines)
    else:
        written = "- This is the first chapter. Establish the voice and the central claim."
    return BIBLE_TEMPLATE.format(
        title=plan.title,
        thesis=plan.thesis or "(implied by the plan)",
        audience=plan.audience,
        voice=plan.voice,
        glossary=glossary,
        summaries=written,
    )


def chapter_messages(
    *,
    plan: Plan,
    chapter: Chapter,
    part_title: str,
    bible: str,
    continuation: bool = False,
) -> list[dict]:
    beats = "\n".join(f"{i}. {b}" for i, b in enumerate(chapter.beats, 1)) or "1. (writer's discretion)"
    system = (
        "You are the author of a nonfiction book. You write one chapter at a time, "
        "in Markdown, as finished prose.\n\n"
        "Craft rules:\n"
        "- Open with a concrete image, scene or claim. Never open with "
        "'In this chapter' or a dictionary definition.\n"
        "- Use '## ' for two to four section headings inside the chapter. "
        "Never repeat the chapter title as a heading.\n"
        "- Prefer flowing paragraphs. Use a bulleted list only when enumerating real items.\n"
        "- One idea per paragraph; vary sentence length; no filler transitions.\n"
        "- Refer to earlier chapters naturally, as an author would, not as a summariser.\n"
        "- No meta-commentary about being an AI, no requests to the reader, no citations "
        "you cannot support.\n"
        "- End the chapter on a line that makes the next chapter feel necessary.\n"
        f"- Length: about {chapter.words} words. Stop when the chapter is done.\n"
    )
    user = (
        f"{bible}\n\n"
        f"# Part: {part_title}\n"
        f"# Chapter {chapter.index}: {chapter.title}\n\n"
        f"Job of this chapter: {chapter.purpose}\n\n"
        f"Beats to hit, in order:\n{beats}\n\n"
        "Write the chapter now. Output only the chapter body in Markdown."
    )
    if continuation:
        user += "\n\n(You were cut off. Continue from the exact word you stopped on. Do not repeat text.)"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def summary_messages(*, chapter: Chapter, text: str) -> list[dict]:
    system = (
        "You maintain continuity notes for a book. Return one JSON object:\n"
        '{"summary": "two sentences, past tense, stating what this chapter argued and any '
        'example it used", "terms": ["new named concepts the chapter introduced, max 3"]}\n'
        "No markdown. No commentary."
    )
    # Only the head and tail matter for continuity, and this keeps the call cheap.
    excerpt = text if len(text) <= 5000 else text[:3000] + "\n…\n" + text[-1800:]
    user = f"Chapter: {chapter.title}\n\n{excerpt}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def chapter_heading(chapter: Chapter) -> str:
    return f"Chapter {chapter.index} · {chapter.title}"


def plan_as_json(plan: Plan) -> str:
    return json.dumps(plan.to_dict(), indent=2, ensure_ascii=False)
