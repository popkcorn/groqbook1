# Repository Analysis — `popkcorn/groqbook1`

**Analyzed:** branch `arena/2d950417-groqbook1` @ `f868a65` (identical to `origin/main`)
**Repo type:** Single-file Streamlit demo app (fork of `Bklieger/infinite-bookshelf`, formerly `Bklieger/groqbook`)
**Size:** 12 tracked files, ~4,300 LOC of which 340 are Python and ~3,900 are generated example output
**Verdict:** Clean, readable, well-scoped demo — but **non-functional today** (both Groq model IDs were shut down on 2025‑08‑30) and carrying several real bugs that would surface even with working credentials.

---

## 1. What this is

Groqbook generates a whole non‑fiction book from a one-line topic prompt:

1. **Outline pass** — `llama3-70b-8192` returns a nested JSON table of contents (sections/sub‑sections, with a short description per section).
2. **Content pass** — for every leaf section, `llama3-8b-8192` streams a "long, comprehensive, structured chapter" as markdown.
3. **Presentation** — markdown is streamed into live `st.empty()` placeholders chapter by chapter, with a rolling tokens/sec statistics table.
4. **Export** — the accumulated markdown is downloadable as `.txt` and rendered to PDF via `md2pdf`/WeasyPrint.

### Layout

| Path | Role |
|---|---|
| `main.py` (340 L) | Entire application: `GenerationStatistics`, `Book`, Groq calls, Streamlit UI |
| `requirements.txt` | Pinned dependency set (55 packages), pinning **transitive** deps too |
| `example.env` | One-line template for `GROQ_API_KEY` (never loaded — no `python-dotenv`) |
| `.devcontainer/devcontainer.json` | Python 3.11 image, installs deps, auto-runs Streamlit on port 8501 |
| `Example_1.md`, `Example_2.md` | Committed sample books (75 KB / 175 KB) — the only "test fixtures" |
| `assets/imgs/release_note_jun_8th.png` | 1.5 MB release screenshot |
| `README.md`, `LICENSE.md`, `packages.txt` | Docs, MIT license, APT packages (only `weasyprint`) |

### Architecture (as implemented)

```
st.form submit
  └─ generate_book_structure(topic)      # blocking, JSON mode, 8000 max_tokens
       └─ json.loads(...) → Book(json)   # builds contents{} + placeholders{}
            └─ stream_section_content(...)   # depth-first, SEQUENTIAL
                 └─ generate_section(title: description)  # streamed
                      └─ update_content() → placeholder.markdown(...)  # every token
                          └─ GenerationStatistics.add(usage) → stats table
  └─ export: .txt (BytesIO) | .pdf (md2pdf)
```

Everything runs synchronously on Streamlit's script thread, and all state lives in `st.session_state`.

---

## 2. Findings by severity

| # | Severity | Finding | Location |
|---|---|---|---|
| 1 | 🔴 Blocker | Both Groq model IDs are decommissioned → every request 400s | `main.py:18, 156, 181, 201, 297` |
| 2 | 🔴 Blocker | Unpinned `md2pdf` resolves to 3.x, whose API no longer accepts `md_content` → PDF export raises `TypeError` | `main.py:144`, `requirements.txt:55` |
| 3 | 🟠 High | Second generation in a session crashes: the new `Book` is built but never stored, so content is written into the *old* book's dicts → `KeyError` | `main.py:301-325` |
| 4 | 🟠 High | Download buttons are nested under a plain `st.button` → they disappear on click (classic Streamlit anti-pattern) | `main.py:235-254` |
| 5 | 🟠 High | No rate-limit/retry handling: a single 429 mid-book aborts the run and strands partial output | `main.py:179-206` (no `tenacity` use) |
| 6 | 🟡 Medium | Unbounded/flat model JSON: no schema validation, recursion depth guard, section cap, or timeout | `main.py:300-329` |
| 7 | 🟡 Medium | `Book.update_content` swallows `TypeError` silently, hiding real failures | `main.py:88-89` |
| 8 | 🟡 Medium | O(n²) re-render: full accumulated chapter is re-markdowned on **every token** | `main.py:87, 91-93` |
| 9 | 🟡 Medium | "Clear" button doesn't clear anything (no `session_state` reset), and is only reachable after an exception | `main.py:336-341` |
| 10 | 🟡 Medium | API key read at import time only; `.env`/`example.env` never loaded despite being shipped | `main.py:8-15` |
| 11 | 🟢 Low | Dead code / unused outputs: `empty_st()`, `large_model_generation_statistics`, `fpdf2`, `pandas`, `numpy`, `gitpython` | `main.py:231, 293`, `requirements.txt` |
| 12 | 🟢 Low | Model output and outline JSON printed to server stdout (log noise + content leak) | `main.py:307` |
| 13 | 🟢 Low | Repo hygiene: 1.5 MB PNG and ~250 KB of generated markdown in Git; `.gitignore` misses `__pycache__`, `.streamlit/secrets.toml`, `*.pdf` | `.gitignore` |
| 14 | 🟢 Low | README quickstart is broken: `export $GROQ_API_KEY = gsk_yA...` (invalid bash, spaces around `=`), and points to a hosted app that can no longer work | `README.md:52` |
| 15 | 🟢 Low | No tests, no CI, no linter config, no `pyproject.toml` | repo-wide |

---

## 3. Detailed findings

### 3.1 🔴 The app cannot run: models retired

`main.py` hardcodes `llama3-70b-8192` (outline) and `llama3-8b-8192` (chapters). Groq's deprecation history records:

- `llama3-70b-8192` → **shut down 2025-08-30**, successor `llama-3.3-70b-versatile`
- `llama3-8b-8192` → **shut down 2025-08-30**, successor `llama-3.1-8b-instant`
- Those successors were themselves **shut down 2026-08-16**; current recommendations are `openai/gpt-oss-120b` / `qwen/qwen3.6-27b` (large) and `openai/gpt-oss-20b` (small).

Reference: <https://console.groq.com/docs/deprecations>

Every call now returns `400 model_decommissioned`, which the blanket `except Exception` turns into a red error box on the page — so the app looks alive but does nothing. This is the single most important defect: nothing else matters until the model IDs (and the prompt style tuned for Llama 3) are updated.

Because the model names are also embedded in UI/statistics defaults (`GenerationStatistics.__init__`, `total_generation_statistics`), fixing this means centralizing model IDs into constants (ideally env-configurable), not sed-replacing five literals.

### 3.2 🔴 PDF export is broken by dependency drift

```python
pdf_buffer = BytesIO()
md2pdf(pdf_buffer, md_content=content)      # main.py:144
```

`requirements.txt` pins 53 packages to exact versions but leaves the two that matter for this feature unpinned (`weasyprint`, `md2pdf`). `md2pdf` on PyPI is now **3.1.1**, whose signature is:

```python
def md2pdf(pdf, raw=None, md=None, css=None, base_url=None, extras=None, ...)
```

There is no `md_content` kwarg, and the first argument is treated as an output **path**, not a buffer. So a fresh `pip install -r requirements.txt` gives `TypeError: md2pdf() got an unexpected keyword argument 'md_content'`. Even pinning `md2pdf==1.0.1` (the API this code was written against) is fragile: 1.x shells out through very old WeasyPrint internals.

**Recommendation:** drop `md2pdf` entirely. WeasyPrint directly is ~15 lines (`markdown.markdown(...)` → `HTML(string=...).write_pdf(target=buffer)`), removes a dead wrapper, and lets you pin `weasyprint` deliberately. Note WeasyPrint needs Cairo/Pango system libs — `packages.txt` only lists `weasyprint` (a PyPI name, not an apt package), so the APT step in the devcontainer installs nothing useful.

### 3.3 🟠 The second generation always crashes

```python
book = Book(book_structure_json)        # fresh placeholders + fresh contents{}
if 'book' not in st.session_state:      # only stored the FIRST time
    st.session_state.book = book
...
st.session_state.book.display_structure()          # old book's widgets
st.session_state.book.update_content(title, chunk) # old contents{} + new title
```

On the second run, `update_content` does `self.contents[title] += new_content` for titles that exist only in the *new* structure. That is a `KeyError`, **not** a `TypeError`, so it escapes `update_content`'s `except`, escapes the inner `except json.JSONDecodeError`, and lands in the outer `except Exception` — user sees a raw `'Some New Chapter'` error, generation stops, and the previously generated book is still in state.

Fix: always replace `st.session_state.book` (and reset `statistics_text`), or key books by a generation ID.

### 3.4 🟠 Download UX is broken

```python
if st.button('End Generation and Download Book'):
    ...
    st.download_button(label='Download Text', ...)
    st.download_button(label='Download PDF', ...)
```

`st.download_button` only exists in the script run triggered by the click. The moment the user clicks "Download", Streamlit reruns the script, the outer `st.button` is `False`, and the download widgets vanish — the file is not delivered. This is a documented Streamlit pitfall.

Fix: use a `st.session_state.show_downloads` flag toggled by the button and render the download buttons unconditionally when the flag is set, or just always render them (disabled/absent) once a book exists. Also `mime='text/plain'` for the PDF should be `application/pdf`.

### 3.5 🟠 No resilience around Groq calls

Chapters are generated **sequentially**, each up to 8,000 output tokens, with no retry, no backoff, no timeout, and no concurrency. On a free/dev tier this is very likely to hit TPM/TPD or request limits partway through a 20+ section book. A single `groq.RateLimitError` aborts everything — and because book state is only appended as tokens arrive, the failure leaves a partially filled book that is hard to resume.

`tenacity==8.3.0` is already pinned in `requirements.txt` but never used, which suggests this was intended. Minimal fix: exponential-backoff retry wrapper around each `generate_section`, plus 2–4 concurrent requests (`asyncio`/`ThreadPoolExecutor`) with a semaphore, streaming into per-section buffers.

### 3.6 🟡 Robustness of the outline contract

`json.loads` + `response_format={"type": "json_object"}` guarantees parseable JSON, not a *valid structure*:

- A top-level list, or values that are neither `str` nor `dict`, silently produce an empty/partial `Book` (`flatten_structure` only recurses into dicts).
- Deep nesting → unbounded recursion in `flatten_structure`, `display_toc`, `get_markdown_content`, `stream_section_content`.
- No cap on section count → a 60-section outline turns into 60 × 8k-token calls and enormous cost/latency.
- No `max_tokens` guard on total book size; long books can exceed Streamlit's memory/download comfort zone.

Recommend a small validator (dict check, depth ≤ 3, leaf count ≤ N, each description ≤ M chars) plus a friendly error.

### 3.7 🟡 Smaller code-quality issues

- **Silent failure** — `except TypeError: pass` in `update_content` (line 88) hides genuine errors. It should not catch anything, or should surface the problem.
- **O(n²) UI work** — `display_content` re-renders `"## {title}" + full accumulated text` for every streamed token. For a 6k-token chapter this is thousands of full markdown re-parses. Throttle to ~10 Hz or accumulate and render once per second.
- **Statistics dead path** — `large_model_generation_statistics` (outline timing/tokens) is computed then never displayed (the display code is commented out at lines 293–295). Only the chapter stats are shown, and `GenerationStatistics.add` never merges the outline stats, so the reported T/s excludes the 70B call.
- **`total_time` divide** — `str()` handles `total_time == 0`, but plain division in `get_output_speed` is fine; however if the SDK returns `None` for the timing fields (newer SDKs are not guaranteed to populate `prompt_time`/`completion_time`), `add()` raises `TypeError`. Guard with `or 0`.
- **Import-time side effects** — lines 10–15 and 211–218 mutate `st.session_state` at module import; `Groq()` is instantiated at import when the env var exists. Harmless in Streamlit, but it makes the module untestable and means `GROQ_API_KEY` changes after import are ignored.
- **Unused helpers** — `empty_st()` (line 231) and `button_text` (always `"Generate"`).
- **"Clear" doesn't clear** — `st.rerun()` only reruns; nothing is popped from `session_state`, so the error/partial book returns.

### 3.8 🟢 Security review

| Aspect | Assessment |
|---|---|
| Secret handling | Key can be entered in a password widget; but if a user puts it in `.streamlit/secrets.toml`, `.gitignore` does **not** exclude it → accidental commit risk. `example.env` contains a fake key, no real leak. |
| XSS / HTML injection | `st.markdown(..., unsafe_allow_html=False)` everywhere → model output is sanitized for the web UI. ✅ |
| PDF injection | Model-generated markdown is rendered by WeasyPrint, which resolves external images/CSS. A prompt-injected `![](http://attacker/...)` becomes an outbound request from the server (SSRF-ish, also leaks generation timing). Low practical risk, worth a `url_fetcher` that blocks non-local schemes. |
| Injection into Groq | User topic is interpolated into the prompt inside `<subject>` tags; prompt injection only affects the user's own output. No cross-tenant risk. |
| Logging | `print(json.dumps(book_structure_json))` writes outline content to server logs. |
| Supply chain | 55 exact pins, two unpinned (`weasyprint`, `md2pdf`) — the unpinned ones are the ones that break. No hashes. |
| Auth model | Bring-your-own-key, no server-side secrets, no user data stored. Good default posture for a demo. |

### 3.9 ✅ What's actually good

- The two-model strategy (big model for structure, small for prose) is a sensible, cheap-and-fast design and is clearly explained in the README.
- `Book` cleanly separates structure, content, and rendering; `flatten_structure` / `get_markdown_content` are tidy and readable.
- Streamed token display + live T/s statistics is a genuinely nice touch and gives real perceived speed.
- Statistics math and markdown table formatting are correct and defensive about division by zero.
- 340-line single file with no framework sprawl; easy to read, easy to fork, MIT licensed, docs are honest about the fiction-quality limitation.

---

## 4. Dependencies & environment

- **Pinned transitive closure** (55 packages) means the environment can't receive security patches and will break on Python ≥ 3.13 (`numpy==1.26.4` has no 3.13 wheels). Pinning is defensible for a reproducible demo, but should be done from a lockfile (`uv.lock` / `pip-tools`), with `requirements.in` listing only direct deps.
- **Direct deps actually used:** `streamlit`, `groq`, `md2pdf` (+ implicitly `weasyprint`), stdlib. `pandas`, `numpy`, `altair`, `pyarrow`, `GitPython`, `fpdf2`, `pydyf`, `protobuf` are Streamlit/leftover baggage — `fpdf2` and `GitPython` are pure leftovers.
- **`groq==0.6.0`** (2024) vs current 1.7.0. The code relies on `usage.prompt_time` / `completion_time` / `total_time` and `chunk.x_groq.usage`, which are Groq-specific extensions rather than OpenAI-compatible standard fields; verify these still exist before upgrading the SDK.
- **Devcontainer**: installs `requirements.txt` + streamlit on Python 3.11 and runs with `--server.enableCORS false --server.enableXsrfProtection false`. Turning XSRF protection **off** is fine for a personal Codespace but should not be copied into any shared deployment, and CORS should be configured explicitly rather than disabled.
- `packages.txt` (Streamlit Cloud APT hook) contains `weasyprint` with no newline — that is a PyPI name, not an APT package; the correct set for WeasyPrint is `libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0` (plus `fonts-*` for PDF typography).

---

## 5. Prioritized remediation plan

**P0 — make it run (hours)**
1. Extract model IDs to constants/env with working defaults (`openai/gpt-oss-120b` for outline, `openai/gpt-oss-20b` for chapters), re-tune the outline prompt/JSON mode for the new models.
2. Replace `md2pdf` with direct WeasyPrint, or pin `md2pdf==1.0.1`; add correct APT packages for Pango/Cairo.
3. Add a startup guard that validates the key with a cheap request and shows a clear message instead of a stack trace.

**P1 — correctness (a day)**
4. Always rebuild `st.session_state.book` (and reset stats) on a new generation.
5. Fix the download flow (`session_state` flag / always-rendered download buttons; `application/pdf` mime).
6. Wrap per-section calls in retry/backoff; add a concurrency limit; make partial progress resumable.
7. Validate the outline JSON (dict, depth, leaf count) before building the book.
8. Make "Clear" actually reset `session_state`.

**P2 — quality (ongoing)**
9. Split `main.py` into `groqbook/{llm,book,export,ui}.py`, add `if __name__ == "__main__"`-style entrypoint or a `run()` function.
10. Throttle streaming re-renders; move the generation loop off the script thread (`st.status` + `asyncio`).
11. Add `requirements.in` + lockfile, drop unused packages, add `ruff` + `pytest` + a tiny test that feeds a fixture outline into `Book`.
12. Fix README quickstart (`export GROQ_API_KEY=gsk_...`, `streamlit run main.py`), document that the hosted demo no longer works, add a model-configuration section.
13. `.gitignore`: `__pycache__/`, `.streamlit/secrets.toml`, `*.pdf`, `generated_book.*`. Consider moving the sample books/screenshot to a `docs/` folder or Git LFS, or trimming them.

---

## 6. Bottom line

A well-written, well-documented **2024-era demo** that has aged out of working order: the models it targets were retired 14 months ago, and one of its two headline features (PDF export) is broken by unpinned dependencies. The core design remains sound; bringing it back to life is a focused P0/P1 effort (model swap, PDF renderer swap, session-state fix, retries) rather than a rewrite. The biggest gaps relative to a "real" app are robustness around rate limits and the complete absence of tests/CI.

---

## 7. Successor: `bookwright/`

This analysis drove a rebuild rather than a patch — see [README.md](README.md). The new system
(`bookwright/` + `server.py` + `static/`) keeps the good parts of the original design (two-tier
model routing, streamed output, live throughput stats) and addresses findings 1–13 above:

| Finding | How the successor handles it |
|---|---|
| 1 retired models | Model IDs live in `bookwright/config.py`, env-overridable, default to current GPT-OSS IDs |
| 2 broken PDF | No `md2pdf`, no WeasyPrint, no system libs — ReportLab with embedded DejaVu fonts |
| 3 second-run crash | New `Book` state per job; jobs are independent and cancellable |
| 4 download UX | Plain HTTP endpoints, built on demand, correct MIME types |
| 5 no retries | Bounded retry/backoff on 429/5xx, mid-stream restart guard, continuation on truncation |
| 6 outline contract | `parse_plan()` validates depth/count/duplicates and feeds the error back for one repair |
| 7 silent failures | Failed chapters are surfaced per chapter; the book completes as `partial` |
| 8 O(n²) rendering | Client renders at most every 130 ms; server streams deltas |
| 9 no reset | Explicit cancel + new-book reset |
| 10 import side effects | Nothing runs at import time; key comes per request or from env |
| 11 dead code | Only five direct dependencies; no unused modules |
| 12 log leak | Outline is no longer printed to stdout |
| 13 hygiene | `.gitignore` covers `__pycache__`, secrets, generated exports |
