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

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-bookwright.txt
export GROQ_API_KEY=gsk_...          # optional — omit to run in demo mode
uvicorn server:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>. No key? It runs the **demo provider**: a deterministic local
fixture that composes placeholder prose so the entire pipeline, streaming UI and exports can be
exercised offline. Demo text is templated on purpose — it is a fixture, not writing.

Docker:

```bash
docker build -t bookwright .
docker run -p 8000:8000 -e GROQ_API_KEY=gsk_... bookwright
```

### Using your own key from the browser

Press **API key** in the header. The key is held in `sessionStorage` for that tab, sent as an
`X-Groq-Key` header only with your own requests, and never written to disk or logged. Leave it
empty to stay in demo mode. If the server has `GROQ_API_KEY` set, that key is used when the browser
sends none.

---

## Layout

```
bookwright/
  config.py      model routing, length presets, limits — the only place models are named
  schema.py      Plan/Part/Chapter dataclasses + parse_plan() validation with repair
  prompts.py     every prompt, separated from transport so it can be reviewed
  llm.py         httpx client: SSE streaming, retry/backoff, usage accounting
  mock.py        offline fixture provider
  jobs.py        job state, event fan-out, the wave-based pipeline
  exporters.py   Markdown · HTML · EPUB 3 · PDF
server.py        FastAPI: JSON API, SSE stream, export endpoints
static/          dependency-free front-end (no build step, no CDN)
tests/           20 tests: schema, pipeline, cancel, continuity, every exporter
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
- Content is model output. The app estimates 6–9k words per book; quality depends entirely on the
  routed models, and claims should be verified before anyone relies on them.
- Demo-mode prose is fixture text and will read like fixture text. That is intentional.

## Development

```bash
pip install -r requirements-bookwright.txt pytest pymupdf ruff
BOOKWRIGHT_MOCK_DELAY=0 pytest -q      # 20 tests, no network, no key
ruff check bookwright server.py
```

## License

MIT, inherited from the original project (see [LICENSE.md](LICENSE.md)).
