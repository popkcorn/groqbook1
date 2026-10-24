"""Offline provider used for demos, tests and previews.

It is not an LLM and does not pretend to be: it composes deterministic prose
from hand-written frames so the whole pipeline — planning, streaming,
continuity, export — can be exercised end to end without a key or a network.
The text is templated on purpose; treat it as fixtures, not as writing.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import AsyncIterator

from . import config
from .llm import LLMError, Usage
from .schema import Chapter, Plan

CHAPTER_TITLES = [
    "The Shape of the Problem",
    "First Principles, Cheaply Bought",
    "How It Actually Works",
    "The Comfortable Mistake",
    "Working With Constraints",
    "Systems, Not Resolutions",
    "Measuring What Matters",
    "Where It Breaks",
    "The Long Horizon",
    "A Practice, Not a Possession",
    "The Cost of Fluency",
    "What the Experts Skip",
]

CHAPTER_PURPOSES = [
    "Establish the central claim and give the reader a first working model they can test this week.",
    "Show why the common framing fails, using the cost of a single repeated mistake.",
    "Walk through the mechanism step by step, with one worked example carried to the end.",
    "Interrogate the strongest objection to the book's argument and answer it honestly.",
    "Move from individual practice to the systems and constraints that shape it.",
    "Define what progress actually looks like when it cannot be seen week to week.",
]

BEAT_SEEDS = [
    "Name the specific failure mode: what people do instead, and what it costs them.",
    "Work one concrete example from a first attempt through a correction.",
    "State the counter-intuitive claim and defend it with a mechanism, not an anecdote.",
    "Offer a diagnostic the reader can run on their own situation in ten minutes.",
    "Separate what is trainable from what is merely desirable.",
]

PART_TITLES = ["Orientation", "Practice", "Consequence", "Coda"]

OPENERS = [
    "{t} rarely announces itself. It arrives as a small decision, repeated until it begins to look like character.",
    "Most writing about {tl} opens with a definition, which is precisely where the trouble starts.",
    "There is a cheap version of {tl} that fits on a single page, and an expensive version that takes years. The distance between the two is what this book is about.",
    "Ask ten people what {tl} means and you will collect ten confident answers, most of them borrowed.",
    "The first thing {tl} asks of you is attention, and attention is the one resource you cannot outsource.",
    "A reliable way to lose an afternoon is to argue about the definition of {tl}. A better way to spend it is to watch what people do instead.",
    "Every serious account of {tl} eventually contradicts itself. The contradiction is usually the interesting part.",
    "You can learn {tl} the way you learn a language: badly, in public, with a patient listener. It is still faster than the alternative.",
]

BODY = [
    "Consider the difference between knowing about {tl} and being able to use it under pressure. The first is a collection of facts; the second is a set of reflexes trained on real constraints.",
    "The pattern repeats across contexts. Someone learns the vocabulary, performs it competently, then meets a case the vocabulary cannot describe.",
    "What looks like talent is usually a smaller loop, run more often, with better feedback and less ceremony.",
    "Notice what is missing from the usual account: time, friction, and the cost of being wrong in front of someone whose opinion matters.",
    "This is not a moral failing. It is what happens when a practice borrows the language of a product.",
    "The useful question is not whether you are good at {tl}, but which part of it you are practising this month.",
    "A concrete example helps. Imagine someone who has just finished a first pass at {tl} and feels, wrongly, that the subject is closed.",
    "None of this requires talent in the romantic sense. It requires a schedule, a way to notice error, and the willingness to be seen being bad at something.",
    "Try to state the mechanism in one sentence without using the word 'just'. If you cannot, you have found the next thing to study.",
    "The strongest version of the idea survives contact with a stranger who does not share your assumptions.",
    "Constraints do most of the work that motivation is credited with. Remove them and even a good habit dissolves.",
    "There is a difference between a practice that is simple and one that is easy. Confusing the two is the most common expensive error.",
]

BEAT_INTROS = [
    "A recurring failure mode is easier to see once its costs accumulate.",
    "The example becomes useful when the first attempt and its correction are both visible.",
    "The less obvious claim is that the mechanism matters more than the anecdote.",
    "A short diagnostic can show whether this pattern applies to a particular case.",
    "The distinction is between a capacity that can be practiced and a preference that can only be expressed.",
]

CLOSERS = [
    "Which leaves a question the next chapter has to answer.",
    "Hold on to that; it becomes the hinge of everything that follows.",
    "None of this is settled yet, and that is the point.",
    "The next step is smaller than it sounds, and harder than it looks.",
    "What follows is an attempt to take that seriously.",
]

PULL_QUOTES = [
    "The opposite of {tl} is not ignorance. It is fluency without practice.",
    "You do not rise to the level of your intentions. You fall to the level of your feedback.",
    "Anything worth doing badly is worth doing badly on a schedule.",
]

BULLETS = [
    "A short loop you can run weekly, not a transformation you announce once.",
    "A signal that tells you the loop worked, and one that tells you it did not.",
    "A named constraint — time, money, attention — that keeps the practice honest.",
    "Someone who will tell you the truth about the output.",
]


def _seed(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def _sentence_case(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _fill(template: str, topic: str, beat: str = "") -> str:
    return template.format(
        t=topic,
        tl=topic[:1].lower() + topic[1:],
        beat=beat,
        beat_l=beat[:1].lower() + beat[1:] if beat else beat,
    )


def _beats_for(index: int, topic: str) -> list[str]:
    start = _seed(topic, index) % len(BEAT_SEEDS)
    rotated = [BEAT_SEEDS[(start + i) % len(BEAT_SEEDS)] for i in range(3)]
    return [f"{b} Use {topic} as the running case." for b in rotated]


def compose_chapter(topic: str, chapter: Chapter, target_words: int) -> str:
    """Build chapter markdown long enough to fill `target_words`."""
    blocks: list[str] = []
    rnd = _seed(topic, chapter.index, chapter.title)

    opener = OPENERS[rnd % len(OPENERS)]
    second = BODY[(rnd // 7) % len(BODY)]
    third = BODY[(rnd // 13 + 2) % len(BODY)]
    blocks.append(" ".join(_fill(s, topic) for s in (opener, second, third)))

    beats = chapter.beats or ["State the claim plainly, then test it against a case."]
    word_count = len(blocks[0].split())
    b_i = 0
    while word_count < target_words * 0.92:
        beat = beats[b_i % len(beats)]
        heading = _heading_for(beat, b_i)
        blocks.append(f"## {heading}")
        intro = _fill(BEAT_INTROS[(rnd + b_i) % len(BEAT_INTROS)], topic, beat)
        paras = [intro]
        for k in range(2):
            picks = [
                BODY[(rnd + b_i * 3 + k * 5 + off) % len(BODY)]
                for off in range(3)
            ]
            paras.append(" ".join(_fill(p, topic) for p in picks))
        if b_i == 1 and "benefits" not in "".join(blocks).lower():
            paras.append(
                "What the practice asks for, concretely:\n\n"
                + "\n".join(f"- {item}" for item in BULLETS)
            )
        if b_i == 2:
            paras.append("> " + _fill(PULL_QUOTES[(rnd + b_i) % len(PULL_QUOTES)], topic))
        if b_i == 0:
            # Deliberate machine-flavoured seam, so the editorial pass has
            # something real to fix in demo mode. Live models produce their own.
            paras.append(
                f"It is worth noting that {topic} is not just a technique — it is a way of "
                "seeing. In today's fast-paced world, we must delve into the complexities of "
                "the landscape, arguably unlocking the secrets of a tapestry that is, "
                "at the end of the day, clear, concise, and compelling."
            )
        blocks.extend(paras)
        word_count += sum(len(p.split()) for p in paras)
        b_i += 1
        if b_i > 8:
            break

    blocks.append(_fill(CLOSERS[(rnd + b_i) % len(CLOSERS)], topic))
    return "\n\n".join(blocks).strip()


def _heading_for(beat: str, i: int) -> str:
    """Use a reader-facing heading instead of repeating the beat instruction."""
    cues = (
        ("failure mode", "Where the Pattern Breaks"),
        ("concrete example", "A Case in Practice"),
        ("counter-intuitive", "The Unexpected Claim"),
        ("diagnostic", "A Useful Diagnostic"),
        ("trainable", "What Practice Can Change"),
    )
    lowered = beat.casefold()
    return next((heading for cue, heading in cues if cue in lowered), f"Point {i + 1}")


def _tidy(text: str) -> str:
    """Repair the grammar a regex leaves behind."""
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"(?<=[.!?])\s+([a-z])", lambda m: " " + m.group(1).upper(), text)
    text = re.sub(r"(?<=[a-z])\s*\.\s*\.", ".", text)
    text = re.sub(r"\bNote that The\b", "The", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class MockProvider:
    name = "demo"
    architect_model = "demo/architect"
    writer_model = "demo/writer"
    editor_model = "demo/editor"

    async def plan(
        self, *, topic: str, audience: str, tone: str, length, notes: str = ""
    ) -> tuple[dict, Usage]:
        # Notes shape the real architect's plan; the fixture plan is fixed.
        await asyncio.sleep(0.35)
        per_part = max(1, round(length.chapters / length.parts))
        chapters: list[dict] = []
        for i in range(length.chapters):
            title = CHAPTER_TITLES[i % len(CHAPTER_TITLES)]
            chapters.append(
                {
                    "title": title,
                    "purpose": CHAPTER_PURPOSES[i % len(CHAPTER_PURPOSES)],
                    "beats": _beats_for(i, topic),
                    "terms": [w.strip(".,:;").lower() for w in topic.split()[:2]],
                }
            )
        parts = []
        for p in range(length.parts):
            slice_ = chapters[p * per_part : (p + 1) * per_part]
            if not slice_:
                continue
            parts.append(
                {
                    "title": PART_TITLES[p % len(PART_TITLES)],
                    "summary": f"Part {p + 1} of the argument about {topic}.",
                    "chapters": slice_,
                }
            )
        if chapters[len(chapters) - 1:] and sum(len(p["chapters"]) for p in parts) < len(chapters):
            parts[-1]["chapters"].extend(chapters[sum(len(p["chapters"]) for p in parts):])

        plan = {
            "title": _sentence_case(topic),
            "subtitle": f"A short, practical book about {topic[:1].lower() + topic[1:]}",
            "thesis": _sentence_case(
                f"{topic} is a practice built under constraint, not a fact to be acquired — "
                "and the constraints are what make it learnable."
            ),
            "voice": (
                "Plain and direct, with a dry edge. Short sentences after long ones. "
                "Concrete examples before abstractions. Never flatter the reader."
            ),
            "parts": parts,
        }
        words = len(str(plan).split())
        return plan, Usage(prompt_tokens=180, completion_tokens=words, elapsed=0.35)

    async def write_chapter(
        self, *, plan: Plan, chapter: Chapter, part_title: str, bible: str, style: str = "",
        pov: str = "", voice_sample: str = "", unit_label: str = "chapter",
        part_label: str = "part",
    ) -> AsyncIterator[dict]:
        text = compose_chapter(plan.title, chapter, chapter.words)
        # Demo prose is fixture text, but honour the visible directives so the
        # style controls demonstrate an observable effect without a key.
        if "at most one em dash" in style:
            text = text.replace(" — ", ", ").replace("—", ",")
        if "Do not address the reader as 'you'" in style:
            text = text.replace(" you ", " one ").replace(" your ", " one's ")
        started = time.perf_counter()
        words = text.split(" ")
        chunk: list[str] = []
        for i, word in enumerate(words):
            chunk.append(word)
            if len(chunk) >= 4 or i == len(words) - 1:
                piece = " ".join(chunk) + (" " if i < len(words) - 1 else "")
                chunk = []
                yield {"type": "delta", "text": piece}
                if config.MOCK_CHUNK_DELAY:
                    await asyncio.sleep(config.MOCK_CHUNK_DELAY)
        elapsed = max(time.perf_counter() - started, 0.001)
        yield {
            "type": "usage",
            "usage": Usage(
                prompt_tokens=len(bible.split()) + 120,
                completion_tokens=len(words),
                elapsed=elapsed,
            ),
        }

    async def regenerate_chapter(self, **kwargs):
        raise LLMError("command-based chapter regeneration requires live Groq mode; demo prose is fixed")

    async def rewrite_chunk(
        self, *, chunk: str, findings: str, rules: str, chapter: Chapter
    ) -> tuple[str, Usage]:
        """Apply the active style rules mechanically.

        Not an editor — a filter. It exists so the de-AI stage is observable
        offline and so tests can assert that flags measurably fall.
        """
        text = chunk

        # The injected seam paragraph is replaced wholesale: a regex filter can
        # strip a phrase but cannot repair the grammar around it.
        seam = re.compile(
            r"It is worth noting that.*?clear, concise, and compelling\.",
            re.IGNORECASE | re.DOTALL,
        )
        if seam.search(text):
            text = seam.sub(
                f"The argument is not decoration. {chapter.title} is a way of seeing, "
                "and the seeing takes practice.",
                text,
            )

        replacements = [
            (r"\bdelve[sd]? into\b", "examine"),
            (r"\bdelve[sd]?\b", "examine"),
            (r"\bIn today'?s fast-paced world,?\s*", ""),
            (r"\bat the end of the day,?\s*", ""),
            (r"\bIt is worth noting that\b", "Note that"),
            (r"\bit is worth noting\b", "notably"),
            (r"\bunlocking the secrets of\b", "mapping"),
            (r"\ba tapestry that is\b", "a picture"),
            (r"\bthe landscape\b", "the situation"),
            (r"\barguably\b", ""),
            (r"\bIn this chapter,?\s*", ""),
            (r"\bAs we have seen,?\s*", ""),
            (r"\bclear, concise, and compelling\b", "clear"),
            (r"\bnot just\b([^.!?]{0,60}?),?\s*but\b", r"not merely\1, but"),
        ]
        for pattern, replacement in replacements:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        if "at most one em dash" in rules:
            text = text.replace(" — ", ", ").replace("—", ", ")
        if "Do not address the reader as 'you'" in rules:
            text = re.sub(r"\byou\b", "one", text, flags=re.IGNORECASE)
            text = re.sub(r"\byour\b", "one's", text, flags=re.IGNORECASE)

        text = _tidy(text)
        await asyncio.sleep(0.05)
        usage = Usage(
            prompt_tokens=len(chunk.split()) + 150,
            completion_tokens=len(text.split()),
            elapsed=0.05,
        )
        return text, usage

    async def summarize(self, *, chapter: Chapter, text: str) -> tuple[dict, Usage]:
        await asyncio.sleep(0.12)
        plain = re.sub(r"[#>*`-]", "", text)
        sentences = re.split(r"(?<=[.!?])\s+", plain.strip())
        summary = " ".join(sentences[:2]).strip()[:400] or f"The chapter {chapter.title}."
        return {"summary": summary, "terms": chapter.terms[:3]}, Usage(
            prompt_tokens=140, completion_tokens=40, elapsed=0.12
        )
