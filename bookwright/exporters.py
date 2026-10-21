"""Export: Markdown, HTML (print-ready), EPUB 3 and PDF.

The PDF path deliberately avoids WeasyPrint/md2pdf — those need Pango/Cairo and
a pinned wrapper API, the exact combination that broke in the original app.
ReportLab is pure Python plus embedded fonts, so a PDF always builds.
"""

from __future__ import annotations

import html
import io
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .schema import Plan

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
def to_markdown(plan: Plan, chapters: dict[int, dict]) -> str:
    lines = [f"# {plan.title}", "", f"*{plan.subtitle}*", ""]
    if plan.thesis:
        lines += [f"> {plan.thesis}", ""]
    lines += ["---", ""]
    for part in plan.parts:
        lines += [f"## {part.title}", ""]
        if part.summary:
            lines += [f"*{part.summary}*", ""]
        for chapter in part.chapters:
            lines += [f"### Chapter {chapter.index}. {chapter.title}", ""]
            lines += [(chapters.get(chapter.index, {}).get("text") or "").strip(), ""]
    return "\n".join(lines).strip() + "\n"


HTML_CSS = """
:root { --ink:#17130f; --muted:#6b5f52; --rule:#d9cfc0; --paper:#fdfaf4; }
* { box-sizing: border-box; }
html { background:#efe7d9; }
body { margin:0; color:var(--ink); background:var(--paper);
  font-family:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  font-size:11.4pt; line-height:1.62; text-rendering:optimizeLegibility; }
.page { max-width:34em; margin:0 auto; padding:4rem 2.2rem 6rem; }
.cover { min-height:80vh; display:flex; flex-direction:column; justify-content:center;
  page-break-after:always; break-after:page; }
.cover .kicker { font-family:ui-sans-serif,system-ui,sans-serif; font-size:.7rem;
  letter-spacing:.22em; text-transform:uppercase; color:var(--muted); margin-bottom:2.5rem; }
.cover h1 { font-size:2.9rem; line-height:1.08; margin:0 0 1rem; font-weight:600;
  letter-spacing:-.01em; }
.cover .sub { font-size:1.15rem; color:var(--muted); font-style:italic; margin:0 0 3rem; }
.cover .rule { width:4rem; height:2px; background:var(--ink); margin:0 0 1.4rem; }
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
.part-title { page-break-before:always; break-before:page; padding-top:22vh; }
.part-title h3 { font-size:1.9rem; margin:0 0 .8rem; font-weight:600; }
.part-title p { color:var(--muted); font-style:italic; max-width:24em; }
.chapter { page-break-before:always; break-before:page; }
.chapter .ch-head { margin-bottom:2rem; }
.chapter .ch-num { font-family:ui-sans-serif,system-ui,sans-serif; font-size:.7rem;
  letter-spacing:.2em; text-transform:uppercase; color:var(--muted); }
.chapter h2 { font-size:1.75rem; line-height:1.2; margin:.4rem 0 0; font-weight:600; }
.chapter p { margin:0 0 0; text-align:justify; hyphens:auto; }
.chapter p + p, .chapter ul + p, .chapter blockquote + p { text-indent:1.4em; margin-top:0; }
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


def to_html(plan: Plan, chapters: dict[int, dict], *, generated_at: datetime | None = None) -> str:
    stamp = (generated_at or datetime.now(UTC)).strftime("%d %B %Y")
    toc = ['<nav class="toc" id="contents"><h2>Contents</h2><ol>']
    for part in plan.parts:
        toc.append(f'<li class="part"><a href="#part-{part.index}">{html.escape(part.title)}</a>')
        toc.append("<ol>")
        for chapter in part.chapters:
            toc.append(
                f'<li class="ch"><a href="#ch-{chapter.index}">'
                f'<span>{chapter.index:02d}</span>'
                f"<span>{html.escape(chapter.title)}</span></a></li>"
            )
        toc.append("</ol></li>")
    toc.append("</ol></nav>")

    body: list[str] = []
    for part in plan.parts:
        body.append(
            f'<section class="part-title" id="part-{part.index}"><h2>Part {part.index}</h2>'
            f"<h3>{html.escape(part.title)}</h3>"
            + (f"<p>{html.escape(part.summary)}</p>" if part.summary else "")
            + "</section>"
        )
        for chapter in part.chapters:
            text = (chapters.get(chapter.index, {}).get("text") or "").strip()
            body.append(
                f'<article class="chapter" id="ch-{chapter.index}">'
                f'<header class="ch-head"><div class="ch-num">Chapter {chapter.index}</div>'
                f"<h2>{html.escape(chapter.title)}</h2></header>"
                f"{blocks_to_html(parse_markdown(text))}</article>"
            )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(plan.title)}</title>
<style>{HTML_CSS}</style></head>
<body><div class="page">
<section class="cover">
  <div class="kicker">A book written with Bookwright</div>
  <div class="rule"></div>
  <h1>{html.escape(plan.title)}</h1>
  <p class="sub">{html.escape(plan.subtitle)}</p>
  <div class="meta">{plan.audience} · {len(plan.chapters)} chapters · {stamp}</div>
  {f'<p class="thesis">{html.escape(plan.thesis)}</p>' if plan.thesis else ''}
</section>
{''.join(toc)}
{''.join(body)}
<section class="colophon"><p>Set in DejaVu Serif. Generated {stamp} from a plan by
Bookwright's architect stage. Everything in this book is model output and should be
read as such.</p></section>
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
.title { text-align: center; margin-top: 30%; }
.title h1 { font-size: 2em; }
.title .sub { text-indent: 0; font-style: italic; color: #555; }
.title .meta { text-indent: 0; margin-top: 3em; font-family: sans-serif;
  font-size: .75em; letter-spacing: .12em; text-transform: uppercase; color: #777; }
"""

XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head><meta charset="utf-8"/><title>{title}</title>
<link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>{body}</body></html>"""


def to_epub(plan: Plan, chapters: dict[int, dict]) -> bytes:
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, plan.title + plan.subtitle)}"
    modified = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    docs: list[tuple[str, str, str]] = []  # (id, filename, xhtml)
    title_body = (
        f'<div class="title"><h1>{html.escape(plan.title)}</h1>'
        f'<p class="sub">{html.escape(plan.subtitle)}</p>'
        f'<p class="meta">{html.escape(plan.audience)}</p></div>'
    )
    docs.append(("titlepage", "title.xhtml", XHTML.format(title="Title", body=title_body)))

    toc_items: list[str] = []
    for part in plan.parts:
        part_body = (
            f"<h1>{html.escape(part.title)}</h1>"
            + (f"<p>{html.escape(part.summary)}</p>" if part.summary else "")
        )
        name = f"part{part.index}.xhtml"
        docs.append((f"part{part.index}", name, XHTML.format(title=part.title, body=part_body)))
        toc_items.append(f'<li><a href="{name}">{html.escape(part.title)}</a><ol>')
        for chapter in part.chapters:
            text = (chapters.get(chapter.index, {}).get("text") or "").strip()
            body = (
                f"<h1>{html.escape(chapter.title)}</h1>"
                f"{blocks_to_html(parse_markdown(text))}"
            )
            ch_name = f"ch{chapter.index:02d}.xhtml"
            docs.append(
                (f"ch{chapter.index:02d}", ch_name, XHTML.format(title=chapter.title, body=body))
            )
            toc_items.append(f'<li><a href="{ch_name}">{html.escape(chapter.title)}</a></li>')
        toc_items.append("</ol></li>")

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
    <dc:title>{html.escape(plan.title)}</dc:title>
    <dc:language>en</dc:language>
    <dc:creator>Bookwright</dc:creator>
    <dc:description>{html.escape(plan.thesis or plan.subtitle)}</dc:description>
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
        zf.writestr("OEBPS/style.css", EPUB_CSS, compress_type=zipfile.ZIP_DEFLATED)
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


def to_pdf(plan: Plan, chapters: dict[int, dict], *, generated_at: datetime | None = None) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
    from reportlab.lib.pagesizes import A5
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

    styles = {
        "cover_kicker": ParagraphStyle(
            "CoverKicker", fontName=sans, fontSize=7.5, textColor=colors.HexColor("#6b5f52"),
            leading=12, spaceAfter=18,
        ),
        "cover_title": ParagraphStyle(
            "CoverTitle", fontName=sans_bold, fontSize=27, leading=31,
            textColor=colors.HexColor("#17130f"), spaceAfter=10,
        ),
        "cover_sub": ParagraphStyle(
            "CoverSub", fontName=serif, fontSize=11.5, leading=16,
            textColor=colors.HexColor("#6b5f52"), spaceAfter=26,
        ),
        "cover_meta": ParagraphStyle(
            "CoverMeta", fontName=sans, fontSize=7.5, leading=12,
            textColor=colors.HexColor("#6b5f52"),
        ),
        "cover_thesis": ParagraphStyle(
            "CoverThesis", fontName=serif, fontSize=9.5, leading=14,
            textColor=colors.HexColor("#6b5f52"), spaceBefore=34,
        ),
        "part_label": ParagraphStyle(
            "PartLabel", fontName=sans, fontSize=7.5, leading=12, spaceAfter=10,
            textColor=colors.HexColor("#6b5f52"),
        ),
        "part_title": ParagraphStyle(
            "PartTitle", fontName=sans_bold, fontSize=17, leading=22, spaceAfter=10,
            textColor=colors.HexColor("#17130f"),
        ),
        "part_summary": ParagraphStyle(
            "PartSummary", fontName=serif, fontSize=9.5, leading=14,
            textColor=colors.HexColor("#6b5f52"),
        ),
        "chapter_label": ParagraphStyle(
            "ChapterLabel", fontName=sans, fontSize=7, leading=10, spaceAfter=4,
            textColor=colors.HexColor("#6b5f52"),
        ),
        "chapter_title": ParagraphStyle(
            "ChapterTitle", fontName=sans_bold, fontSize=15, leading=19, spaceAfter=14,
            textColor=colors.HexColor("#17130f"),
        ),
        "body_first": ParagraphStyle(
            "BodyFirst", fontName=serif, fontSize=9.6, leading=14.4, alignment=TA_JUSTIFY,
            spaceAfter=0, firstLineIndent=0,
        ),
        "body": ParagraphStyle(
            "Body", fontName=serif, fontSize=9.6, leading=14.4, alignment=TA_JUSTIFY,
            firstLineIndent=4.2 * mm,
        ),
        "h2": ParagraphStyle(
            "H2", fontName=sans_bold, fontSize=8.2, leading=12, spaceBefore=11,
            spaceAfter=5, textColor=colors.HexColor("#3b3128"),
        ),
        "bullet": ParagraphStyle(
            "Bullet", fontName=serif, fontSize=9.6, leading=14, leftIndent=5 * mm,
            bulletIndent=1 * mm, spaceBefore=2,
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

    page_w, page_h = A5
    margin_x, margin_top, margin_bottom = 17 * mm, 19 * mm, 20 * mm
    book_title = plan.title.upper()

    class BookDoc(BaseDocTemplate):
        def __init__(self, target):
            super().__init__(
                target, pagesize=A5, leftMargin=margin_x, rightMargin=margin_x,
                topMargin=margin_top, bottomMargin=margin_bottom,
                title=plan.title, author="Bookwright", subject=plan.subtitle,
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
            canvas.saveState()
            canvas.setFont(sans, 6.6)
            canvas.setFillColor(colors.HexColor("#8a7d6c"))
            canvas.drawString(margin_x, page_h - margin_top + 6 * mm, book_title)
            canvas.drawRightString(page_w - margin_x, page_h - margin_top + 6 * mm,
                                   "BOOKWRIGHT")
            canvas.setStrokeColor(colors.HexColor("#d9cfc0"))
            canvas.setLineWidth(0.4)
            canvas.line(margin_x, page_h - margin_top + 4.5 * mm,
                        page_w - margin_x, page_h - margin_top + 4.5 * mm)
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

    # Cover (drawn on the bare template, then we switch to the decorated one)
    story += [
        Spacer(1, 38 * mm),
        Paragraph("A BOOK WRITTEN WITH BOOKWRIGHT", styles["cover_kicker"]),
        HRFlowable(width=28 * mm, thickness=1.2, color=colors.HexColor("#17130f"),
                   spaceBefore=0, spaceAfter=16),
        Paragraph(html.escape(plan.title), styles["cover_title"]),
        Paragraph(html.escape(plan.subtitle), styles["cover_sub"]),
        HRFlowable(width=12 * mm, thickness=0.6, color=colors.HexColor("#c8963e"),
                   spaceBefore=2, spaceAfter=14),
        Paragraph(
            html.escape(
                f"{plan.audience} · {len(plan.chapters)} chapters · "
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
            Spacer(1, 34 * mm),
            Paragraph(f"PART {part.index}", styles["part_label"]),
            Paragraph(html.escape(part.title), styles["part_title"]),
        ]
        if part.summary:
            story.append(Paragraph(html.escape(part.summary), styles["part_summary"]))
        story.append(PageBreak())

        for chapter in part.chapters:
            text = (chapters.get(chapter.index, {}).get("text") or "").strip()
            story += [
                Paragraph(f"CHAPTER {chapter.index}", styles["chapter_label"]),
                Paragraph(html.escape(chapter.title), styles["chapter_title"]),
            ]
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
            story.append(PageBreak())

    stamp = (generated_at or datetime.now(UTC)).strftime("%d %B %Y")
    story += [
        Spacer(1, 40 * mm),
        Paragraph(f"{html.escape(plan.title)} — {html.escape(plan.subtitle)}", styles["toc_part"]),
        Spacer(1, 4 * mm),
        Paragraph(
            f"Generated on {stamp} by Bookwright: an architect model planned the book, "
            "a writer model drafted each chapter against a running continuity bible, and "
            "an editor model summarised what had been written. Text is model output; "
            "verify any claim before relying on it.",
            styles["colophon"],
        ),
    ]

    doc.multiBuild(story)
    return buffer.getvalue()


def export(plan: Plan, chapters: dict[int, dict], fmt: str) -> tuple[bytes, str, str]:
    """Return (bytes, filename, content type)."""
    base = slug(plan.title)
    if fmt == "md":
        return to_markdown(plan, chapters).encode("utf-8"), f"{base}.md", "text/markdown; charset=utf-8"
    if fmt == "html":
        return to_html(plan, chapters).encode("utf-8"), f"{base}.html", "text/html; charset=utf-8"
    if fmt == "epub":
        return to_epub(plan, chapters), f"{base}.epub", "application/epub+zip"
    if fmt == "pdf":
        return to_pdf(plan, chapters), f"{base}.pdf", "application/pdf"
    raise ValueError(f"unknown format: {fmt}")
