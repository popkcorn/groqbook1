"""Export: Markdown, HTML (print-ready), EPUB 3 and PDF.

The PDF path deliberately avoids WeasyPrint/md2pdf — those need Pango/Cairo and
a pinned wrapper API, the exact combination that broke in the original app.
ReportLab is pure Python plus embedded fonts, so a PDF always builds.
"""

from __future__ import annotations

import copy
import html
import io
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .schema import Plan


@dataclass(frozen=True)
class ExportOptions:
    """Author-selected layout settings shared by the export formats."""

    page_size: str = "a5"
    margins: str = "normal"
    chapter_heading: str = "chapter"
    body_font_size: int = 10
    line_spacing: str = "normal"
    paragraph_style: str = "indent"
    alignment: str = "justify"
    chapter_new_page: bool = True
    running_matter: bool = True
    page_numbers: bool = True
    unit_label: str = "chapter"
    part_label: str = "part"
    title_override: str | None = None
    subtitle_override: str | None = None
    show_title_page: bool = True
    show_subtitle: bool = True
    cover_kicker: str | None = None
    cover_alignment: str = "left"
    cover_position: str = "top"
    part_alignment: str = "left"
    part_position: str = "top"
    chapter_alignment: str = "left"
    chapter_position: str = "top"
    end_imprint: str | None = None
    header_left: str = ""
    header_right: str = "BOOKWRIGHT"

    def __post_init__(self):
        if self.unit_label not in UNIT_LABELS:
            raise ValueError(f"unit_label must be one of {sorted(UNIT_LABELS)}")
        if self.part_label not in PART_LABELS:
            raise ValueError(f"part_label must be one of {sorted(PART_LABELS)}")
        if self.title_override is not None and len(self.title_override) > 120:
            raise ValueError("title override is too long")
        if self.subtitle_override is not None and len(self.subtitle_override) > 180:
            raise ValueError("subtitle override is too long")
        if self.cover_kicker is not None and len(self.cover_kicker) > 120:
            raise ValueError("cover line is too long")
        if self.end_imprint is not None and len(self.end_imprint) > 1500:
            raise ValueError("end imprint is too long")
        for field_name in ("cover_alignment", "part_alignment", "chapter_alignment"):
            if getattr(self, field_name) not in TEXT_ALIGNMENTS:
                raise ValueError(f"{field_name} must be one of {sorted(TEXT_ALIGNMENTS)}")
        for field_name in ("cover_position", "part_position", "chapter_position"):
            if getattr(self, field_name) not in TEXT_POSITIONS:
                raise ValueError(f"{field_name} must be one of {sorted(TEXT_POSITIONS)}")
        if len(self.header_left) > 80 or len(self.header_right) > 80:
            raise ValueError("PDF header text must be 80 characters or fewer")
        if self.page_size not in PAGE_SIZES:
            raise ValueError(f"page_size must be one of {sorted(PAGE_SIZES)}")
        if self.margins not in PAGE_MARGINS:
            raise ValueError(f"margins must be one of {sorted(PAGE_MARGINS)}")
        if self.chapter_heading not in CHAPTER_HEADINGS:
            raise ValueError(f"chapter_heading must be one of {sorted(CHAPTER_HEADINGS)}")
        if self.body_font_size not in FONT_SIZES:
            raise ValueError(f"body_font_size must be one of {sorted(FONT_SIZES)}")
        if self.line_spacing not in LINE_SPACING:
            raise ValueError(f"line_spacing must be one of {sorted(LINE_SPACING)}")
        if self.paragraph_style not in PARAGRAPH_STYLES:
            raise ValueError(f"paragraph_style must be one of {sorted(PARAGRAPH_STYLES)}")
        if self.alignment not in ALIGNMENTS:
            raise ValueError(f"alignment must be one of {sorted(ALIGNMENTS)}")


PAGE_SIZES = {"a5": "A5", "a4": "A4", "letter": "Letter"}
# Horizontal, top, bottom in millimetres.
PAGE_MARGINS = {"narrow": (12, 14, 15), "normal": (17, 19, 20), "wide": (25, 25, 25)}
CHAPTER_HEADINGS = {"chapter", "number", "title"}
UNIT_LABELS = {"chapter", "section", "essay", "entry"}
PART_LABELS = {"part", "book", "volume"}
FONT_SIZES = {9, 10, 11, 12, 13, 14}
LINE_SPACING = {"compact": 1.3, "normal": 1.5, "airy": 1.8}
PARAGRAPH_STYLES = {"indent", "space"}
ALIGNMENTS = {"justify", "left"}
TEXT_ALIGNMENTS = {"left", "center", "right"}
TEXT_POSITIONS = {"top", "middle", "bottom"}
DEFAULT_COVER_KICKER = "A BOOK WRITTEN WITH BOOKWRIGHT"
DEFAULT_END_IMPRINT = (
    "Generated on {date} by Bookwright: an architect model planned the book, "
    "a writer model drafted each {unit} against a running continuity bible, "
    "and an editor model summarised what had been written. Text is model output; "
    "verify any claim before relying on it."
)
DEFAULT_EXPORT_OPTIONS = ExportOptions()

# Models sometimes use Unicode hyphens that older fonts and ebook/PDF readers
# cannot render. Keep the intended word hyphen, but export it as the universal
# ASCII hyphen-minus across every format.
EXPORT_HYPHENS = str.maketrans({
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u00ad": "-",  # soft hyphen
    "\ufe63": "-",  # small hyphen-minus
    "\uff0d": "-",  # full-width hyphen-minus
})


def _normalized_export_inputs(plan: Plan, chapters: dict[int, dict]) -> tuple[Plan, dict[int, dict]]:
    """Normalize uncommon hyphen glyphs without changing the stored book."""
    plan_copy = copy.deepcopy(plan)
    for field_name in ("title", "subtitle", "thesis", "audience", "voice"):
        value = getattr(plan_copy, field_name)
        if isinstance(value, str):
            setattr(plan_copy, field_name, value.translate(EXPORT_HYPHENS))
    for part in plan_copy.parts:
        part.title = part.title.translate(EXPORT_HYPHENS)
        part.summary = part.summary.translate(EXPORT_HYPHENS)
        for chapter in part.chapters:
            chapter.title = chapter.title.translate(EXPORT_HYPHENS)

    chapters_copy = copy.deepcopy(chapters)
    for state in chapters_copy.values():
        if isinstance(state, dict) and isinstance(state.get("text"), str):
            state["text"] = state["text"].translate(EXPORT_HYPHENS)
    return plan_copy, chapters_copy


def _plural(label: str) -> str:
    if label.endswith("y") and label[-2:-1] not in "aeiou":
        return label[:-1] + "ies"
    if label.endswith(("s", "x", "ch", "sh")):
        return label + "es"
    return label + "s"


def _chapter_marker(index: int, style: str, unit_label: str = "chapter") -> str:
    if style == "chapter":
        return f"{unit_label.title()} {index}"
    if style == "number":
        return f"{index}."
    return ""


def _display_title(plan: Plan, options: ExportOptions) -> str:
    override = options.title_override.strip() if options.title_override else ""
    return (override or plan.title).translate(EXPORT_HYPHENS)


def _display_subtitle(plan: Plan, options: ExportOptions) -> str:
    value = plan.subtitle if options.subtitle_override is None else options.subtitle_override.strip()
    return value.translate(EXPORT_HYPHENS)


def _end_imprint(options: ExportOptions, *, stamp: str, title: str, subtitle: str) -> str:
    template = DEFAULT_END_IMPRINT if options.end_imprint is None else options.end_imprint
    replacements = {
        "{date}": stamp,
        "{title}": title,
        "{subtitle}": subtitle,
        "{unit}": options.unit_label,
        "{group}": options.part_label,
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template.translate(EXPORT_HYPHENS).strip()


def _position_fraction(position: str, *, top: float, middle: float, bottom: float) -> float:
    return {"top": top, "middle": middle, "bottom": bottom}[position]


def _position_css(position: str) -> str:
    return {"top": "flex-start", "middle": "center", "bottom": "flex-end"}[position]


FONT_DIRS = [
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/TTF"),
    Path("/Library/Fonts"),
    Path("C:/Windows/Fonts"),
]


# --------------------------------------------------------------------------- #
# Minimal Markdown -> blocks
# --------------------------------------------------------------------------- #
@dataclass
class Block:
    type: str  # h | p | ul | quote | code
    text: str = ""
    level: int = 0
    items: list[str] | None = None


def parse_markdown(md: str) -> list[Block]:
    blocks: list[Block] = []
    para: list[str] = []
    bullets: list[str] = []
    quote: list[str] = []
    code: list[str] = []
    in_code = False

    def flush_para():
        if para:
            blocks.append(Block("p", " ".join(para).strip()))
            para.clear()

    def flush_bullets():
        if bullets:
            blocks.append(Block("ul", items=list(bullets)))
            bullets.clear()

    def flush_quote():
        if quote:
            blocks.append(Block("quote", " ".join(quote).strip()))
            quote.clear()

    for raw in md.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if line.startswith("```"):
            if in_code:
                blocks.append(Block("code", "\n".join(code)))
                code.clear()
                in_code = False
            else:
                flush_para()
                flush_bullets()
                flush_quote()
                in_code = True
            continue
        if in_code:
            code.append(line)
            continue
        if not line.strip():
            flush_para()
            flush_bullets()
            flush_quote()
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            flush_para()
            flush_bullets()
            flush_quote()
            blocks.append(Block("h", heading.group(2).strip(), level=len(heading.group(1))))
            continue
        if re.match(r"^\s*[-*]\s+", line):
            flush_para()
            flush_quote()
            bullets.append(re.sub(r"^\s*[-*]\s+", "", line).strip())
            continue
        if re.match(r"^\s*>\s?", line):
            flush_para()
            flush_bullets()
            quote.append(re.sub(r"^\s*>\s?", "", line).strip())
            continue
        if bullets:
            flush_bullets()
        if quote:
            flush_quote()
        para.append(line.strip())

    flush_para()
    flush_bullets()
    flush_quote()
    if code:
        blocks.append(Block("code", "\n".join(code)))
    return [b for b in blocks if (b.text.strip() or b.items)]


INLINE_RE = [
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), r"<em>\1</em>"),
]


def inline_html(text: str) -> str:
    escaped = html.escape(text, quote=False)
    for pattern, repl in INLINE_RE:
        escaped = pattern.sub(repl, escaped)
    return escaped


def inline_reportlab(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"`([^`]+)`", r'<font face="DejaVuSans">\1</font>', escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)
    return escaped


def blocks_to_html(blocks: list[Block]) -> str:
    out: list[str] = []
    for b in blocks:
        if b.type == "h":
            level = min(max(b.level + 1, 2), 6)  # chapter title is the <h1>
            out.append(f"<h{level}>{inline_html(b.text)}</h{level}>")
        elif b.type == "p":
            out.append(f"<p>{inline_html(b.text)}</p>")
        elif b.type == "ul":
            items = "".join(f"<li>{inline_html(i)}</li>" for i in b.items or [])
            out.append(f"<ul>{items}</ul>")
        elif b.type == "quote":
            out.append(f"<blockquote><p>{inline_html(b.text)}</p></blockquote>")
        elif b.type == "code":
            out.append(f"<pre><code>{html.escape(b.text)}</code></pre>")
    return "\n".join(out)


def slug(text: str, fallback: str = "book") -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (value[:60] or fallback)


# --------------------------------------------------------------------------- #
# Markdown / HTML
# --------------------------------------------------------------------------- #
def to_markdown(
    plan: Plan, chapters: dict[int, dict], *, options: ExportOptions | None = None
) -> str:
    options = options or DEFAULT_EXPORT_OPTIONS
    plan, chapters = _normalized_export_inputs(plan, chapters)
    title, subtitle = _display_title(plan, options), _display_subtitle(plan, options)
    lines = [f"# {title}", ""]
    if options.show_subtitle and subtitle:
        lines += [f"*{subtitle}*", ""]
    if plan.thesis:
        lines += [f"> {plan.thesis}", ""]
    lines += ["---", ""]
    for part in plan.parts:
        lines += [f"## {options.part_label.title()} {part.index}: {part.title}", ""]
        if part.summary:
            lines += [f"*{part.summary}*", ""]
        for chapter in part.chapters:
            marker = _chapter_marker(chapter.index, options.chapter_heading, options.unit_label)
            if marker and options.chapter_heading == "chapter":
                marker += "."
            heading = f"{marker} {chapter.title}".strip()
            lines += [f"### {heading}", ""]
            lines += [(chapters.get(chapter.index, {}).get("text") or "").strip(), ""]
    stamp = datetime.now(UTC).strftime("%d %B %Y")
    imprint = _end_imprint(options, stamp=stamp, title=title, subtitle=subtitle)
    if imprint:
        lines += ["---", "", imprint, ""]
    return "\n".join(lines).strip() + "\n"


HTML_CSS = """
:root { --ink:#17130f; --muted:#6b5f52; --rule:#d9cfc0; --paper:#fdfaf4; }
* { box-sizing: border-box; }
html { background:#efe7d9; }
body { margin:0; color:var(--ink); background:var(--paper);
  font-family:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  font-size:var(--body-size,10pt); line-height:var(--line-spacing,1.5); text-rendering:optimizeLegibility; }
.page { max-width:34em; margin:0 auto; padding:4rem 2.2rem 6rem; }
.cover { min-height:80vh; display:flex; flex-direction:column;
  page-break-after:always; break-after:page; }
.cover .kicker { font-family:ui-sans-serif,system-ui,sans-serif; font-size:.7rem;
  letter-spacing:.22em; color:var(--muted); margin-bottom:2.5rem; }
.cover h1 { font-size:2.9rem; line-height:1.08; margin:0 0 1rem; font-weight:600;
  letter-spacing:-.01em; }
.cover .sub { font-size:1.15rem; color:var(--muted); font-style:italic; margin:0 0 3rem; }
.cover .rule { width:4rem; height:2px; background:var(--ink); margin:0 0 1.4rem; }
.cover .rule.align-center { margin-left:auto; margin-right:auto; }
.cover .rule.align-right { margin-left:auto; margin-right:0; }
.cover .meta { font-family:ui-sans-serif,system-ui,sans-serif; font-size:.72rem;
  letter-spacing:.14em; text-transform:uppercase; color:var(--muted); }
.cover .thesis { margin-top:3rem; font-size:.95rem; color:var(--muted); max-width:26em; }
.toc { page-break-after:always; break-after:page; }
.toc h2, .part-title h2 { font-family:ui-sans-serif,system-ui,sans-serif;
  font-size:.72rem; letter-spacing:.22em; text-transform:uppercase; color:var(--muted);
  margin:0 0 1.8rem; font-weight:600; }
.toc ol { list-style:none; padding:0; margin:0; }
.toc .part { margin:1.6rem 0 .5rem; font-weight:600; }
.toc li { padding:.18rem 0; font-size:.95rem; }
.toc a { color:inherit; text-decoration:none; }
.toc .ch { display:flex; justify-content:space-between; gap:1rem; color:var(--ink); }
.toc .ch span:first-child { color:var(--muted); font-variant-numeric:tabular-nums; padding-right:.5rem; }
.part-title { page-break-before:always; break-before:page; display:flex; flex-direction:column;
  min-height:80vh; }
.part-title.position-top { padding-top:22vh; }
.part-title h3 { font-size:1.9rem; margin:0 0 .8rem; font-weight:600; }
.part-title p { color:var(--muted); font-style:italic; max-width:24em; }
.chapter.page-break { page-break-before:always; break-before:page; }
.chapter .ch-head { margin-bottom:2rem; }
.chapter.page-break .ch-head.position-middle { padding-top:28vh; }
.chapter.page-break .ch-head.position-bottom { padding-top:48vh; }
.chapter .ch-num { font-family:ui-sans-serif,system-ui,sans-serif; font-size:.7rem;
  letter-spacing:.2em; text-transform:uppercase; color:var(--muted); }
.chapter h2 { font-size:1.75rem; line-height:1.2; margin:.4rem 0 0; font-weight:600; }
.chapter p { margin:0 0 var(--paragraph-gap,0); text-align:var(--body-align,justify); hyphens:auto; }
.chapter p + p, .chapter ul + p, .chapter blockquote + p { text-indent:var(--paragraph-indent,1.4em); margin-top:0; }
.chapter h3 { font-family:ui-sans-serif,system-ui,sans-serif; font-size:.86rem;
  letter-spacing:.12em; text-transform:uppercase; margin:1.9rem 0 .7rem; color:var(--muted); }
.chapter h3 + p { text-indent:0; }
.chapter blockquote { margin:1.6rem 0; padding:.2rem 0 .2rem 1.2rem;
  border-left:2px solid var(--rule); color:var(--muted); font-style:italic; }
.chapter blockquote p { text-indent:0; text-align:left; }
.chapter ul { margin:1.2rem 0; padding-left:1.2rem; }
.chapter li { margin:.4rem 0; }
.chapter pre { background:#f3ece0; padding:.9rem 1rem; overflow:auto; font-size:.82rem;
  border-radius:2px; }
code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.9em; }
.colophon { page-break-before:always; break-before:page; color:var(--muted);
  font-size:.85rem; padding-top:22vh; }
@media print {
  html, body { background:#fff; }
  .page { max-width:none; padding:0; }
  @page { size:A5; margin:19mm 17mm 20mm; }
  @page :first { margin:0; }
  .chapter p { orphans:3; widows:3; }
  .cover { min-height:auto; padding:32mm 17mm; }
  a { color:inherit; text-decoration:none; }
}
"""


def to_html(
    plan: Plan,
    chapters: dict[int, dict],
    *,
    generated_at: datetime | None = None,
    options: ExportOptions | None = None,
) -> str:
    options = options or DEFAULT_EXPORT_OPTIONS
    plan, chapters = _normalized_export_inputs(plan, chapters)
    stamp = (generated_at or datetime.now(UTC)).strftime("%d %B %Y")
    title, subtitle = _display_title(plan, options), _display_subtitle(plan, options)
    toc = ['<nav class="toc" id="contents"><h2>Contents</h2><ol>']
    for part in plan.parts:
        part_heading = f"{options.part_label.title()} {part.index}: {part.title}"
        toc.append(f'<li class="part"><a href="#part-{part.index}">{html.escape(part_heading)}</a>')
        toc.append("<ol>")
        for chapter in part.chapters:
            marker = _chapter_marker(chapter.index, options.chapter_heading, options.unit_label)
            toc.append(
                f'<li class="ch"><a href="#ch-{chapter.index}">'
                + (f'<span>{html.escape(marker)}</span>' if marker else "")
                + f"<span>{html.escape(chapter.title)}</span></a></li>"
            )
        toc.append("</ol></li>")
    toc.append("</ol></nav>")

    body: list[str] = []
    for part in plan.parts:
        body.append(
            f'<section class="part-title position-{options.part_position}" id="part-{part.index}" style="text-align:{options.part_alignment};justify-content:{_position_css(options.part_position)}"><h2>{options.part_label.title()} {part.index}</h2>'
            f"<h3>{html.escape(part.title)}</h3>"
            + (f"<p>{html.escape(part.summary)}</p>" if part.summary else "")
            + "</section>"
        )
        for chapter in part.chapters:
            text = (chapters.get(chapter.index, {}).get("text") or "").strip()
            marker = _chapter_marker(chapter.index, options.chapter_heading, options.unit_label)
            page_class = " page-break" if options.chapter_new_page else ""
            label = f'<div class="ch-num">{html.escape(marker)}</div>' if marker else ""
            body.append(
                f'<article class="chapter{page_class}" id="ch-{chapter.index}">'
                f'<header class="ch-head position-{options.chapter_position}" style="text-align:{options.chapter_alignment}">{label}'
                f"<h2>{html.escape(chapter.title)}</h2></header>"
                f"{blocks_to_html(parse_markdown(text))}</article>"
            )

    cover_html = ""
    if options.show_title_page:
        cover_kicker = DEFAULT_COVER_KICKER if options.cover_kicker is None else options.cover_kicker
        cover_parts = [
            f'<section class="cover position-{options.cover_position}" style="text-align:{options.cover_alignment};justify-content:{_position_css(options.cover_position)}">',
        ]
        if cover_kicker.strip():
            cover_parts.append(f'<div class="kicker">{html.escape(cover_kicker.strip())}</div>')
        cover_parts += [
            f'<div class="rule align-{options.cover_alignment}"></div>',
            f"<h1>{html.escape(title)}</h1>",
        ]
        if options.show_subtitle and subtitle:
            cover_parts.append(f'<p class="sub">{html.escape(subtitle)}</p>')
        cover_parts.append(
            f'<div class="meta">{html.escape(plan.audience)} · {len(plan.chapters)} '
            f'{_plural(options.unit_label)} · {stamp}</div>'
        )
        if plan.thesis:
            cover_parts.append(f'<p class="thesis">{html.escape(plan.thesis)}</p>')
        cover_parts.append("</section>")
        cover_html = "".join(cover_parts)

    imprint = _end_imprint(options, stamp=stamp, title=title, subtitle=subtitle)
    imprint_html = (
        f'<section class="colophon"><p>{html.escape(imprint).replace(chr(10), "<br>")}</p></section>'
        if imprint else ""
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{HTML_CSS.replace('@page { size:A5; margin:19mm 17mm 20mm;', f'@page {{ size:{PAGE_SIZES[options.page_size]}; margin:{PAGE_MARGINS[options.margins][1]}mm {PAGE_MARGINS[options.margins][0]}mm {PAGE_MARGINS[options.margins][2]}mm;')}</style></head>
<body style="--body-size:{options.body_font_size}pt; --line-spacing:{LINE_SPACING[options.line_spacing]}; --body-align:{options.alignment}; --paragraph-indent:{'1.4em' if options.paragraph_style == 'indent' else '0'}; --paragraph-gap:{'0' if options.paragraph_style == 'indent' else '0.75em'};"><div class="page">
{cover_html}
{''.join(toc)}
{''.join(body)}
{imprint_html}
</div></body></html>"""


# --------------------------------------------------------------------------- #
# EPUB 3
# --------------------------------------------------------------------------- #
EPUB_CSS = """
body { font-family: serif; line-height: 1.6; margin: 0 5%; }
h1 { font-family: sans-serif; font-size: 1.6em; margin: 1.5em 0 .6em; }
h2 { font-size: 1.15em; margin: 1.4em 0 .5em; }
p { margin: 0; text-indent: 1.3em; text-align: justify; }
p.first, h1 + p, h2 + p { text-indent: 0; }
blockquote { margin: 1.2em 1.4em; font-style: italic; color: #555; }
ul { margin: 1em 0; }
li { margin: .35em 0; }
.title { margin-top: 0; min-height: 80vh; display: flex; flex-direction: column; }
.title h1 { font-size: 2em; }
.title .sub { text-indent: 0; font-style: italic; color: #555; }
.title .meta { text-indent: 0; margin-top: 3em; font-family: sans-serif;
  font-size: .75em; letter-spacing: .12em; text-transform: uppercase; color: #777; }
.part-title { min-height: 80vh; display: flex; flex-direction: column; }
.chapter.page-break .chapter-head.position-middle { padding-top: 28vh; }
.chapter.page-break .chapter-head.position-bottom { padding-top: 48vh; }
.colophon { page-break-before: always; break-before: page; }
"""

XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head><meta charset="utf-8"/><title>{title}</title>
<link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>{body}</body></html>"""


def to_epub(
    plan: Plan, chapters: dict[int, dict], *, options: ExportOptions | None = None
) -> bytes:
    options = options or DEFAULT_EXPORT_OPTIONS
    plan, chapters = _normalized_export_inputs(plan, chapters)
    title, subtitle = _display_title(plan, options), _display_subtitle(plan, options)
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, title + subtitle)}"
    modified = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    docs: list[tuple[str, str, str]] = []  # (id, filename, xhtml)
    cover_kicker = DEFAULT_COVER_KICKER if options.cover_kicker is None else options.cover_kicker
    title_body = (
        f'<div class="title position-{options.cover_position}" style="text-align:{options.cover_alignment};justify-content:{_position_css(options.cover_position)}">'
        + (f'<p class="kicker">{html.escape(cover_kicker.strip())}</p>' if cover_kicker.strip() else "")
        + f'<h1>{html.escape(title)}</h1>'
        + (f'<p class="sub">{html.escape(subtitle)}</p>' if options.show_subtitle and subtitle else "")
        + f'<p class="meta">{html.escape(plan.audience)}</p></div>'
    )
    if options.show_title_page:
        docs.append(("titlepage", "title.xhtml", XHTML.format(title="Title", body=title_body)))

    toc_items: list[str] = []
    for part in plan.parts:
        part_body = (
            f'<section class="part-title position-{options.part_position}" style="text-align:{options.part_alignment};justify-content:{_position_css(options.part_position)}">'
            f"<h1>{options.part_label.title()} {part.index}: {html.escape(part.title)}</h1>"
            + (f"<p>{html.escape(part.summary)}</p>" if part.summary else "")
            + "</section>"
        )
        name = f"part{part.index}.xhtml"
        docs.append((f"part{part.index}", name, XHTML.format(title=part.title, body=part_body)))
        part_heading = f"{options.part_label.title()} {part.index}: {part.title}"
        toc_items.append(f'<li><a href="{name}">{html.escape(part_heading)}</a><ol>')
        for chapter in part.chapters:
            text = (chapters.get(chapter.index, {}).get("text") or "").strip()
            marker = _chapter_marker(chapter.index, options.chapter_heading, options.unit_label)
            break_class = " chapter-break" if options.chapter_new_page else ""
            label = f'<p class="chapter-num">{html.escape(marker)}</p>' if marker else ""
            body = (
                f'<section class="chapter{break_class}"><header class="chapter-head position-{options.chapter_position}" style="text-align:{options.chapter_alignment}">{label}'
                f"<h1>{html.escape(chapter.title)}</h1></header>"
                f"{blocks_to_html(parse_markdown(text))}</section>"
            )
            ch_name = f"ch{chapter.index:02d}.xhtml"
            docs.append(
                (f"ch{chapter.index:02d}", ch_name, XHTML.format(title=chapter.title, body=body))
            )
            toc_items.append(f'<li><a href="{ch_name}">{html.escape(chapter.title)}</a></li>')
        toc_items.append("</ol></li>")

    stamp = datetime.now(UTC).strftime("%d %B %Y")
    imprint = _end_imprint(options, stamp=stamp, title=title, subtitle=subtitle)
    if imprint:
        imprint_html = '<section class="colophon"><p>' + html.escape(imprint).replace("\n", "<br/>") + "</p></section>"
        docs.append(("colophon", "colophon.xhtml", XHTML.format(title="Colophon", body=imprint_html)))

    nav = XHTML.format(
        title="Contents",
        body='<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>'
        + "".join(toc_items)
        + "</ol></nav>",
    )

    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
    ]
    spine = []
    for doc_id, filename, _ in docs:
        manifest.append(
            f'<item id="{doc_id}" href="{filename}" media-type="application/xhtml+xml"/>'
        )
        spine.append(f'<itemref idref="{doc_id}"/>')

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{book_id}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:language>en</dc:language>
    <dc:creator>Bookwright</dc:creator>
    <dc:description>{html.escape(plan.thesis or subtitle)}</dc:description>
    <meta property="dcterms:modified">{modified}</meta>
  </metadata>
  <manifest>
    {chr(10).join('    ' + m for m in manifest)}
  </manifest>
  <spine>
    {chr(10).join('    ' + s for s in spine)}
  </spine>
</package>"""

    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        # mimetype must be first and uncompressed for EPUB readers.
        info = zipfile.ZipInfo("mimetype")
        zf.writestr(info, "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        epub_css = EPUB_CSS.replace(
            "body { font-family: serif; line-height: 1.6; margin: 0 5%; }",
            f"body {{ font-family: serif; font-size: {options.body_font_size}pt; "
            f"line-height: {LINE_SPACING[options.line_spacing]}; margin: 0 5%; }}",
        )
        paragraph_margin = "0" if options.paragraph_style == "indent" else "0 0 .75em"
        paragraph_indent = "1.3em" if options.paragraph_style == "indent" else "0"
        epub_css = epub_css.replace(
            "p { margin: 0; text-indent: 1.3em; text-align: justify; }",
            f"p {{ margin: {paragraph_margin}; text-indent: {paragraph_indent}; "
            f"text-align: {options.alignment}; }}",
        )
        epub_css += (
            f"\n.title {{ text-align: {options.cover_alignment}; justify-content:{_position_css(options.cover_position)}; }}\n"
            f".part-title {{ text-align: {options.part_alignment}; justify-content:{_position_css(options.part_position)}; }}\n"
            f".chapter-head {{ text-align: {options.chapter_alignment}; }}\n"
            ".title { min-height:80vh; display:flex; flex-direction:column; }\n"
            ".part-title { min-height:80vh; display:flex; flex-direction:column; }\n"
        )
        if options.chapter_new_page:
            epub_css += (
                "\n.chapter-break { break-before: page; page-break-before: always; }\n"
                ".chapter-head.position-middle { padding-top:28vh; }\n"
                ".chapter-head.position-bottom { padding-top:48vh; }\n"
            )
        if options.chapter_heading != "title":
            epub_css += ".chapter-num { font-family: sans-serif; font-size: .75em; text-indent: 0; }\n"
        zf.writestr("OEBPS/style.css", epub_css, compress_type=zipfile.ZIP_DEFLATED)
        for _, filename, xhtml in docs:
            zf.writestr(f"OEBPS/{filename}", xhtml, compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def _font_files() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for directory in FONT_DIRS:
        if not directory.is_dir():
            continue
        for name, filename in {
            "serif": "DejaVuSerif.ttf",
            "serif_bold": "DejaVuSerif-Bold.ttf",
            "sans": "DejaVuSans.ttf",
            "sans_bold": "DejaVuSans-Bold.ttf",
            "mono": "DejaVuSansMono.ttf",
        }.items():
            path = directory / filename
            if path.exists() and name not in found:
                found[name] = path
        if {"serif", "serif_bold", "sans", "sans_bold"} <= found.keys():
            break
    return found


def to_pdf(
    plan: Plan,
    chapters: dict[int, dict],
    *,
    generated_at: datetime | None = None,
    options: ExportOptions | None = None,
) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4, A5, LETTER
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        BaseDocTemplate,
        Frame,
        HRFlowable,
        NextPageTemplate,
        PageBreak,
        PageTemplate,
        Paragraph,
        Spacer,
    )
    from reportlab.platypus.tableofcontents import TableOfContents

    options = options or DEFAULT_EXPORT_OPTIONS
    plan, chapters = _normalized_export_inputs(plan, chapters)
    fonts = _font_files()
    if {"serif", "serif_bold", "sans", "sans_bold"} <= fonts.keys():
        for name, path in fonts.items():
            pdfmetrics.registerFont(TTFont(f"BW-{name}", str(path)))
        serif, serif_bold = "BW-serif", "BW-serif_bold"
        sans, sans_bold = "BW-sans", "BW-sans_bold"
        for family, normal, bold in ((serif, serif, serif_bold), (sans, sans, sans_bold)):
            pdfmetrics.registerFontFamily(
                family, normal=normal, bold=bold, italic=normal, boldItalic=bold
            )
    else:  # fall back to base-14 fonts; unicode coverage is lost but layout holds
        serif = serif_bold = "Times-Roman"
        sans = sans_bold = "Helvetica"

    body_size = options.body_font_size
    body_leading = body_size * LINE_SPACING[options.line_spacing]
    paragraph_indent = 4.2 * mm if options.paragraph_style == "indent" else 0
    paragraph_gap = 0 if options.paragraph_style == "indent" else body_size * 0.75
    body_alignment = TA_JUSTIFY if options.alignment == "justify" else TA_LEFT
    alignment_map = {"left": TA_LEFT, "center": TA_CENTER, "right": TA_RIGHT}

    styles = {
        "cover_kicker": ParagraphStyle(
            "CoverKicker", fontName=sans, fontSize=7.5, textColor=colors.HexColor("#6b5f52"),
            leading=12, spaceAfter=18, alignment=alignment_map[options.cover_alignment],
        ),
        "cover_title": ParagraphStyle(
            "CoverTitle", fontName=sans_bold, fontSize=27, leading=31,
            textColor=colors.HexColor("#17130f"), spaceAfter=10,
            alignment=alignment_map[options.cover_alignment],
        ),
        "cover_sub": ParagraphStyle(
            "CoverSub", fontName=serif, fontSize=11.5, leading=16,
            textColor=colors.HexColor("#6b5f52"), spaceAfter=26,
            alignment=alignment_map[options.cover_alignment],
        ),
        "cover_meta": ParagraphStyle(
            "CoverMeta", fontName=sans, fontSize=7.5, leading=12,
            textColor=colors.HexColor("#6b5f52"), alignment=alignment_map[options.cover_alignment],
        ),
        "cover_thesis": ParagraphStyle(
            "CoverThesis", fontName=serif, fontSize=9.5, leading=14,
            textColor=colors.HexColor("#6b5f52"), spaceBefore=34,
            alignment=alignment_map[options.cover_alignment],
        ),
        "part_label": ParagraphStyle(
            "PartLabel", fontName=sans, fontSize=7.5, leading=12, spaceAfter=10,
            textColor=colors.HexColor("#6b5f52"), alignment=alignment_map[options.part_alignment],
        ),
        "part_title": ParagraphStyle(
            "PartTitle", fontName=sans_bold, fontSize=17, leading=22, spaceAfter=10,
            textColor=colors.HexColor("#17130f"), alignment=alignment_map[options.part_alignment],
        ),
        "part_summary": ParagraphStyle(
            "PartSummary", fontName=serif, fontSize=9.5, leading=14,
            textColor=colors.HexColor("#6b5f52"), alignment=alignment_map[options.part_alignment],
        ),
        "chapter_label": ParagraphStyle(
            "ChapterLabel", fontName=sans, fontSize=7, leading=10, spaceAfter=4,
            textColor=colors.HexColor("#6b5f52"), alignment=alignment_map[options.chapter_alignment],
        ),
        "chapter_title": ParagraphStyle(
            "ChapterTitle", fontName=sans_bold, fontSize=max(14, body_size * 1.5),
            leading=max(18, body_size * 1.9), spaceAfter=14, keepWithNext=True,
            textColor=colors.HexColor("#17130f"), alignment=alignment_map[options.chapter_alignment],
        ),
        "body_first": ParagraphStyle(
            "BodyFirst", fontName=serif, fontSize=body_size, leading=body_leading,
            alignment=body_alignment, spaceAfter=paragraph_gap, firstLineIndent=0,
        ),
        "body": ParagraphStyle(
            "Body", fontName=serif, fontSize=body_size, leading=body_leading,
            alignment=body_alignment, firstLineIndent=paragraph_indent,
            spaceAfter=paragraph_gap,
        ),
        "h2": ParagraphStyle(
            "H2", fontName=sans_bold, fontSize=8.2, leading=12, spaceBefore=11,
            spaceAfter=5, textColor=colors.HexColor("#3b3128"),
        ),
        "bullet": ParagraphStyle(
            "Bullet", fontName=serif, fontSize=body_size, leading=body_leading,
            leftIndent=5 * mm, bulletIndent=1 * mm, spaceBefore=2,
            alignment=body_alignment,
        ),
        "quote": ParagraphStyle(
            "Quote", fontName=serif, fontSize=9.2, leading=14, leftIndent=6 * mm,
            rightIndent=4 * mm, textColor=colors.HexColor("#5a4f43"), spaceBefore=8,
            spaceAfter=8, alignment=TA_LEFT,
        ),
        "code": ParagraphStyle(
            "Code", fontName="BW-mono" if "mono" in fonts else "Courier",
            fontSize=7.8, leading=11, leftIndent=3 * mm, textColor=colors.HexColor("#3b3128"),
        ),
        "toc_title": ParagraphStyle(
            "TocTitle", fontName=sans_bold, fontSize=13, leading=18, spaceAfter=16,
        ),
        "toc_part": ParagraphStyle(
            "TocPart", fontName=sans_bold, fontSize=9.5, leading=16, spaceBefore=8,
            textColor=colors.HexColor("#17130f"),
        ),
        "toc_ch": ParagraphStyle(
            "TocCh", fontName=serif, fontSize=9.5, leading=16, leftIndent=4 * mm,
            firstLineIndent=-4 * mm,
        ),
        "colophon": ParagraphStyle(
            "Colophon", fontName=serif, fontSize=8.5, leading=13,
            textColor=colors.HexColor("#6b5f52"),
        ),
    }

    title, subtitle = _display_title(plan, options), _display_subtitle(plan, options)
    pagesize = {"a5": A5, "a4": A4, "letter": LETTER}[options.page_size]
    page_w, page_h = pagesize
    margin_x, margin_top, margin_bottom = (value * mm for value in PAGE_MARGINS[options.margins])
    book_title = (options.header_left.strip() or title).translate(EXPORT_HYPHENS).upper()
    header_right = options.header_right.strip().translate(EXPORT_HYPHENS).upper()

    class BookDoc(BaseDocTemplate):
        def __init__(self, target):
            super().__init__(
                target, pagesize=pagesize, leftMargin=margin_x, rightMargin=margin_x,
                topMargin=margin_top, bottomMargin=margin_bottom,
                title=title, author="Bookwright", subject=subtitle,
                creator="Bookwright",
            )
            frame = Frame(
                margin_x, margin_bottom, page_w - 2 * margin_x,
                page_h - margin_top - margin_bottom, id="body",
                leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
            )
            # A bare template for the title page: covers should not carry a
            # running head or a folio.
            self.addPageTemplates(
                [
                    PageTemplate(id="cover", frames=[frame]),
                    PageTemplate(id="body", frames=[frame], onPage=self._decorate),
                ]
            )

        def _decorate(self, canvas, doc):
            if not options.running_matter and not options.page_numbers:
                return
            canvas.saveState()
            if options.running_matter:
                canvas.setFont(sans, 6.6)
                canvas.setFillColor(colors.HexColor("#8a7d6c"))
                if book_title:
                    canvas.drawString(margin_x, page_h - margin_top + 6 * mm, book_title)
                if header_right:
                    canvas.drawRightString(page_w - margin_x, page_h - margin_top + 6 * mm,
                                           header_right)
                canvas.setStrokeColor(colors.HexColor("#d9cfc0"))
                canvas.setLineWidth(0.4)
                canvas.line(margin_x, page_h - margin_top + 4.5 * mm,
                            page_w - margin_x, page_h - margin_top + 4.5 * mm)
            if options.page_numbers:
                canvas.setFont(serif, 8.5)
                canvas.setFillColor(colors.HexColor("#5a4f43"))
                canvas.drawCentredString(page_w / 2, margin_bottom - 9 * mm, str(doc.page))
            canvas.restoreState()

        def afterFlowable(self, flowable):
            if not isinstance(flowable, Paragraph):
                return
            style = flowable.style.name
            text = flowable.getPlainText()
            if style in {"PartTitle", "ChapterTitle"}:
                level = 0 if style == "PartTitle" else 1
                key = f"{style}-{self.page}-{abs(hash(text))}"
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(text, key, level=level, closed=False)
                self.notify("TOCEntry", (level, text, self.page))

    buffer = io.BytesIO()
    doc = BookDoc(buffer)
    story: list = [NextPageTemplate("body")]

    # Optional cover (drawn on the bare template, then switch to body pages).
    if options.show_title_page:
        cover_offset = _position_fraction(options.cover_position, top=0.15, middle=0.30, bottom=0.42) * (page_h - margin_top - margin_bottom)
        story += [
            Spacer(1, cover_offset),
        ]
        cover_kicker = DEFAULT_COVER_KICKER if options.cover_kicker is None else options.cover_kicker
        if cover_kicker.strip():
            story.append(Paragraph(html.escape(cover_kicker.strip()), styles["cover_kicker"]))
        story += [
            HRFlowable(width=28 * mm, thickness=1.2, color=colors.HexColor("#17130f"),
                       spaceBefore=0, spaceAfter=16, hAlign=options.cover_alignment.upper()),
            Paragraph(html.escape(title), styles["cover_title"]),
        ]
        if options.show_subtitle and subtitle:
            story.append(Paragraph(html.escape(subtitle), styles["cover_sub"]))
        story += [
            HRFlowable(width=12 * mm, thickness=0.6, color=colors.HexColor("#c8963e"),
                       spaceBefore=2, spaceAfter=14),
            Paragraph(
                html.escape(
                    f"{plan.audience} · {len(plan.chapters)} {_plural(options.unit_label)} · "
                    f"{(generated_at or datetime.now(UTC)).strftime('%B %Y')}"
                ),
                styles["cover_meta"],
            ),
        ]
        if plan.thesis:
            story.append(Paragraph(html.escape(plan.thesis), styles["cover_thesis"]))
        story.append(PageBreak())

    # Contents
    story += [Paragraph("Contents", styles["toc_title"])]
    toc = TableOfContents()
    toc.levelStyles = [styles["toc_part"], styles["toc_ch"]]
    toc.dotsMinLevel = 1
    story += [toc, PageBreak()]

    for part in plan.parts:
        story += [
            Spacer(1, _position_fraction(options.part_position, top=0.15, middle=0.30, bottom=0.42) * (page_h - margin_top - margin_bottom)),
            Paragraph(f"{options.part_label.upper()} {part.index}", styles["part_label"]),
            Paragraph(html.escape(part.title), styles["part_title"]),
        ]
        if part.summary:
            story.append(Paragraph(html.escape(part.summary), styles["part_summary"]))
        story.append(PageBreak())

        for chapter in part.chapters:
            text = (chapters.get(chapter.index, {}).get("text") or "").strip()
            marker = _chapter_marker(chapter.index, options.chapter_heading, options.unit_label)
            if options.chapter_new_page:
                offset = _position_fraction(options.chapter_position, top=0.0, middle=0.25, bottom=0.40) * (page_h - margin_top - margin_bottom)
                if offset:
                    story.append(Spacer(1, offset))
            if marker:
                story.append(Paragraph(html.escape(marker.upper()), styles["chapter_label"]))
            story.append(Paragraph(html.escape(chapter.title), styles["chapter_title"]))
            first_para = True
            for block in parse_markdown(text):
                if block.type == "h":
                    story.append(Paragraph(inline_reportlab(block.text), styles["h2"]))
                    first_para = True
                elif block.type == "p":
                    style = styles["body_first"] if first_para else styles["body"]
                    story.append(Paragraph(inline_reportlab(block.text), style))
                    first_para = False
                elif block.type == "ul":
                    for item in block.items or []:
                        story.append(
                            Paragraph(inline_reportlab(item), styles["bullet"], bulletText="•")
                        )
                    first_para = True
                elif block.type == "quote":
                    story.append(Paragraph(inline_reportlab(block.text), styles["quote"]))
                    first_para = False
                elif block.type == "code":
                    story.append(Paragraph(html.escape(block.text).replace("\n", "<br/>"),
                                           styles["code"]))
                    first_para = True
            if options.chapter_new_page:
                story.append(PageBreak())

    if not options.chapter_new_page:
        story.append(PageBreak())

    stamp = (generated_at or datetime.now(UTC)).strftime("%d %B %Y")
    imprint = _end_imprint(options, stamp=stamp, title=title, subtitle=subtitle)
    if imprint:
        story += [
            Spacer(1, 40 * mm),
            Paragraph(html.escape(imprint).replace("\n", "<br/>"), styles["colophon"]),
        ]

    doc.multiBuild(story)
    return buffer.getvalue()


def export(
    plan: Plan,
    chapters: dict[int, dict],
    fmt: str,
    *,
    options: ExportOptions | None = None,
) -> tuple[bytes, str, str]:
    """Return (bytes, filename, content type)."""
    options = options or DEFAULT_EXPORT_OPTIONS
    base = slug(_display_title(plan, options))
    if fmt == "md":
        return to_markdown(plan, chapters, options=options).encode("utf-8"), f"{base}.md", "text/markdown; charset=utf-8"
    if fmt == "html":
        return to_html(plan, chapters, options=options).encode("utf-8"), f"{base}.html", "text/html; charset=utf-8"
    if fmt == "epub":
        return to_epub(plan, chapters, options=options), f"{base}.epub", "application/epub+zip"
    if fmt == "pdf":
        return to_pdf(plan, chapters, options=options), f"{base}.pdf", "application/pdf"
    raise ValueError(f"unknown format: {fmt}")
