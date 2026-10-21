"""Detection for machine-flavoured prose, and diff utilities.

This is the measuring instrument behind the de-AI editorial pass. It has two
jobs:

1. Tell the writer stage what was actually produced, per active rule, with the
   offending excerpts — so the rewrite prompt points at real sentences instead
   of restating a style guide.
2. Prove afterwards that the rewrite worked. If the flags did not go down, the
   pass is not applied (see `jobs._deai_chapter`), which keeps the stage honest.

Detection is deliberately conservative: a rule only fires on things it can name
exactly. "Uniform rhythm" is the one structural check, and it looks for runs of
near-identical sentence lengths rather than guessing at "boring".
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
WORD = re.compile(r"[A-Za-z']+")
BULLET = re.compile(r"(?m)^\s*[-*]\s+\S")

# Structural checks are computed rather than matched by regex.
STRUCTURAL_RULES = ("uniform_rhythm", "listicles", "anaphora", "adverb_density")

# rule id -> (pattern, human description for the diff/prompt)
DETECTORS: dict[str, tuple[re.Pattern, str]] = {
    "cliches": (
        re.compile(
            r"\b(delve[sd]?|delving|tapestry|testament to|navigate the complexities|"
            r"in today'?s fast-paced|it is worth noting|it'?s worth noting|"
            r"at the end of the day|unlock the secrets|beacon of|dive into|"
            r"in the realm of|a double-edged sword|game-?changer)\b",
            re.IGNORECASE,
        ),
        "stock phrase",
    ),
    "not_just": (
        re.compile(
            r"\bnot just\b[^.!?]{0,90}?\b(but|it'?s|it is)\b|"
            r"\bnot only\b[^.!?]{0,90}?\bbut also\b|"
            r"\bisn'?t just\b[^.!?]{0,90}?\bit'?s\b",
            re.IGNORECASE,
        ),
        "'not just X, but Y' scaffold",
    ),
    "rule_of_three": (
        re.compile(r"\b\w+,\s+\w+,\s+and\s+\w+\b"),
        "three-item list used as rhythm",
    ),
    "em_dash": (re.compile(r"—|\s--\s"), "em dash"),
    "rhetorical_openers": (
        re.compile(
            r"(?m)^\s*(?:In a world where|Imagine a world|Picture this|Have you ever|"
            r"What if I told you)",
            re.IGNORECASE,
        ),
        "formulaic opener",
    ),
    "hedging": (
        re.compile(
            r"\b(arguably|it can be argued|it could be argued|some might say|"
            r"it goes without saying|in some sense|one could argue|"
            r"it is arguably|perhaps the most)\b",
            re.IGNORECASE,
        ),
        "hedge",
    ),
    "summarising": (
        re.compile(
            r"\b(in this chapter|in this section|as we have seen|as we saw|"
            r"in conclusion|to summari[sz]e|to recap|this chapter will)\b",
            re.IGNORECASE,
        ),
        "chapter recap",
    ),
    "grandiosity": (
        re.compile(
            r"\b(humanity as a whole|all of civilization|all of civilisation|"
            r"the entire universe|throughout human history|"
            r"the future of everything|society as a whole)\b",
            re.IGNORECASE,
        ),
        "sweeping claim",
    ),
    "question_headers": (re.compile(r"(?m)^#{2,6}\s+.*\?\s*$"), "question heading"),
    "no_second_person": (
        re.compile(r"\b(you|your|yours|yourself|you're|you've|you'll)\b", re.IGNORECASE),
        "second-person address",
    ),
    "no_first_person": (re.compile(r"\b(I|me|my|mine|myself)\b"), "first-person address"),
    "intensifiers": (
        re.compile(r"\b(very|really|quite|actually|simply|basically|literally|truly)\b", re.IGNORECASE),
        "empty intensifier",
    ),
    "corporate_verbs": (
        re.compile(
            r"\b(leverage[ds]?|leveraging|utili[sz](e|ed|ing)|facilitat(e|ed|ing)|"
            r"optimi[sz](e|ed|ing)|streamlin(e|ed|ing)|synergi[sz](e|ed|ing))\b",
            re.IGNORECASE,
        ),
        "corporate verb",
    ),
    "superlatives": (
        re.compile(
            r"\b(one of the most|the most important thing|the greatest|"
            r"the best way to|the single most)\b",
            re.IGNORECASE,
        ),
        "unearned superlative",
    ),
    "expletives": (
        re.compile(
            r"(?m)(?:^|(?<=[.!?])\s)(?:There (?:is|are|was|were)|It is (?:important|clear|obvious|"
            r"interesting|worth noting))\b",
            re.IGNORECASE,
        ),
        "'there is / it is' filler",
    ),
    "the_fact_that": (
        re.compile(
            r"\b(the fact that|due to the fact that|in spite of the fact that|"
            r"regardless of the fact that)\b",
            re.IGNORECASE,
        ),
        "'the fact that' padding",
    ),
    "wordy_connectives": (
        re.compile(
            r"\b(in order to|prior to|in the event that|a large number of|"
            r"at this point in time|in light of the fact)\b",
            re.IGNORECASE,
        ),
        "wordy connective",
    ),
    "rhetorical_questions": (
        re.compile(r"(?m)^(?!#)[^\n?.]{4,}\?\s*$"),
        "question in the body",
    ),
    "flourish_metaphors": (
        re.compile(
            r"\b(a journey|the journey|a labyrinth|the labyrinth|a symphony|the symphony|"
            r"a double-edged sword|the double-edged sword|a rabbit hole|bridge the gap|"
            r"a dance between|the dance of)\b",
            re.IGNORECASE,
        ),
        "flourish metaphor",
    ),
}


ALL_RULES: tuple[str, ...] = (*DETECTORS, *STRUCTURAL_RULES, "banned_phrases")


@dataclass
class Finding:
    rule: str
    count: int
    description: str
    excerpts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "count": self.count,
            "description": self.description,
            "excerpts": self.excerpts,
        }


def _excerpt(text: str, match: re.Match, radius: int = 46) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    snippet = re.sub(r"\s+", " ", text[start:end]).strip()
    return ("…" if start else "") + snippet + ("…" if end < len(text) else "")


def _uniform_rhythm(text: str) -> Finding | None:
    """Runs of four consecutive sentences with near-identical word counts."""
    counts: list[int] = []
    for sentence in SENTENCE_SPLIT.split(text):
        words = len(WORD.findall(sentence))
        if words >= 8:
            counts.append(words)
    runs = 0
    for i in range(len(counts) - 3):
        window = counts[i : i + 4]
        if min(window) / max(window) >= 0.78:
            runs += 1
    if not runs:
        return None
    return Finding("uniform_rhythm", runs, "run of same-length sentences")


def _listicles(text: str) -> Finding | None:
    bullets = len(BULLET.findall(text))
    if bullets <= 8:
        return None
    return Finding("listicles", bullets, "bullet-point prose")


DETERMINERS = {
    "the", "a", "an", "this", "that", "these", "those", "it", "there", "and", "but",
    "so", "then", "if", "when", "as", "in", "on", "at", "for", "to", "of", "with", "its",
}


def _anaphora(text: str) -> Finding | None:
    """Three consecutive sentences opening with the same word."""
    openers: list[str] = []
    for sentence in SENTENCE_SPLIT.split(text):
        words = [w.lower() for w in WORD.findall(sentence)]
        content = next((w for w in words if w not in DETERMINERS), "")
        openers.append(content)
    runs = 0
    for i in range(len(openers) - 2):
        window = openers[i : i + 3]
        if window[0] and len(set(window)) == 1:
            runs += 1
    if not runs:
        return None
    return Finding("anaphora", runs, "repeated sentence openers")


def _adverb_density(text: str) -> Finding | None:
    words = WORD.findall(text)
    if len(words) < 80:
        return None
    adverbs = [w for w in words if w.lower().endswith("ly") and len(w) > 4]
    if len(adverbs) < 5 or len(adverbs) / len(words) < 0.025:
        return None
    return Finding("adverb_density", len(adverbs), "'-ly' adverbs in bulk")


def scan(
    text: str,
    rules: list[str] | None = None,
    banned_phrases: list[str] | None = None,
) -> list[Finding]:
    """Find violations of `rules` (default: every rule) in `text`."""
    if not text.strip():
        return []
    active = ALL_RULES if rules is None else rules
    findings: list[Finding] = []
    for rule in active:
        if rule == "uniform_rhythm":
            found = _uniform_rhythm(text)
            if found:
                findings.append(found)
            continue
        if rule == "listicles":
            found = _listicles(text)
            if found:
                findings.append(found)
            continue
        if rule == "anaphora":
            found = _anaphora(text)
            if found:
                findings.append(found)
            continue
        if rule == "adverb_density":
            found = _adverb_density(text)
            if found:
                findings.append(found)
            continue
        if rule == "banned_phrases":
            hits = banned_hits(text, banned_phrases or [])
            if hits:
                excerpts = []
                lowered = text.lower()
                for phrase in hits[:3]:
                    index = lowered.find(phrase.lower())
                    if index >= 0:
                        start = max(0, index - 46)
                        end = min(len(text), index + len(phrase) + 46)
                        excerpts.append(("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else ""))
                findings.append(Finding("banned_phrases", len(hits), "banned phrase", excerpts))
            continue
        detector = DETECTORS.get(rule)
        if detector is None:
            continue
        pattern, description = detector
        matches = list(pattern.finditer(text))
        if not matches:
            continue
        excerpts: list[str] = []
        for match in matches[:3]:
            snippet = _excerpt(text, match)
            if snippet not in excerpts:
                excerpts.append(snippet)
        findings.append(Finding(rule, len(matches), description, excerpts))

    findings.sort(key=lambda f: f.count, reverse=True)
    return findings


def banned_hits(text: str, phrases: list[str]) -> list[str]:
    """Case-insensitive occurrences of the author's own banned phrases."""
    lowered = text.lower()
    return [phrase for phrase in phrases if phrase and phrase.lower() in lowered]


# --------------------------------------------------------------------------- #
# Readability — reported alongside the pattern residue
# --------------------------------------------------------------------------- #
VOWEL_GROUP = re.compile(r"[aeiouy]+", re.IGNORECASE)


def _syllables(word: str) -> int:
    count = len(VOWEL_GROUP.findall(word))
    if word.lower().endswith("e") and count > 1:
        count -= 1
    return max(1, count)


def readability(text: str) -> dict:
    """Flesch reading ease, grade level and the raw counts behind them."""
    sentences = [s for s in SENTENCE_SPLIT.split(text) if s.strip()]
    words = WORD.findall(text)
    paragraphs = [p for p in PARAGRAPH.split(text) if p.strip()]
    if not sentences or not words:
        return {
            "sentences": 0, "words": 0, "paragraphs": 0,
            "avg_sentence_words": 0.0, "avg_paragraph_words": 0.0,
            "lexical_variety": 0.0, "flesch_ease": 0.0, "grade_level": 0.0,
            "adverbs_per_100_words": 0.0,
        }
    syllables = sum(_syllables(w) for w in words)
    words_per_sentence = len(words) / len(sentences)
    syllables_per_word = syllables / len(words)
    ease = 206.835 - 1.015 * words_per_sentence - 84.6 * syllables_per_word
    grade = 0.39 * words_per_sentence + 11.8 * syllables_per_word - 15.59
    adverbs = [w for w in words if w.lower().endswith("ly") and len(w) > 4]
    return {
        "sentences": len(sentences),
        "words": len(words),
        "paragraphs": len(paragraphs),
        "avg_sentence_words": round(words_per_sentence, 1),
        "avg_paragraph_words": round(len(words) / len(paragraphs), 1),
        "lexical_variety": round(len({w.lower() for w in words}) / len(words), 3),
        "flesch_ease": round(max(0.0, min(120.0, ease)), 1),
        "grade_level": round(max(0.0, grade), 1),
        "adverbs_per_100_words": round(len(adverbs) / len(words) * 100, 1),
    }


def report(text: str, rules: list[str], banned_phrases: list[str] | None = None) -> dict:
    """Everything the style panel needs for one text."""
    findings = scan(text, rules, banned_phrases)
    return {
        "total": total(findings),
        "by_rule": summary(findings),
        "findings": [f.to_dict() for f in findings],
        "readability": readability(text),
    }


def total(findings: list[Finding]) -> int:
    return sum(f.count for f in findings)


def summary(findings: list[Finding]) -> dict[str, int]:
    return {f.rule: f.count for f in findings}


def bullet_report(findings: list[Finding], limit: int = 8) -> str:
    """Compact, prompt-ready list of what to fix."""
    lines = []
    for finding in findings[:limit]:
        examples = "; ".join(f"“{e}”" for e in finding.excerpts[:2])
        line = f"- {finding.description}: {finding.count} instance(s)"
        if examples:
            line += f" — e.g. {examples}"
        lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Diff (used by the UI to show what the editorial pass changed)
# --------------------------------------------------------------------------- #
PARAGRAPH = re.compile(r"\n\s*\n")


def paragraph_diff(before: str, after: str) -> list[dict]:
    """Paragraph-level diff: [{'op': 'equal'|'del'|'ins'|'change', 'before':…, 'after':…}]"""
    a = [p.strip() for p in PARAGRAPH.split(before.strip()) if p.strip()]
    b = [p.strip() for p in PARAGRAPH.split(after.strip()) if p.strip()]
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.append({"op": "equal", "before": a[i1:i2], "after": b[j1:j2]})
        elif tag == "replace":
            # Pair up paragraphs so the reader sees which one changed.
            old, new = a[i1:i2], b[j1:j2]
            for k in range(max(len(old), len(new))):
                out.append(
                    {
                        "op": "change",
                        "before": old[k] if k < len(old) else "",
                        "after": new[k] if k < len(new) else "",
                    }
                )
        elif tag == "delete":
            out.append({"op": "del", "before": a[i1:i2], "after": []})
        elif tag == "insert":
            out.append({"op": "ins", "before": [], "after": b[j1:j2]})
    return out


def length_ratio(before: str, after: str) -> float:
    before_words = len(before.split()) or 1
    return len(after.split()) / before_words
