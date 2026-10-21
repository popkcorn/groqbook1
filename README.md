# Bookwright

**A staged book-writing model.** Give it a sentence about a subject; it designs the argument,
writes the chapters, keeps them from contradicting each other, and typesets the result as
Markdown, HTML, EPUB or PDF.

Built as a successor to [Groqbook](README.groqbook.md) — the original one-file app in `main.py` —
whose two hardcoded models were retired in August 2025. See [ANALYSIS.md](ANALYSIS.md) for what
was wrong with it and which of those faults this design deliberately fixes.

```
topic ──▶ ARCHITECT ──▶ plan (parts, chapters, beats)
                              │
                              ▼
             ┌──────────── WRITER ◀── continuity bible ◀── EDITOR ┐
             │   chapter 1..N drafted in waves, streamed live      │
             └────────────────────────────────────────────────────┘
                              │
                              ▼
                        COMPOSER ──▶ .md · .html · .epub · .pdf
```

---

## Why "a model" and not "a prompt"

Groqbook asked one model, once, for each chapter, with only the section's title in context. That
is a prompt, not a system. Bookwright is a **model in the systems sense**: typed stages with
contracts between them, a shared state that accumulates, and an explicit editing pass. The four
roles are separately routable — a big model plans, a cheap fast one drafts, the cheapest one keeps
notes.

| Stage | Job | Default model | Env override |
|---|---|---|---|
| **Architect** | Emits a JSON plan: parts → chapters → purpose → beats → glossary terms | `openai/gpt-oss-120b` | `BOOKWRIGHT_ARCHITECT_MODEL` |
| **Writer** | Drafts one chapter in Markdown against the plan **and** the bible | `openai/gpt-oss-20b` | `BOOKWRIGHT_WRITER_MODEL` |
| **Editor** | After each chapter: two-sentence summary + terms introduced | `openai/gpt-oss-20b` | `BOOKWRIGHT_EDITOR_MODEL` |
| **Composer** | Typeset and export; no model involved | — | — |

Model IDs are **never hardcoded at a call site** — they live in `bookwright/config.py` and are
env-overridable, because Groq retired the IDs this project's ancestor used within fourteen months.

### The continuity mechanism

Each chapter prompt receives a *bible*: the thesis, the reader, the voice, a glossary that grows as
terms are introduced, and one-line summaries of the chapters already written. Chapters are drafted
in **waves** of `CONCURRENCY` so that each wave can fold its summaries into the bible before the
next wave starts. That is deliberate: full parallelism would be faster and incoherent. A retry or a
cancellation never loses what was already written.

### Choosing Groq models

Expand **Groq models** in the app and load the catalog with your API key. You can select an
Architect model (plans the argument and outline), Writer model (drafts every chapter), and Editor
model (summaries plus the optional style pass) independently. The catalog is fetched from Groq for
your account and shows the owner, context window, maximum completion tokens, and active status when
Groq provides them. Model selection changes capability, prose style, speed, and token consumption;
larger context/output limits do not guarantee better writing and may use more TPM. Choices are saved
as model IDs with the book; the API key is request-scoped and is never stored in the job. With no key,
the app uses fixed demo prose and model selection is disabled.

The API accepts `architect_model`, `writer_model`, and `editor_model` in `POST /api/books`; omit them
to use the `.env` defaults above. `GET /api/models` returns the live account catalog.

---

## Run it on your own PC

You need **Python 3.10 or newer** and a Groq API key (free at
<https://console.groq.com/keys>). No key? It still runs in demo mode.

### Windows

1. Install Python from <https://www.python.org/downloads/windows/> and **tick
   "Add python.exe to PATH"** in the installer.
2. Download this repo (**Code → Download ZIP**, then extract) or clone it.
3. Double-click **`start-windows.bat`**.

On first run it creates a `.venv` folder and installs the dependencies (about a minute).
Then it opens <http://localhost:8000> in your browser.

### macOS / Linux

```bash
git clone https://github.com/popkcorn/groqbook1.git
cd groqbook1
bash start-mac-linux.sh
```

`bash` is used deliberately: ZIP downloads lose the executable bit, so
`./start-mac-linux.sh` would fail with "permission denied" (fix: `chmod +x start-mac-linux.sh`).

### Any OS, manually

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-bookwright.txt
python run.py
```

### Getting the files

- **Merged into `main`?** Green **Code** button → **Download ZIP**.
- **In the pull request?** Download the branch directly:
  `https://github.com/popkcorn/groqbook1/archive/refs/heads/arena/2d950417-groqbook1.zip`
- **Releases page:** <https://github.com/popkcorn/groqbook1/releases> has a
  `bookwright-source.zip` with no `.git` folder.

Extract anywhere (Desktop is fine) and run the start script from inside the extracted
`groqbook1-*` folder. Do not run it from inside the ZIP viewer.

### Adding your API key

Copy `example.env` to `.env` next to `run.py` and paste your key in:

```
GROQ_API_KEY=gsk_your_key_here
```

That is all — `run.py` loads `.env` automatically, no shell exports needed. A key
exported in your shell overrides the file. You can also leave `.env` empty and paste the
key in the browser via **API key** in the header (it stays in that tab only).

### Launcher options

```bash
python run.py                  # http://127.0.0.1:8000, opens your browser
python run.py --port 9000      # different port
python run.py --reload         # restart on file changes (development)
python run.py --host 0.0.0.0   # reachable from other devices on your LAN
python run.py --no-browser     # do not open a browser
python run.py --install        # install missing dependencies automatically
```

### If something goes wrong

| Symptom | Fix |
|---|---|
| `Python was not found` (Windows) | Reinstall Python with "Add python.exe to PATH" ticked |
| `error: externally-managed-environment` | Use a virtual environment (the `start-*` scripts do this for you) |
| `port 8000 is already in use` | Another copy is running, or use `--port 8001` |
| Browser shows demo prose | No key was found — check `.env` or use the **API key** button |
| `401 Unauthorized` | The key is wrong or expired; regenerate it at console.groq.com |
| `404` mentioning a model | Groq retired that model — check `console.groq.com/docs/deprecations`, then set `BOOKWRIGHT_WRITER_MODEL` in `.env` |

Docker, if you prefer containers:

```bash
docker build -t bookwright .
docker run -p 8000:8000 -e GROQ_API_KEY=gsk_... bookwright
```

## Lengths, voices and style controls

### Length

| Preset | Chapters | Words/chapter | Total |
|---|---|---|---|
| Chapbook | 4 | 420 | ~1,700 |
| Standard | 6 | 650 | ~3,900 |
| Deep dive | 8 | 900 | ~7,200 |
| Folio | 10 | 1,000 | ~10,000 |
| Novella | 12 | 1,200 | ~14,400 |
| Tome | 14 | 1,400 | ~19,600 |
| Epic | 20 | 1,500 | ~30,000 |
| **Custom** | 2–20 | 250–2,000 | up to ~40,000 |

The writer's token budget scales with the requested chapter length
(`config.writer_tokens_for`), so long chapters finish in one call instead of
tripping the completion ceiling and needing continuation.

### Voices

Twelve, grouped in the picker: **plain** (clear, conversational, minimal),
**literary** (narrative, lyrical, playful), **argument** (rigorous, journalistic,
editorial, socratic) and **specialist** (technical, satirical). The voice shapes
the architect's plan as well as the prose.

### Ground the book in your material

The book form has a **Notes & voice** section for the things a one-sentence brief
cannot supply:

- **Facts, notes and sources** (up to 4,000 chars): paste the dates, figures,
  names, case studies or references the book should actually use. The architect
  sees them while planning; the writer carries them in the continuity bible for
  every chapter. The prompt says to avoid unsupported specifics and qualify
  unverified claims — but this is grounding, not a fact-checker. Check the output.
- **Voice sample** (up to 1,200 chars): a paragraph of your own writing whose
  cadence and diction the model should approximate. Its subject and sentences
  are explicitly not to be reused. Only submit text you have the right to use.
- **Point of view**: Auto, I, we, you, or third person. If a style switch
  contradicts the chosen person (for example, “no I” with first person), the
  conflicting rule is disabled rather than asking the writer to do both.

Both notes and the voice sample are included in the API's conservative token
estimate. Longer references improve grounding but also add input tokens to every
chapter.

### Approve the outline before drafting

**Review the outline before writing** is on by default in the browser. The
architect returns the title, parts, chapter purposes and concrete beats; the
book pauses so you can retitle chapters, rewrite purposes and beats, add a
chapter, or remove one. Approving starts the writer. Every edit is re-validated
by the server, including the two-chapter minimum and 20-chapter ceiling. If you
turn the option off, the book writes as soon as the plan passes validation.

This is the cheapest point to correct a weak structure. A division whose purpose
is wrong is much harder to repair after several thousand words exist.

### Name and organize the divisions

The **Book structure** controls let you call each major division a chapter,
section, essay, or entry, and its parent group a part, book, or volume. Choose
the group count (or keep it automatic) and internal-heading density, from
continuous prose with no subheadings to a more detailed hierarchy. A 2,000-word unit is a short chapter or essay—not a
paragraph; the label describes its role, while the word target controls length.
The underlying plan/API keeps the existing `parts` and `chapters` fields for
compatibility.

### Style controls — getting away from "AI voice"

Twenty-four switches, each injecting a precise instruction into every chapter prompt
*and* into the continuity bible, so the constraint survives across waves rather
than drifting after chapter three:

| Rule | What it forbids |
|---|---|
| Stock phrases | *delve, tapestry, testament to, navigate the complexities, it is worth noting…* |
| "Not just X, but Y" | The `it is not just X, it is Y` and `not only… but also` scaffolds |
| Rule-of-three lists | Three-item lists used as rhythm filler |
| Em-dash tics | More than one em dash per chapter |
| "In a world where…" openers | Formulaic openings and rhetorical-question hooks |
| Hedging | *arguably, it can be argued, some might say* |
| Chapter recaps | *In this chapter we will*, *as we have seen*, closing summaries |
| Uniform rhythm | Three paragraphs or sentences of identical shape |
| Grand claims | Sweeping statements about humanity or the future |
| Question headings | Section headings phrased as questions |
| Bullet-point prose | Lists that should have been paragraphs |
| No "you" | Second-person address |
| No "I" | First-person anecdote |
| Intensifiers | *really, very, quite, absolutely* used as filler |
| Corporate verbs | *leverage, facilitate, optimise, utilise, streamline* |
| Superlatives | *the most important, the greatest, one of the best* |
| Expletives | *there is / there are / it is* openings that bury the actor |
| "The fact that" | The whole `due to the fact that` family |
| Wordy connectives | *in order to, in terms of, with regard to* |
| Rhetorical questions | "Why does this keep happening?" as a paragraph hook |
| Anaphora | Three consecutive sentences opening on the same word |
| Adverb density | Adverbs over ~4% of a chapter's words |
| Flourish metaphors | *a journey, a tapestry, a beacon, an ecosystem of…* |
| Your banned list | Any word or phrase you type into *Banned phrases* |

Five are on by default (stock phrases, not-just, hedging, recaps, uniform
rhythm). Below the switches is a free-text **house style** box (400 chars) for
anything the presets miss — British spelling, no rhetorical questions, name real
places, and so on.

#### Editorial profiles

One click sets the switches, the intensity and the house-style line together;
touching any of them afterwards flips the selection to **Custom**, so the chip
never claims credit for rules you turned off.

| Profile | Rules | Intensity | Aimed at |
|---|---|---|---|
| Anti-AI — strict | 21 | aggressive | Default; maximum pattern removal |
| Plain and tight | 9 | standard | Short sentences, one idea per paragraph |
| Literary | 7 | gentle | Flourish allowed, tics still banned |
| Academic | 9 | standard | Hedges kept, hype removed |
| Journalistic | 9 | standard | Short paragraphs, attribution over claims |
| Minimal | 12 | aggressive | Only the most obvious machine tells |
| Custom | your picks | standard | Starting point for hand-tuning |

#### Banned phrases

Up to 25 phrases (60 chars each), one per line or comma separated. They are
enforced twice: quoted into every chapter prompt as an outright ban, and matched
case-insensitively by the scanner, so the chapter fails the check if any survive.
This is the escape hatch for house rules no preset covers — a competitor's name,
your editor's pet hates, a word that has become a cliché in your field.

#### Intensity

| Setting | Acts when | Rewrite budget | Typical cost |
|---|---|---|---|
| Gentle | 5+ flags in a chapter | 2 chunks per chapter | cheapest; leaves light tics alone |
| Standard | 2+ flags | 4 chunks per chapter | default |
| Aggressive | any flag | no cap | most calls, best odds of reaching zero |

Everything is passed as data (`config.ANTI_PATTERNS`) so the API, the UI and the
tests agree on the same catalogue; unknown rule ids are rejected with a 422
rather than silently ignored.

```bash
# same controls from the API
curl -X POST localhost:8000/api/books -H 'content-type: application/json' -d '{
  "topic": "why maintenance is the real design problem",
  "tone": "journalistic", "length": "custom",
  "chapters": 9, "words_per_chapter": 1200,
  "directives": ["cliches", "not_just", "em_dash", "no_second_person"],
  "extra_instructions": "British spelling, no rhetorical questions",
  "profile": "academic",
  "banned_phrases": ["synergy", "paradigm shift"],
  "stylist": true, "stylist_strength": "aggressive"
}'
```

## The de-AI editorial pass

Prompt instructions reduce machine-flavoured prose but cannot verify it. So after a chapter is
drafted, a fourth stage measures it and rewrites only what still breaks the rules.

```
draft ──▶ SCAN ──▶ rewrite flagged passages ──▶ RE-SCAN ──▶ keep or discard ──▶ diff
         (regex +     (editor model,             (same rules)   (three guards)
          structure)   chunk by chunk)
```

**Scan** (`bookwright/style.py`) is deliberately literal. Each active rule has a detector that
matches something nameable — stock phrases, em dashes, hedges, recaps — and reports the offending
excerpts. Three checks are computed rather than matched: runs of four same-length sentences,
bullet-point prose over eight items, and adverb density over a chapter's worth of words.

**Rewrite** only touches chunks that contain a flag, so a clean chapter costs nothing. Chunks are
split on paragraph boundaries (~5,000 chars) and capped by the intensity setting (2 / 4 / unlimited
per chapter).

**Three guards** decide whether the result ships:

| Guard | Rule | On failure |
|---|---|---|
| Length | result within 60–160% of the draft | `stylist_rejected`, draft kept |
| Effect | re-scan shows fewer flags than before | `stylist_rejected`, draft kept |
| Survival | any error in the editor call | draft kept, warning logged |

A chapter is never lost to the editorial pass, and the draft is retained as `original` so the UI can
show exactly what changed. Detection also only polices the rules **you enabled** — turning off
"Stock phrases" both stops the instruction and stops the check.

In the app: **Editorial de-AI pass** toggles it (on by default), with `Gentle` / `Standard` /
`Aggressive` intensity. Each edited chapter shows a *"7 de-AI fixes"* badge, a **See changes**
button that opens a paragraph-level before/after diff (unchanged paragraphs hidden), and **Undo
edits**, which puts that one chapter's pre-editorial draft back without touching the rest.

### Regenerate one finished chapter

After the book reaches **Done** or **Partial**, expand **Regenerate this chapter** on a completed
chapter, describe the change in plain language, and submit it. The rewrite is scoped to that chapter:
other chapter text is left untouched, and later chapters are not automatically rewritten. The command
is sent to the Writer with the chapter, book bible, style, point of view, and optional voice sample.
The browser shows **Undo last regeneration**; up to five prior versions (including summaries and
editorial metadata) are retained for that chapter only.

Custom regeneration requires a Groq key, supplied either by the existing **API key** control (`X-Groq-Key`)
or `GROQ_API_KEY` on the server. Demo fixture prose is intentionally fixed and cannot be regenerated.
The key is request-scoped and is never stored in the book job. A failed provider call leaves the current
chapter unchanged. The instruction is limited to 1,200 characters.

#### The style report

When a book finishes, the app reports what the editorial stage actually achieved:

- **patterns left** and **fixed by the editor** — residue and total reduction, book-wide;
- a bar per remaining rule, so you can see which tic survived;
- readability: Flesch reading ease, grade level, mean sentence length, adverbs per 100 words.

It is computed by re-running the same detectors over the finished text, so it cannot flatter the
editor — a rule that still fires still shows up. `GET /api/books/{id}/style-report` returns the
same numbers as JSON.

```bash
curl -X POST localhost:8000/api/books -H 'content-type: application/json' -d '{
  "topic": "why maintenance is the real design problem",
  "length": "standard",
  "stylist": true, "stylist_strength": "aggressive"
}'
```

Cost: one extra editor-model call per flagged chunk, usually one per chapter. `gentle` skips
chapters with fewer than five flags, which is most of them on a capable model.

## Layout

```
bookwright/
  config.py      model routing, length presets, limits — the only place models are named
  env.py         tiny .env reader, so `cp example.env .env` is enough to configure
  schema.py      Plan/Part/Chapter dataclasses + parse_plan() validation with repair
  prompts.py     every prompt, separated from transport so it can be reviewed
  llm.py         httpx client: SSE streaming, retry/backoff, usage accounting
  mock.py        offline fixture provider
  jobs.py        job state, event fan-out, the wave-based pipeline, the de-AI stage
  style.py       pattern detectors, chunking, paragraph diff for the UI
  exporters.py   Markdown · HTML · EPUB 3 · PDF
server.py        FastAPI: JSON API, SSE stream, export endpoints
static/          dependency-free front-end (no build step, no CDN)
tests/           schema, pipeline, cancel, continuity, env, style, exports, UI-facing options
run.py           standalone launcher: checks deps, loads .env, opens the browser
start-windows.bat / start-mac-linux.sh
                 double-click / one-command setup for a fresh machine
main.py          the original Groqbook app, unchanged, for reference
```

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Mode (`live`/`demo`) and the routed model IDs |
| `GET` | `/api/presets` | Lengths, tones, audiences, limits |
| `GET` | `/api/models` | Models available to the request's Groq key, with owner and token limits |
| `POST` | `/api/books` | Start a book; returns `id` + token estimate |
| `GET` | `/api/books/{id}` | Full snapshot (plan, chapters, stats) |
| `GET` | `/api/books/{id}/events` | **SSE** stream: `snapshot`, `status`, `plan`, `chapter_start`, `delta`, `chapter_written`, `stylist_start`, `stylist_done`, `stylist_rejected`, `chapter_reverted`, `chapter_regenerated`, `chapter_regeneration_undone`, `chapter_drafted`, `chapter_done`, `outline_review`, `done`, `error` |
| `GET` | `/api/books/{id}/style-report` | Residue per rule, flags fixed, readability |
| `POST` | `/api/books/{id}/chapters/{n}/revert` | Undo the editorial edits on one chapter |
| `POST` | `/api/books/{id}/chapters/{n}/regenerate` | Regenerate one finished chapter; JSON `{"instruction":"..."}`, Groq key required |
| `POST` | `/api/books/{id}/chapters/{n}/undo-regeneration` | Restore the chapter version before its latest regeneration |
| `POST` | `/api/books/{id}/outline` | Validate and approve an edited plan; resumes the parked writer |
| `POST` | `/api/books/{id}/cancel` | Stop; keeps what was written |
| `GET` | `/api/books/{id}/export/{md\|html\|epub\|pdf}` | Download |

```bash
curl -s -X POST localhost:8000/api/books -H 'content-type: application/json' \
  -d '{"topic":"why cities keep building systems nobody can maintain","length":"standard"}'
curl -N localhost:8000/api/books/<id>/events          # live progress
curl -OJ localhost:8000/api/books/<id>/export/pdf
```

### Export layout controls

After a book has text, the Export panel lets you set the division/group labels,
heading format (`Chapter 1`, `1.`, or title only), exported title and subtitle,
and whether to include a title page or subtitle. PDF running-header text (left
and right) and page numbers are controlled independently. Layout controls include
page size (A5, A4, Letter), margins, body font size (9–14 pt), line spacing,
paragraph indents versus spacing, alignment, and a page break per major division.
These presentation settings do not change the generated prose. Page size and
pagination apply to PDF and print-ready HTML. EPUB readers control pagination;
EPUB honors typography and heading choices. Markdown uses the selected division
heading style and includes the exported title.

The settings are also available as query parameters, for example:

```text
/api/books/<id>/export/pdf?page_size=letter&margins=wide&chapter_heading=number&unit_label=essay&part_label=book&title=Sample&subtitle=An%20overview&body_font_size=12&line_spacing=airy&paragraph_style=space&alignment=left&chapter_new_page=false&running_matter=true&page_numbers=false&header_left=Sample&header_right=AUTHOR&show_title_page=false&show_subtitle=false
```

For grounded or reviewed books, `POST /api/books` also accepts `notes`,
`voice_sample`, `pov` (`auto`, `first`, `we`, `you`, `third`), `review_outline`,
`unit_label` (`chapter`, `section`, `essay`, `entry`), `part_label` (`part`,
`book`, `volume`), `group_count` (1–6, optional), and `subheading_style`
(`none`, `few`, `standard`, `detailed`). Browser review defaults on; API clients opt in with
`"review_outline": true`. The outline endpoint accepts the architect's plan
shape with your edits; it does not accept prose.

Interactive docs: `/api/docs`.

---

## Reliability notes

- **Retries**: 429/5xx/transport errors are retried up to four times with jittered exponential
  backoff. Rate-limit retries honor `Retry-After` and Groq's reset delay in the error message (for
  example, a 24-second TPM reset); streams retry only before the first token to avoid duplicate prose.
- **Truncation**: a chapter that stops on `finish_reason == "length"` is continued (up to twice),
  so long chapters do not end mid-sentence.
- **Plan validation**: the outline is checked (object shape, ≥2 chapters, ≤20 chapters, title
  uniqueness, string normalisation) and the specific failure is fed back to the architect for one
  repair attempt before the job errors.
- **Partial failure**: one failed chapter does not sink the book; the job finishes as `partial`
  and the export includes everything that succeeded.
- **Backpressure**: SSE subscribers have bounded queues; a slow reader gets a `snapshot` resync
  rather than a silent gap.
- **Exports are pure Python**: ReportLab with embedded DejaVu fonts. No Pango/Cairo, no `md2pdf`
  shim — the exact stack that broke PDF export in the original app.

## Limits

- In-memory job store (24 books, 3 concurrent); restarting the server drops them.
- No auth or per-user quota. Run it for yourself or behind your own gateway.
- Content is model output. Quality depends entirely on the routed models, and claims should be
  verified before anyone relies on them.
- The style report counts pattern residue, not quality. Readability scores reward short sentences;
  prose that scores well can still be empty.
- The de-AI pass fixes surface patterns, not substance. It cannot add evidence, fix a wrong claim,
  or improve an argument — by design, it is forbidden from rewriting anything but flagged sentences.
- Long books are slow by design: chapters run in waves of 2 by default, and each wave waits for
  summaries before the next starts. Set `BOOKWRIGHT_CONCURRENCY=1` in `.env` if your Groq TPM limit
  is especially low; restart the app after changing it. An Epic (20 chapters) is roughly ten waves.
- The continuity window carries 10 chapter summaries. Past ~14 chapters the earliest ones fall
  out of the bible, so very long books will drift; that is a model-context limit, not a bug.
- Demo-mode prose is fixture text and will read like fixture text. That is intentional.

## Development

```bash
pip install -r requirements-bookwright.txt pytest pymupdf ruff
BOOKWRIGHT_MOCK_DELAY=0 pytest -q      # no network, no key
ruff check bookwright server.py tests run.py
```

## License

MIT, inherited from the original project (see [LICENSE.md](LICENSE.md)).
