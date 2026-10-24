"""The book plan: the contract between Architect and Writer.

A plan is only accepted if it survives `parse_plan`. Models produce
"parseable JSON" happily; they produce *valid structure* less reliably, so
everything is checked, clamped, or rejected with an actionable message that is
fed back to the model for one repair attempt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config


class PlanError(ValueError):
    """Raised when a plan cannot be repaired into something writable."""


@dataclass
class Chapter:
    index: int
    title: str
    purpose: str
    beats: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    words: int = 600

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "title": self.title,
            "purpose": self.purpose,
            "beats": self.beats,
            "terms": self.terms,
            "words": self.words,
        }


@dataclass
class Part:
    index: int
    title: str
    summary: str
    chapters: list[Chapter] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "title": self.title,
            "summary": self.summary,
            "chapters": [c.to_dict() for c in self.chapters],
        }


@dataclass
class Plan:
    title: str
    subtitle: str
    thesis: str
    audience: str
    voice: str
    parts: list[Part] = field(default_factory=list)

    @property
    def chapters(self) -> list[Chapter]:
        return [c for part in self.parts for c in part.chapters]

    def chapter(self, index: int) -> Chapter:
        for c in self.chapters:
            if c.index == index:
                return c
        raise KeyError(index)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "thesis": self.thesis,
            "audience": self.audience,
            "voice": self.voice,
            "parts": [p.to_dict() for p in self.parts],
            "chapter_count": len(self.chapters),
            "total_words": sum(c.words for c in self.chapters),
        }


def _text(value, field_name: str, *, limit: int, required: bool = True) -> str:
    if value is None:
        if required:
            raise PlanError(f"'{field_name}' is missing")
        return ""
    if not isinstance(value, str):
        raise PlanError(f"'{field_name}' must be a string, got {type(value).__name__}")
    value = re.sub(r"\s+", " ", value).strip()
    if required and not value:
        raise PlanError(f"'{field_name}' is empty")
    return value[:limit]


def _str_list(value, field_name: str, *, limit: int, max_items: int) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):  # a lone string is a common model slip
        value = [value]
    if not isinstance(value, list):
        raise PlanError(f"'{field_name}' must be a list of strings")
    out: list[str] = []
    for item in value[:max_items]:
        if not isinstance(item, str):
            continue
        item = re.sub(r"\s+", " ", item).strip()
        if item:
            out.append(item[:limit])
    return out


def parse_plan(
    raw: dict,
    *,
    topic: str,
    audience: str,
    tone: str,
    words_per_chapter: int,
    max_chapters: int = config.MAX_CHAPTERS,
) -> Plan:
    """Validate and normalise a model-authored plan.

    Raises PlanError with a human-readable reason. The pipeline retries once,
    feeding that reason back to the model.
    """
    if not isinstance(raw, dict):
        raise PlanError("the plan must be a JSON object, not a list or scalar")

    title = _text(raw.get("title"), "title", limit=120)
    subtitle = _text(raw.get("subtitle"), "subtitle", limit=180, required=False)
    thesis = _text(raw.get("thesis"), "thesis", limit=600, required=False)
    voice = _text(raw.get("voice"), "voice", limit=400, required=False)

    raw_parts = raw.get("parts") or raw.get("chapters")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise PlanError("'parts' must be a non-empty list")

    parts: list[Part] = []
    index = 0
    for p_i, raw_part in enumerate(raw_parts):
        if not isinstance(raw_part, dict):
            continue
        # Tolerate a flat "parts" array: treat each entry as a part with one chapter.
        raw_chapters = raw_part.get("chapters")
        if not isinstance(raw_chapters, list) or not raw_chapters:
            raw_chapters = [raw_part]
        chapters: list[Chapter] = []
        for raw_ch in raw_chapters:
            if not isinstance(raw_ch, dict):
                continue
            if index >= max_chapters:
                break
            index += 1
            chapters.append(
                Chapter(
                    index=index,
                    title=_text(raw_ch.get("title"), f"parts[{p_i}].chapters[].title", limit=120),
                    purpose=_text(
                        raw_ch.get("purpose") or raw_ch.get("description"),
                        f"parts[{p_i}].chapters[].purpose",
                        limit=config.MAX_BEAT_CHARS,
                    ),
                    beats=_str_list(
                        raw_ch.get("beats"),
                        "beats",
                        limit=config.MAX_BEAT_CHARS,
                        max_items=config.MAX_BEATS,
                    ),
                    terms=_str_list(raw_ch.get("terms"), "terms", limit=60, max_items=8),
                    words=words_per_chapter,
                )
            )
        if chapters:
            parts.append(
                Part(
                    index=len(parts) + 1,
                    title=_text(raw_part.get("title"), f"parts[{p_i}].title", limit=120, required=False)
                    or f"Part {len(parts) + 1}",
                    summary=_text(raw_part.get("summary"), "summary", limit=400, required=False),
                    chapters=chapters,
                )
            )
        if index >= max_chapters:
            break

    if not parts:
        raise PlanError("no usable chapters were found in the plan")
    if len(parts) > config.MAX_PARTS:
        parts = parts[: config.MAX_PARTS]
    if index < 2:
        raise PlanError(f"a book needs at least 2 chapters, the plan had {index}")

    # De-duplicate chapter titles (duplicates break per-chapter keying in the UI).
    seen: set[str] = set()
    for chapter in (c for p in parts for c in p.chapters):
        base = chapter.title
        n = 2
        while chapter.title.lower() in seen:
            chapter.title = f"{base} ({n})"
            n += 1
        seen.add(chapter.title.lower())

    return Plan(
        title=title,
        subtitle=subtitle,
        thesis=thesis,
        audience=audience,
        voice=voice or config.TONES.get(tone, ""),
        parts=parts,
    )


def plan_skeleton_hint(length) -> str:
    """Human-readable shape constraints handed to the architect."""
    unit = getattr(length, "unit_label", "chapter")
    group = getattr(length, "part_label", "part")
    def plural(word: str) -> str:
        return word[:-1] + "ies" if word.endswith("y") and word[-2:-1] not in "aeiou" else word + "s"
    group_name = group if length.parts == 1 else plural(group)
    unit_name = unit if length.chapters == 1 else plural(unit)
    return (
        f"Exactly {length.parts} {group_name} and {length.chapters} {unit_name} total "
        f"(roughly {max(1, round(length.chapters / length.parts))} per {group}). "
        f"Each {unit} is about {length.words_per_chapter:,} words."
    )
