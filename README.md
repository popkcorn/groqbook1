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

### Style controls — getting away from "AI voice"

Thirteen switches, each injecting a precise instruction into every chapter prompt
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

Five are on by default (stock phrases, not-just, hedging, recaps, uniform
rhythm). Below the switches is a free-text **house style** box (400 chars) for
anything the presets miss — British spelling, no rhetorical questions, name real
places, and so on.

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
  "extra_instructions": "British spelling, no rhetorical questions"
}'
```

## Layout

```
bookwright/
  config.py      model routing, length presets, limits — the only place models are named
  env.py         tiny .env reader, so `cp example.env .env` is enough to configure
  schema.py      Plan/Part/Chapter dataclasses + parse_plan() validation with repair
  prompts.py     every prompt, separated from transport so it can be reviewed
  llm.py         httpx client: SSE streaming, retry/backoff, usage accounting
  mock.py        offline fixture provider
  jobs.py        job state, event fan-out, the wave-based pipeline
  exporters.py   Markdown · HTML · EPUB 3 · PDF
server.py        FastAPI: JSON API, SSE stream, export endpoints
static/          dependency-free front-end (no build step, no CDN)
tests/           34 tests: schema, pipeline, cancel, continuity, env, style, exports
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
| `POST` | `/api/books` | Start a book; returns `id` + token estimate |
| `GET` | `/api/books/{id}` | Full snapshot (plan, chapters, stats) |
| `GET` | `/api/books/{id}/events` | **SSE** stream: `snapshot`, `status`, `plan`, `chapter_start`, `delta`, `chapter_written`, `chapter_done`, `done`, `error` |
| `POST` | `/api/books/{id}/cancel` | Stop; keeps what was written |
| `GET` | `/api/books/{id}/export/{md\|html\|epub\|pdf}` | Download |

```bash
curl -s -X POST localhost:8000/api/books -H 'content-type: application/json' \
  -d '{"topic":"why cities keep building systems nobody can maintain","length":"standard"}'
curl -N localhost:8000/api/books/<id>/events          # live progress
curl -OJ localhost:8000/api/books/<id>/export/pdf
```

Interactive docs: `/api/docs`.

---

## Reliability notes

- **Retries**: 429/5xx/transport errors are retried up to four times with jittered exponential
  backoff, honouring `Retry-After`. A stream is only retried if no token has been emitted —
  restarting mid-chapter would duplicate prose.
- **Truncation**: a chapter that stops on `finish_reason == "length"` is continued (up to twice),
  so long chapters do not end mid-sentence.
- **Plan validation**: the outline is checked (object shape, ≥2 chapters, ≤16 chapters, title
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
- Long books are slow by design: chapters run in waves of 3 and each wave waits for its
  summaries before the next starts. An Epic (20 chapters) is roughly seven waves.
- The continuity window carries 10 chapter summaries. Past ~14 chapters the earliest ones fall
  out of the bible, so very long books will drift; that is a model-context limit, not a bug.
- Demo-mode prose is fixture text and will read like fixture text. That is intentional.

## Development

```bash
pip install -r requirements-bookwright.txt pytest pymupdf ruff
BOOKWRIGHT_MOCK_DELAY=0 pytest -q      # 34 tests, no network, no key
ruff check bookwright server.py
```

## License

MIT, inherited from the original project (see [LICENSE.md](LICENSE.md)).
