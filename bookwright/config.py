"""Runtime configuration: model routing, length presets, hard limits.

Everything that could drift — model IDs above all — lives here and can be
overridden with environment variables instead of being edited in five places.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
# Groq retires model IDs regularly (llama3-8b-8192 went dark on 2025-08-30).
# Never hardcode a model ID at a call site: route through these constants.
GROQ_BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

ARCHITECT_MODEL = os.environ.get("BOOKWRIGHT_ARCHITECT_MODEL", "openai/gpt-oss-120b")
WRITER_MODEL = os.environ.get("BOOKWRIGHT_WRITER_MODEL", "openai/gpt-oss-20b")
EDITOR_MODEL = os.environ.get("BOOKWRIGHT_EDITOR_MODEL", "openai/gpt-oss-20b")

# Token ceilings per role. The architect returns JSON; writers get enough room
# for a full chapter plus one continuation.
ARCHITECT_MAX_TOKENS = 3000
WRITER_MAX_TOKENS = int(os.environ.get("BOOKWRIGHT_WRITER_MAX_TOKENS", "2200"))
EDITOR_MAX_TOKENS = 400
MAX_CONTINUATIONS = 2


@dataclass(frozen=True)
class LengthPreset:
    id: str
    label: str
    chapters: int
    words_per_chapter: int
    parts: int
    blurb: str

    @property
    def total_words(self) -> int:
        return self.chapters * self.words_per_chapter


LENGTHS: dict[str, LengthPreset] = {
    p.id: p
    for p in (
        LengthPreset("chapbook", "Chapbook", 4, 420, 2, "4 chapters · ~1,700 words"),
        LengthPreset("standard", "Standard", 6, 650, 3, "6 chapters · ~3,900 words"),
        LengthPreset("deep", "Deep dive", 8, 800, 3, "8 chapters · ~6,400 words"),
        LengthPreset("folio", "Folio", 10, 900, 4, "10 chapters · ~9,000 words"),
    )
}
DEFAULT_LENGTH = "standard"

TONES = {
    "clear": "clear and practical — plain sentences, concrete examples, no throat-clearing",
    "narrative": "narrative and vivid — scenes, specific people, forward motion",
    "rigorous": "rigorous and academic — precise claims, named sources, careful hedges",
    "playful": "witty and playful — dry humour, surprising analogies, light touch",
}
DEFAULT_TONE = "clear"

AUDIENCES = ["Curious general reader", "Practitioner", "Student", "Executive", "Specialist"]

# --------------------------------------------------------------------------- #
# Limits / safety
# --------------------------------------------------------------------------- #
MIN_TOPIC_CHARS = 8
MAX_TOPIC_CHARS = 240
MAX_CHAPTERS = 16
MAX_PARTS = 5
MAX_BEATS = 6
MAX_BEAT_CHARS = 320
CONCURRENCY = max(1, int(os.environ.get("BOOKWRIGHT_CONCURRENCY", "3")))

# How many completed chapter summaries are carried into the next prompt.
BIBLE_SUMMARY_WINDOW = 6
BIBLE_SUMMARY_CHARS = 420

# Mock provider pacing (demo mode). Seconds between streamed chunks.
MOCK_CHUNK_DELAY = float(os.environ.get("BOOKWRIGHT_MOCK_DELAY", "0.018"))


def has_server_key() -> bool:
    return bool(os.environ.get("GROQ_API_KEY", "").strip())


def presets_payload() -> dict:
    return {
        "lengths": [asdict(p) | {"total_words": p.total_words} for p in LENGTHS.values()],
        "tones": [{"id": k, "description": v} for k, v in TONES.items()],
        "audiences": AUDIENCES,
        "defaults": {"length": DEFAULT_LENGTH, "tone": DEFAULT_TONE, "audience": AUDIENCES[0]},
        "models": {"architect": ARCHITECT_MODEL, "writer": WRITER_MODEL, "editor": EDITOR_MODEL},
        "limits": {
            "min_topic_chars": MIN_TOPIC_CHARS,
            "max_topic_chars": MAX_TOPIC_CHARS,
            "max_chapters": MAX_CHAPTERS,
        },
    }
