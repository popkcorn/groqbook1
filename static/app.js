/* Bookwright front-end. Vanilla JS, no build step, no CDN. */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const state = {
    key: sessionStorage.getItem("bookwright.key") || "",
    mode: "demo",
    length: "standard",
    directives: [],
    preset: null,
    job: null,
    plan: null,
    chapters: new Map(), // index -> { text, status, title, node, bodyNode, words }
    rendering: new Set(),
    stream: null,
    follow: true,
    startedAt: 0,
  };

  const L = {
    md: ["Markdown", ".md"],
    html: ["HTML", ".html"],
    epub: ["EPUB", ".epub"],
    pdf: ["PDF", ".pdf"],
  };

  // --------------------------------------------------------------- markdown
  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function inline(s) {
    return esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s.,;:!?)]|$)/g, "$1<em>$2</em>")
      .replace(/(^|[\s(])_([^_\n]+)_(?=[\s.,;:!?)]|$)/g, "$1<em>$2</em>");
  }

  function mdToHtml(md) {
    const lines = md.replace(/\r\n/g, "\n").split("\n");
    let html = "";
    let para = [];
    let list = [];
    let quote = [];
    let code = [];
    let inCode = false;

    const flushPara = () => {
      if (para.length) {
        html += `<p>${inline(para.join(" "))}</p>`;
        para = [];
      }
    };
    const flushList = () => {
      if (list.length) {
        html += `<ul>${list.map((i) => `<li>${inline(i)}</li>`).join("")}</ul>`;
        list = [];
      }
    };
    const flushQuote = () => {
      if (quote.length) {
        html += `<blockquote><p>${inline(quote.join(" "))}</p></blockquote>`;
        quote = [];
      }
    };

    for (const raw of lines) {
      const line = raw.replace(/\s+$/, "");
      if (line.startsWith("```")) {
        if (inCode) {
          html += `<pre><code>${esc(code.join("\n"))}</code></pre>`;
          code = [];
          inCode = false;
        } else {
          flushPara(); flushList(); flushQuote();
          inCode = true;
        }
        continue;
      }
      if (inCode) { code.push(line); continue; }

      if (!line.trim()) { flushPara(); flushList(); flushQuote(); continue; }

      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        flushPara(); flushList(); flushQuote();
        const level = Math.min(Math.max(heading[1].length, 2), 4);
        html += `<h${level}>${inline(heading[2])}</h${level}>`;
        continue;
      }
      if (/^\s*[-*]\s+/.test(line)) {
        flushPara(); flushQuote();
        list.push(line.replace(/^\s*[-*]\s+/, ""));
        continue;
      }
      if (/^\s*>\s?/.test(line)) {
        flushPara(); flushList();
        quote.push(line.replace(/^\s*>\s?/, ""));
        continue;
      }
      if (list.length) flushList();
      if (quote.length) flushQuote();
      para.push(line.trim());
    }
    flushPara(); flushList(); flushQuote();
    if (inCode && code.length) html += `<pre><code>${esc(code.join("\n"))}</code></pre>`;
    return html;
  }

  // ------------------------------------------------------------------ utils
  const nf = new Intl.NumberFormat("en-US");
  const compact = (n) =>
    n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/\.0$/, "") + "k" : nf.format(n);
  const duration = (s) => {
    s = Math.max(0, Math.round(s));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const r = s % 60;
    return m < 60 ? `${m}m ${String(r).padStart(2, "0")}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
  };

  let toastTimer = null;
  function toast(message, ms = 6000) {
    const node = $("#toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, ms);
  }

  async function api(path, options = {}) {
    const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
    if (state.key) headers["X-Groq-Key"] = state.key;
    const res = await fetch(path, { ...options, headers });
    if (!res.ok) {
      let detail = `request failed (${res.status})`;
      try {
        const body = await res.json();
        detail = body.error || body.detail || detail;
        if (typeof detail !== "string") detail = JSON.stringify(detail);
        if (Array.isArray(body.detail)) detail = body.detail.map((d) => d.msg).join("; ");
      } catch (_) { /* non-JSON error */ }
      throw new Error(detail);
    }
    return res.json();
  }

  // ------------------------------------------------------------- boot / form
  async function boot() {
    try {
      const [presets, health] = await Promise.all([api("/api/presets"), api("/api/health")]);
      state.mode = state.key ? "live" : health.mode;
      setMode(state.mode, health.models);
      fillForm(presets);
    } catch (err) {
      toast(`Could not reach the server: ${err.message}`);
    }
  }

  function setMode(mode, models) {
    const badge = $("#mode-badge");
    badge.textContent = mode === "live" ? "live" : "demo";
    badge.classList.toggle("live", mode === "live");
    badge.title =
      mode === "live"
        ? models
          ? `Groq · ${models.architect} + ${models.writer}`
          : "Groq API"
        : "Demo mode: a local fixture provider writes placeholder prose. Add a key for real text.";
  }

  function fillForm(presets) {
    const audience = $("#audience");
    presets.audiences.forEach((name) => audience.add(new Option(name, name)));

    const tone = $("#tone");
    presets.tones.forEach((t) => tone.add(new Option(t.id[0].toUpperCase() + t.id.slice(1), t.id)));
    tone.value = presets.defaults.tone;

    const chips = $("#lengths");
    presets.lengths.forEach((preset) => {
      const chip = el("button", "chip");
      chip.type = "button";
      chip.setAttribute("role", "radio");
      chip.dataset.id = preset.id;
      chip.append(el("b", "", preset.label), el("span", "", preset.blurb));
      chip.addEventListener("click", () => {
        state.length = preset.id;
        [...chips.children].forEach((c) => c.setAttribute("aria-checked", String(c === chip)));
        $("#custom-length").hidden = preset.id !== "custom";
        updateEstimate(presets);
      });
      chip.setAttribute("aria-checked", String(preset.id === presets.defaults.length));
      chips.append(chip);
    });
    state.length = presets.defaults.length;
    state.presets = presets;

    // Custom length inputs
    const chap = $("#c-chapters");
    const words = $("#c-words");
    const [minCh, maxCh] = presets.limits.chapters_range;
    const [minW, maxW] = presets.limits.words_range;
    chap.min = minCh; chap.max = maxCh;
    words.min = minW; words.max = maxW;
    const syncCustom = () => {
      chap.value = Math.min(Math.max(Number(chap.value) || minCh, minCh), maxCh);
      words.value = Math.min(Math.max(Number(words.value) || minW, minW), maxW);
      const total = Number(chap.value) * Number(words.value);
      $("#custom-summary").textContent =
        `${chap.value} chapters · ~${nf.format(total)} words`;
      updateEstimate(presets);
    };
    chap.addEventListener("input", syncCustom);
    words.addEventListener("input", syncCustom);

    // Style toggles
    const toggles = $("#anti-patterns");
    const defaults = new Set(presets.default_directives || []);
    presets.anti_patterns.forEach((item) => {
      const label = el("label", "toggle");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = item.id;
      input.checked = defaults.has(item.id);
      const text = el("span", "t-label", item.label);
      text.append(el("span", "t-desc", item.description));
      label.append(input, text);
      input.addEventListener("change", () => {
        state.directives = [...toggles.querySelectorAll("input:checked")].map((i) => i.value);
        paintStyleCount();
        updateEstimate(presets);
      });
      toggles.append(label);
    });
    state.directives = [...toggles.querySelectorAll("input:checked")].map((i) => i.value);
    paintStyleCount();

    const extra = $("#extra-instructions");
    extra.addEventListener("input", () => {
      $("#extra-count").textContent = extra.value.length;
      updateEstimate(presets);
    });

    updateEstimate(presets);
  }

  function currentLength(presets) {
    const p = presets.lengths.find((x) => x.id === state.length) || presets.lengths[1];
    if (p.id !== "custom") return p;
    const chapters = Number($("#c-chapters").value) || p.chapters;
    const words = Number($("#c-words").value) || p.words_per_chapter;
    return { ...p, chapters, words_per_chapter: words, total_words: chapters * words };
  }

  function updateEstimate(presets) {
    const p = currentLength(presets);
    const styleBits = [];
    if (state.directives.length) styleBits.push(`${state.directives.length} style rules`);
    const extra = $("#extra-instructions").value.trim();
    if (extra) styleBits.push("house style");
    const suffix = styleBits.length ? ` · ${styleBits.join(" + ")}` : "";
    const words = compact(p.total_words);
    $("#estimate").textContent = state.key
      ? `Live mode · ${p.chapters} chapters, about ${words} words of output${suffix}.`
      : `Demo mode · ${p.chapters} chapters of fixture prose${suffix}. Add a Groq key for real writing.`;
  }

  function paintStyleCount() {
    const n = state.directives.length;
    $("#style-count").textContent = n ? `${n} active` : "all off";
  }

  $("#topic").addEventListener("input", (e) => {
    $("#topic-count").textContent = e.target.value.length;
  });

  document.querySelectorAll("[data-topic]").forEach((btn) => {
    btn.addEventListener("click", () => {
      $("#topic").value = btn.dataset.topic;
      $("#topic-count").textContent = btn.dataset.topic.length;
      $("#topic").focus();
    });
  });

  $("#brief").addEventListener("submit", async (event) => {
    event.preventDefault();
    const topic = $("#topic").value.trim();
    if (topic.length < 8) {
      toast("Give the architect at least 8 characters of subject.");
      return;
    }
    const button = $("#go");
    button.disabled = true;
    resetReader();
    try {
      const res = await api("/api/books", {
        method: "POST",
        body: JSON.stringify(buildPayload(topic)),
      });
      state.job = res.id;
      state.mode = res.mode;
      setMode(res.mode, res.models);
      state.startedAt = performance.now() / 1000;
      $("#run").hidden = false;
      $("#again").hidden = true;
      $("#cancel").hidden = false;
      $("#export").hidden = true;
      $("#toc").hidden = true;
      setStatus(res.mode === "live" ? "Queued on Groq" : "Queued in demo mode");
      setStats({ chapters_done: 0, chapters_total: res.estimate.chapters, tokens: 0, words: 0 });
      connect(res.id);
    } catch (err) {
      toast(err.message);
      button.disabled = false;
    }
  });

  function buildPayload(topic) {
    const payload = {
      topic,
      audience: $("#audience").value,
      tone: $("#tone").value,
      length: state.length,
      directives: state.directives,
      extra_instructions: $("#extra-instructions").value.trim(),
    };
    if (state.length === "custom") {
      payload.chapters = Number($("#c-chapters").value);
      payload.words_per_chapter = Number($("#c-words").value);
    }
    return payload;
  }

  $("#again").addEventListener("click", () => {
    resetReader();
    $("#run").hidden = true;
    $("#export").hidden = true;
    $("#toc").hidden = true;
    $("#topic").focus();
  });

  $("#cancel").addEventListener("click", async () => {
    if (!state.job) return;
    try { await api(`/api/books/${state.job}/cancel`, { method: "POST" }); } catch (err) { toast(err.message); }
  });

  // -------------------------------------------------------------------- SSE
  function connect(jobId) {
    if (typeof EventSource === "undefined") {
      setStatus("This browser cannot stream live progress");
      toast("Writing continues on the server, but this browser has no EventSource support. Reload in a while to check the result.", 12000);
      markRunDone("error");
      return;
    }
    if (state.stream) state.stream.close();
    const stream = new EventSource(`/api/books/${jobId}/events`);
    state.stream = stream;

    stream.addEventListener("snapshot", (e) => {
      const snap = JSON.parse(e.data);
      applySnapshot(snap);
      if (["done", "partial", "error", "cancelled"].includes(snap.status)) closeStream(false);
    });

    stream.addEventListener("status", (e) => {
      const data = JSON.parse(e.data);
      if (data.message) setStatus(data.message);
    });

    stream.addEventListener("plan", (e) => {
      const data = JSON.parse(e.data);
      renderPlan(data.plan);
      setStatus("Plan accepted — drafting chapters");
    });

    stream.addEventListener("chapter_start", (e) => {
      const data = JSON.parse(e.data);
      ensureChapter(data.index, data.title);
      setChapterStatus(data.index, "writing");
      setStatus(`Writing chapter ${data.index}: ${data.title}`);
    });

    stream.addEventListener("delta", (e) => {
      const data = JSON.parse(e.data);
      const chapter = state.chapters.get(data.index);
      if (!chapter) return;
      chapter.text += data.text;
      chapter.status = "writing";
      scheduleRender(data.index);
      scheduleStats();
      followScroll();
    });

    stream.addEventListener("chapter_written", (e) => {
      const data = JSON.parse(e.data);
      setChapterStatus(data.index, "done", data.words);
      renderChapter(data.index, true);
      applyStats(data.stats);
    });

    stream.addEventListener("chapter_done", (e) => {
      const data = JSON.parse(e.data);
      applyStats(data.stats);
      const chapter = state.chapters.get(data.index);
      if (chapter && data.summary) chapter.summary = data.summary;
      paintToc();
    });

    stream.addEventListener("chapter_error", (e) => {
      const data = JSON.parse(e.data);
      setChapterStatus(data.index, "failed");
      toast(`Chapter ${data.index} failed: ${data.message}`, 9000);
      paintToc();
    });

    stream.addEventListener("stats", (e) => {
      applyStats(JSON.parse(e.data).stats);
    });

    stream.addEventListener("done", (e) => {
      const data = JSON.parse(e.data);
      const label =
        data.status === "partial"
          ? `Finished with ${data.failed.length} failed chapter${data.failed.length === 1 ? "" : "s"}`
          : `Book written in ${duration(data.elapsed)}`;
      setStatus(label);
      markRunDone(data.status === "partial" ? "error" : "done");
      applyStats(data.stats);
      closeStream();
      paintToc();
    });

    stream.addEventListener("error", (e) => {
      if (e.data) {
        const data = JSON.parse(e.data);
        setStatus("Generation failed");
        toast(data.message || "Generation failed", 12000);
        markRunDone("error");
      }
      closeStream();
    });

    stream.addEventListener("cancelled", () => {
      setStatus("Stopped — what was written is kept");
      markRunDone("error");
      closeStream();
    });

    stream.addEventListener("resync", () => { /* snapshot follows from the server */ });
  }

  function closeStream(reset = true) {
    if (state.stream) {
      state.stream.close();
      state.stream = null;
    }
    $("#cancel").hidden = true;
    $("#again").hidden = false;
    $("#go").disabled = false;
    updateExports();
  }

  // ------------------------------------------------------------------ state
  function resetReader() {
    if (state.stream) { state.stream.close(); state.stream = null; }
    state.job = null;
    state.plan = null;
    state.chapters.clear();
    state.follow = true;
    $("#book").hidden = true;
    $("#book").innerHTML = "";
    $("#empty").hidden = false;
    $("#toc-list").innerHTML = "";
    $("#rail span").style.width = "0%";
    $("#meter-fill").style.width = "0%";
    markRunDone(null);
  }

  function applySnapshot(snap) {
    if (snap.plan) renderPlan(snap.plan, snap.chapters);
    for (const chapter of snap.chapters || []) {
      ensureChapter(chapter.index, chapter.title);
      const local = state.chapters.get(chapter.index);
      if (chapter.text.length >= local.text.length) {
        local.text = chapter.text;
        local.status = chapter.status;
        local.summary = chapter.summary || "";
        renderChapter(chapter.index, true);
      }
      paintToc();
    }
    applyStats(snap.stats);
    if (snap.status === "error" && snap.error) {
      setStatus("Generation failed");
      toast(snap.error, 12000);
      markRunDone("error");
    } else if (snap.status === "done" || snap.status === "partial") {
      setStatus(snap.status === "partial" ? "Finished (partial)" : "Book written");
      markRunDone(snap.status === "partial" ? "error" : "done");
    } else if (snap.status === "cancelled") {
      setStatus("Stopped");
      markRunDone("error");
    }
  }

  function ensureChapter(index, title) {
    if (state.chapters.has(index)) {
      if (title) state.chapters.get(index).title = title;
      return state.chapters.get(index);
    }
    const chapter = { index, title: title || `Chapter ${index}`, text: "", status: "pending", summary: "", words: 0, node: null, bodyNode: null };
    state.chapters.set(index, chapter);
    return chapter;
  }

  function setChapterStatus(index, status, words) {
    const chapter = state.chapters.get(index);
    if (!chapter) return;
    chapter.status = status;
    if (words) chapter.words = words;
    const node = chapter.node;
    if (node) {
      node.classList.toggle("writing", status === "writing");
      node.classList.toggle("done", status === "done");
      node.classList.toggle("failed", status === "failed");
      node.classList.toggle("pending", status === "pending");
      const flag = node.querySelector(".flag");
      if (flag) {
        if (status === "failed") flag.textContent = "generation failed";
        else if (words) flag.textContent = `${nf.format(words)} words`;
        else flag.textContent = "";
      }
    }
    paintToc();
  }

  // ---------------------------------------------------------------- render
  function renderPlan(plan, chapters) {
    state.plan = plan;
    $("#empty").hidden = true;
    const book = $("#book");
    book.hidden = false;
    book.innerHTML = "";

    const cover = el("header", "cover");
    cover.append(
      el("p", "cover-kicker", "A Bookwright book"),
      (() => { const h = el("h2", "", plan.title); return h; })(),
      el("p", "sub", plan.subtitle || ""),
      el("div", "rule"),
      el("p", "meta", `${plan.audience} · ${plan.chapter_count} chapters · ${compact(plan.total_words)} words`)
    );
    const styleSummary = $("#style-panel") ? state.directives.length : 0;
    if (styleSummary) {
      cover.append(el("p", "cover-style", `${styleSummary} style rules applied`));
    }
    if (plan.thesis) cover.append(el("p", "thesis", plan.thesis));
    book.append(cover);

    for (const part of plan.parts) {
      const divider = el("section", "part-divider");
      divider.append(el("div", "num", `Part ${part.index}`), el("h3", "", part.title));
      if (part.summary) divider.append(el("p", "", part.summary));
      book.append(divider);

      for (const spec of part.chapters) {
        const chapter = ensureChapter(spec.index, spec.title);
        const article = el("article", "chapter pending");
        article.id = `chapter-${spec.index}`;
        const head = el("header");
        head.append(el("div", "ch-num", `Chapter ${spec.index}`), el("h3", "", spec.title));
        const body = el("div", "body");
        body.innerHTML = '<div class="skeleton w80"></div><div class="skeleton"></div><div class="skeleton w60"></div>';
        const foot = el("div", "ch-foot");
        foot.append(el("span", "flag", ""), el("span", "sep"));
        article.append(head, body, foot);
        book.append(article);

        chapter.node = article;
        chapter.bodyNode = body;
        chapter.footNode = foot;
        chapter.status = chapter.status || "pending";
        if (chapter.text) renderChapter(spec.index, true);
      }
    }

    $("#toc").hidden = false;
    paintToc();

    if (chapters && chapters.length) {
      for (const item of chapters) {
        const chapter = ensureChapter(item.index, item.title);
        if (item.text && item.text.length > chapter.text.length) chapter.text = item.text;
      }
    }
  }

  function scheduleRender(index) {
    if (state.rendering.has(index)) return;
    state.rendering.add(index);
    setTimeout(() => {
      state.rendering.delete(index);
      renderChapter(index);
    }, 130);
  }

  function renderChapter(index, final = false) {
    const chapter = state.chapters.get(index);
    if (!chapter || !chapter.bodyNode) return;
    const html = mdToHtml(chapter.text);
    const caret = final || chapter.status !== "writing" ? "" : '<span class="caret"></span>';
    chapter.bodyNode.innerHTML = html + caret;
    chapter.node?.classList.toggle("pending", false);
  }

  function paintToc() {
    if (!state.plan) return;
    const list = $("#toc-list");
    const existing = new Map([...list.children].map((li) => [li.dataset.key, li]));
    const frag = document.createDocumentFragment();
    for (const part of state.plan.parts) {
      const partId = `part-${part.index}`;
      let li = existing.get(partId);
      if (!li) {
        li = el("li", "part", part.title);
        li.dataset.key = partId;
      }
      frag.append(li);
      for (const spec of part.chapters) {
        const key = `ch-${spec.index}`;
        const chapter = state.chapters.get(spec.index);
        let item = existing.get(key);
        if (!item) {
          item = el("li", "ch");
          item.dataset.key = key;
          item.append(el("span", "n", String(spec.index)), el("span", "t", spec.title), el("span", "dot"));
          item.addEventListener("click", () => {
            document.getElementById(`chapter-${spec.index}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
          });
        }
        const status = chapter ? chapter.status : "pending";
        item.className = `ch ${status}`;
        frag.append(item);
      }
    }
    list.replaceChildren(frag);
  }

  function setStatus(text) {
    $("#status").textContent = text;
  }

  function markRunDone(kind) {
    const run = $("#run");
    run.classList.toggle("done", kind === "done");
    run.classList.toggle("error", kind === "error");
  }

  // ---------------------------------------------------------------- stats
  let statsQueued = false;
  function scheduleStats() {
    if (statsQueued) return;
    statsQueued = true;
    setTimeout(() => {
      statsQueued = false;
      const done = [...state.chapters.values()].filter((c) => c.status === "done").length;
      const words = [...state.chapters.values()].reduce(
        (sum, c) => sum + (c.words || c.text.split(/\s+/).filter(Boolean).length), 0);
      $("#s-words").textContent = compact(words);
      $("#s-chapters").textContent = `${done}/${state.plan ? state.plan.chapter_count : "?"}`;
      const total = state.plan ? state.plan.chapter_count : 1;
      const pct = Math.min(100, Math.round((done / total) * 100));
      $("#meter-fill").style.width = `${pct}%`;
      $("#rail span").style.width = `${pct}%`;
      $("#s-time").textContent = duration(performance.now() / 1000 - state.startedAt);
    }, 220);
  }

  function applyStats(stats) {
    if (!stats) return;
    if (stats.chapters_total) {
      $("#s-chapters").textContent = `${stats.chapters_done || 0}/${stats.chapters_total}`;
    }
    if (stats.total_tokens !== undefined) $("#s-tokens").textContent = compact(stats.total_tokens);
    if (stats.tokens_per_second) $("#s-speed").textContent = `${Math.round(stats.tokens_per_second)} t/s`;
    if (stats.elapsed !== undefined) $("#s-time").textContent = duration(stats.elapsed);
    scheduleStats();
  }

  function setStats(stats) {
    $("#s-chapters").textContent = `${stats.chapters_done || 0}/${stats.chapters_total || 0}`;
    $("#s-tokens").textContent = compact(stats.tokens || stats.total_tokens || 0);
    $("#s-words").textContent = "0";
    $("#s-speed").textContent = "—";
    $("#s-time").textContent = "0s";
  }

  // ---------------------------------------------------------------- export
  function updateExports() {
    const grid = $("#export-grid");
    const hasText = [...state.chapters.values()].some((c) => c.status === "done" || c.text.trim());
    if (!state.job || !hasText) {
      $("#export").hidden = true;
      return;
    }
    $("#export").hidden = false;
    if (grid.children.length) return;
    for (const [fmt, [label, ext]] of Object.entries(L)) {
      const button = el("button", "", label);
      button.type = "button";
      button.append(el("small", "", ext));
      button.addEventListener("click", async () => {
        button.disabled = true;
        const original = button.firstChild.textContent;
        button.firstChild.textContent = "building…";
        try {
          const res = await fetch(`/api/books/${state.job}/export/${fmt}`);
          if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.error || `export failed (${res.status})`);
          }
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = `${slugify(state.plan?.title || "book")}.${fmt}`;
          document.body.append(a);
          a.click();
          a.remove();
          URL.revokeObjectURL(url);
        } catch (err) {
          toast(err.message, 9000);
        } finally {
          button.disabled = false;
          button.firstChild.textContent = original;
        }
      });
      grid.append(button);
    }
  }

  const slugify = (s) =>
    s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "book";

  // ------------------------------------------------------------------ misc
  function followScroll() {
    if (!state.follow) return;
    const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 240;
    if (nearBottom) return;
    if (window.scrollY > 200) window.scrollTo({ top: document.body.scrollHeight, behavior: "auto" });
  }

  window.addEventListener("scroll", () => {
    const atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 260;
    state.follow = atBottom;
  }, { passive: true });

  // key modal
  const openKey = () => {
    $("#key-input").value = state.key;
    $("#key-hint").textContent = state.key
      ? "A key is stored in this tab. It stays in session storage."
      : "Leave empty to stay in demo mode.";
    $("#scrim").hidden = false;
    $("#key-modal").hidden = false;
    $("#key-input").focus();
  };
  const closeKey = () => {
    $("#scrim").hidden = true;
    $("#key-modal").hidden = true;
  };
  $("#key-btn").addEventListener("click", openKey);
  $("#scrim").addEventListener("click", closeKey);
  $("#key-save").addEventListener("click", async () => {
    const value = $("#key-input").value.trim();
    if (value && !value.startsWith("gsk_")) {
      $("#key-hint").textContent = "That does not look like a Groq key — expected gsk_...";
      return;
    }
    state.key = value;
    if (value) sessionStorage.setItem("bookwright.key", value);
    else sessionStorage.removeItem("bookwright.key");
    closeKey();
    state.mode = value ? "live" : "demo";
    setMode(state.mode);
    if (state.presets) updateEstimate(state.presets);
    toast(value ? "Key set for this tab. Next book will use Groq." : "Key cleared — back to demo mode.");
  });
  $("#key-clear").addEventListener("click", () => {
    $("#key-input").value = "";
    state.key = "";
    sessionStorage.removeItem("bookwright.key");
    state.mode = "demo";
    setMode("demo");
    if (state.presets) updateEstimate(state.presets);
    closeKey();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#key-modal").hidden) closeKey();
  });

  boot();
})();
