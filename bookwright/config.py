"""Runtime configuration: model routing, length presets, hard limits.

Everything that could drift — model IDs above all — lives here and can be
overridden with environment variables instead of being edited in five places.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from .env import load_env

# Load `.env` before reading anything below, so `cp example.env .env` is
# enough to configure the app. Real environment variables take precedence.
load_env()

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


def writer_tokens_for(words: int) -> int:
    """Token budget for one chapter call.

    Roughly 1.6 tokens per word, plus headroom, so a chapter finishes in a single
    call instead of hitting the ceiling and needing continuation. Bounded so a
    typo in the target length cannot request a runaway completion.
    """
    needed = int(words * 1.6) + 300
    floor = min(max(WRITER_MAX_TOKENS, 2_200), 8_000)
    return max(2_200, min(max(needed, floor), 8_000))


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
        LengthPreset("deep", "Deep dive", 8, 900, 3, "8 chapters · ~7,200 words"),
        LengthPreset("folio", "Folio", 10, 1_000, 4, "10 chapters · ~10,000 words"),
        LengthPreset("novella", "Novella", 12, 1_200, 4, "12 chapters · ~14,400 words"),
        LengthPreset("tome", "Tome", 14, 1_400, 5, "14 chapters · ~19,600 words"),
        LengthPreset("epic", "Epic", 20, 1_500, 5, "20 chapters · ~30,000 words"),
        LengthPreset("custom", "Custom", 10, 1_000, 4, "set chapters and words yourself"),
    )
}
DEFAULT_LENGTH = "standard"

# Custom-length bounds. The writer ceiling scales with the target, so the
# upper bound is set by rate limits and patience rather than by a token cap.
CHAPTERS_RANGE = (2, 20)
WORDS_PER_CHAPTER_RANGE = (250, 2_000)
MAX_PARTS = 6

TONES = {
    "clear": "clear and practical — plain sentences, concrete examples, no throat-clearing",
    "narrative": "narrative and vivid — scenes, specific people, forward motion",
    "rigorous": "rigorous and academic — precise claims, named sources, careful qualification",
    "playful": "witty and playful — dry humour, surprising analogies, light touch",
    "journalistic": "journalistic — inverted pyramid, attributed claims, active verbs, no adjectives doing the work of facts",
    "conversational": "conversational — spoken rhythm, contractions, direct address, short paragraphs",
    "lyrical": "lyrical — concrete imagery, cadence, attention to sound, metaphor used sparingly and precisely",
    "satirical": "satirical — deadpan, straight-faced absurdity, institutions described in their own jargon",
    "socratic": "socratic — questions drive the argument, each answer opens a sharper question",
    "technical": "technical — precise definitions, numbered conditions, worked examples, no motivational filler",
    "editorial": "editorial — a stated position, argued in public, conceding what is true on the other side",
    "minimal": "minimal — short declarative sentences, no ornamental adjectives, white space on the page",
}
DEFAULT_TONE = "clear"

# Voice ids that imply a stance; surfaced in the UI with a description.
TONE_GROUPS = {
    "plain": ["clear", "conversational", "minimal"],
    "literary": ["narrative", "lyrical", "playful"],
    "argument": ["rigorous", "journalistic", "editorial", "socratic"],
    "specialist": ["technical", "satirical"],
}

# --------------------------------------------------------------------------- #
# Anti-pattern controls
# --------------------------------------------------------------------------- #
# Patterns that make prose read as machine-generated. Each id maps to a label
# (shown in the UI) and an instruction injected into the writer prompt. Keeping
# them as data rather than prose means the UI, the API and the tests all agree.
ANTI_PATTERNS: dict[str, tuple[str, str]] = {
    "cliches": (
        "Stock phrases",
        "Never use 'delve', 'tapestry', 'landscape', 'testament to', 'navigate the complexities', "
        "'in today's fast-paced world', 'it is worth noting', 'at the end of the day', "
        "'unlock the secrets', or 'a beacon of'. Use the plain word instead.",
    ),
    "not_just": (
        "'Not just X, but Y'",
        "Do not use the 'it is not just X, it is Y' scaffold, nor 'not only ... but also'. "
        "State the thing directly.",
    ),
    "rule_of_three": (
        "Rule-of-three lists",
        "Do not pad with three-item lists for rhythm ('clear, concise, and compelling'). "
        "List something only if the number is true.",
    ),
    "em_dash": (
        "Em-dash tics",
        "Use at most one em dash per chapter. Prefer a full stop, a comma, or a colon.",
    ),
    "rhetorical_openers": (
        "'In a world where…' openers",
        "Do not open a chapter or section with 'In a world where', 'Imagine a world', "
        "'Picture this', or a rhetorical question.",
    ),
    "hedging": (
        "Hedging",
        "Avoid 'arguably', 'it can be argued', 'some might say', 'it goes without saying', "
        "'in some sense'. Make the claim or drop it.",
    ),
    "summarising": (
        "Chapter recaps",
        "Never write 'In this chapter we will', 'As we have seen', or a closing summary of "
        "what the reader just read. Move forward instead.",
    ),
    "uniform_rhythm": (
        "Uniform rhythm",
        "Vary sentence and paragraph length deliberately. Never write three consecutive "
        "paragraphs of the same length, and never three sentences of the same shape.",
    ),
    "grandiosity": (
        "Grand claims",
        "No sweeping statements about humanity, civilisation, the universe, or the future of "
        "everything. Keep claims inside what the chapter can support.",
    ),
    "question_headers": (
        "Question headings",
        "Section headings are statements or concrete nouns, never questions.",
    ),
    "listicles": (
        "Bullet-point prose",
        "Write flowing paragraphs. Use bullets only for a genuine enumeration of discrete "
        "items, and never more than one list per chapter.",
    ),
    "no_second_person": (
        "No 'you'",
        "Do not address the reader as 'you'. Write about the subject, not at the reader.",
    ),
    "no_first_person": (
        "No 'I'",
        "Do not use the first person singular. No anecdotes about the author.",
    ),
}

# --- De-AI editorial pass ------------------------------------------------- #
# After a chapter is drafted, scan it for the active rules and rewrite only the
# offending passages. "light" touches chunks with a handful of flags; "full"
# rewrites any chunk with at least one.
STYLIST_DEFAULT = True
STYLIST_STRENGTHS = {"light": 3, "full": 1}  # strength -> minimum flags to act
DEFAULT_STYLIST_STRENGTH = "light"
STYLIST_CHUNK_CHARS = 5_000
STYLIST_MAX_CHUNKS = 8
STYLIST_LENGTH_TOLERANCE = (0.6, 1.6)  # reject rewrites outside this ratio

# Sensible default: the tics that most reliably read as machine-written.
DEFAULT_DIRECTIVES = ["cliches", "not_just", "hedging", "summarising", "uniform_rhythm"]
MAX_DIRECTIVES = len(ANTI_PATTERNS)
MAX_EXTRA_INSTRUCTIONS = 400

AUDIENCES = ["Curious general reader", "Practitioner", "Student", "Executive", "Specialist"]

# --------------------------------------------------------------------------- #
# Limits / safety
# --------------------------------------------------------------------------- #
MIN_TOPIC_CHARS = 8
MAX_TOPIC_CHARS = 240
MAX_CHAPTERS = 20

MAX_BEATS = 6
MAX_BEAT_CHARS = 320
CONCURRENCY = max(1, int(os.environ.get("BOOKWRIGHT_CONCURRENCY", "3")))

# How many completed chapter summaries are carried into the next prompt.
# Wider books need a wider window or the first chapters fall out of context.
BIBLE_SUMMARY_WINDOW = 10
BIBLE_SUMMARY_CHARS = 420

# Mock provider pacing (demo mode). Seconds between streamed chunks.
MOCK_CHUNK_DELAY = float(os.environ.get("BOOKWRIGHT_MOCK_DELAY", "0.018"))


def has_server_key() -> bool:
    return bool(os.environ.get("GROQ_API_KEY", "").strip())


def presets_payload() -> dict:
    return {
        "lengths": [asdict(p) | {"total_words": p.total_words} for p in LENGTHS.values()],
        "tones": [{"id": k, "description": v} for k, v in TONES.items()],
        "tone_groups": TONE_GROUPS,
        "audiences": AUDIENCES,
        "anti_patterns": [
            {"id": key, "label": label, "description": instruction}
            for key, (label, instruction) in ANTI_PATTERNS.items()
        ],
        "default_directives": DEFAULT_DIRECTIVES,
        "stylist": {
            "default": STYLIST_DEFAULT,
            "strengths": list(STYLIST_STRENGTHS),
            "default_strength": DEFAULT_STYLIST_STRENGTH,
        },
        "defaults": {"length": DEFAULT_LENGTH, "tone": DEFAULT_TONE, "audience": AUDIENCES[0]},
        "models": {"architect": ARCHITECT_MODEL, "writer": WRITER_MODEL, "editor": EDITOR_MODEL},
        "limits": {
            "min_topic_chars": MIN_TOPIC_CHARS,
            "max_topic_chars": MAX_TOPIC_CHARS,
            "max_chapters": MAX_CHAPTERS,
            "chapters_range": list(CHAPTERS_RANGE),
            "words_range": list(WORDS_PER_CHAPTER_RANGE),
            "max_extra_instructions": MAX_EXTRA_INSTRUCTIONS,
        },
    }
