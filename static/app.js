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
    banned: [],
    profile: null,
    outline: null,
    preset: null,
    job: null,
    plan: null,
    status: null,
    chapters: new Map(), // index -> { text, status, title, node, bodyNode, words }
    rendering: new Set(),
    stream: null,
    follow: true,
    startedAt: 0,
    modelDefaults: {},
    modelCatalog: [],
    unitLabel: "chapter",
    partLabel: "part",
  };

  const L = {
    md: ["Markdown", ".md"],
    html: ["HTML", ".html"],
    epub: ["EPUB", ".epub"],
    pdf: ["PDF", ".pdf"],
  };

  function pluralLabel(word) {
    if (word.endsWith("y") && !/[aeiou]y$/.test(word)) return `${word.slice(0, -1)}ies`;
    if (/(s|x|ch|sh)$/.test(word)) return `${word}es`;
    return `${word}s`;
  }
  function quantity(count, word) { return `${count} ${count === 1 ? word : pluralLabel(word)}`; }
  function unitLabel() { return $("#unit-label")?.value || state.unitLabel || "chapter"; }
  function partLabel() { return $("#part-label")?.value || state.partLabel || "part"; }
  function article(word) { return "aeiou".includes(word[0]?.toLowerCase()) ? "an" : "a"; }
  function unitTitle() { const word = unitLabel(); return word[0].toUpperCase() + word.slice(1); }
  function partTitle() { const word = partLabel(); return word[0].toUpperCase() + word.slice(1); }

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
      setupModelSelectors(health.models);
      if (state.mode === "live") loadGroqModels({ silent: true });
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
    updateModelAvailability();
  }

  const MODEL_ROLES = ["architect", "writer", "editor"];

  function setupModelSelectors(defaults) {
    state.modelDefaults = defaults || {};
    MODEL_ROLES.forEach((role) => {
      const select = $(`#${role}-model`);
      const defaultModel = state.modelDefaults[role] || "";
      select.replaceChildren(new Option(defaultModel || "Load Groq models", defaultModel));
      select.addEventListener("change", () => paintModelDetails(role));
      paintModelDetails(role);
    });
    $("#model-load").addEventListener("click", () => {
      if (state.mode !== "live") {
        openKey();
        return;
      }
      loadGroqModels();
    });
    updateModelAvailability();
  }

  function updateModelAvailability() {
    const live = state.mode === "live";
    const loaded = state.modelCatalog.length > 0;
    MODEL_ROLES.forEach((role) => {
      const select = $(`#${role}-model`);
      if (select) select.disabled = !live || !loaded;
    });
    const loadButton = $("#model-load");
    if (loadButton) loadButton.textContent = loaded ? "Refresh Groq model list" : "Load models from Groq";
    const count = $("#model-count");
    if (count) count.textContent = loaded ? `${state.modelCatalog.length} available` : live ? "not loaded" : "Groq only";
    const status = $("#model-list-status");
    if (status) {
      status.textContent = loaded
        ? "Catalog loaded from Groq for this key. Select a role to see its limits and owner."
        : live
          ? "Load the models available to your Groq account."
          : "Add a Groq API key to browse models. Demo mode uses fixed sample prose.";
    }
  }

  function paintModelDetails(role) {
    const select = $(`#${role}-model`);
    const info = $(`#${role}-model-info`);
    if (!select || !info) return;
    const id = select.value;
    const model = state.modelCatalog.find((item) => item.id === id);
    if (!model) {
      info.textContent = id
        ? `${id} · configured default${state.modelCatalog.length ? "; not in the current account catalog" : ""}.`
        : "Add a key and load the model catalog.";
      return;
    }
    const details = [];
    if (model.owned_by) details.push(`Provider: ${model.owned_by}`);
    if (Number.isFinite(model.context_window)) details.push(`Context: ${nf.format(model.context_window)} tokens`);
    if (Number.isFinite(model.max_completion_tokens)) details.push(`Max output: ${nf.format(model.max_completion_tokens)} tokens`);
    if (model.active === false) details.push("Inactive in Groq");
    info.textContent = details.join(" · ") || "Listed by Groq; model limits are not exposed in the catalog.";
  }

  async function loadGroqModels({ silent = false } = {}) {
    const button = $("#model-load");
    if (button) button.disabled = true;
    try {
      const response = await api("/api/models");
      state.modelCatalog = (response.models || []).filter((item) => item.id);
      MODEL_ROLES.forEach((role) => {
        const select = $(`#${role}-model`);
        const current = select.value || state.modelDefaults[role] || "";
        const options = state.modelCatalog.map((model) => {
          const label = model.active === false ? `${model.id} · inactive` : model.id;
          const option = new Option(label, model.id);
          option.disabled = model.active === false;
          return option;
        });
        if (current && !state.modelCatalog.some((model) => model.id === current)) {
          options.unshift(new Option(`${current} · configured default`, current));
        }
        select.replaceChildren(...options);
        select.value = current;
        paintModelDetails(role);
      });
      updateModelAvailability();
      if (!silent) toast(`${state.modelCatalog.length} models loaded from Groq.`);
    } catch (err) {
      updateModelAvailability();
      if (silent) {
        $("#model-list-status").textContent = `Could not load Groq models: ${err.message}. You can retry below.`;
      } else {
        toast(err.message, 9000);
      }
    } finally {
      if (button) button.disabled = false;
    }
  }

  function resetModelSelectors() {
    state.modelCatalog = [];
    MODEL_ROLES.forEach((role) => {
      const select = $(`#${role}-model`);
      const defaultModel = state.modelDefaults[role] || "";
      select.replaceChildren(new Option(defaultModel || "Groq model", defaultModel));
      paintModelDetails(role);
    });
    updateModelAvailability();
  }

  function fillForm(presets) {
    const audience = $("#audience");
    presets.audiences.forEach((name) => audience.add(new Option(name, name)));

    const tone = $("#tone");
    presets.tones.forEach((t) => tone.add(new Option(t.id[0].toUpperCase() + t.id.slice(1), t.id)));
    tone.value = presets.defaults.tone;
    const toneHint = $("#tone-hint");
    const updateToneHint = () => {
      toneHint.textContent = presets.tones.find((item) => item.id === tone.value)?.description || "";
    };
    tone.addEventListener("change", updateToneHint);
    updateToneHint();

    $("#unit-label").value = state.unitLabel;
    $("#part-label").value = state.partLabel;
    const syncStructure = () => {
      state.unitLabel = unitLabel();
      state.partLabel = partLabel();
      document.querySelectorAll("#lengths .chip span").forEach((node, index) => {
        const preset = presets.lengths[index];
        node.textContent = preset.blurb.replace(/chapters?/gi, (match) =>
          match.toLowerCase().endsWith("s") ? pluralLabel(unitLabel()) : unitLabel());
      });
      const lengthInfo = currentLength(presets);
      const chapterCount = lengthInfo.chapters || 10;
      const words = Number($("#c-words").value) || 1000;
      const automaticGroups = state.length === "custom"
        ? Math.max(1, Math.min(Math.round(chapterCount / 4), 6)) : (lengthInfo.parts || 1);
      if (Number($("#group-count").value) > chapterCount) $("#group-count").value = "";
      const groups = Number($("#group-count").value) || automaticGroups;
      [...$("#group-count").options].forEach((option) => {
        if (!option.value) return;
        const count = Number(option.value);
        option.disabled = count > chapterCount;
        option.textContent = quantity(count, partLabel());
      });
      $("#custom-units-label").textContent = pluralLabel(unitLabel());
      $("#custom-words-label").textContent = `Words / ${unitLabel()}`;
      $("#s-units-label").textContent = pluralLabel(unitLabel())[0].toUpperCase() + pluralLabel(unitLabel()).slice(1);
      $("#o-add").textContent = `Add ${article(unitLabel())} ${unitLabel()}`;
      $("#custom-summary").textContent = `${quantity(chapterCount, unitLabel())} · ${quantity(groups, partLabel())} · ~${compact(Number(chapterCount) * words)} words`;
      const option = $("#exp-chapter-heading")?.querySelector('option[value="chapter"]');
      if (option) option.textContent = `${unitTitle()} 1`;
      updateEstimate(presets);
    };
    $("#unit-label").addEventListener("change", syncStructure);
    $("#part-label").addEventListener("change", syncStructure);
    $("#group-count").addEventListener("change", syncStructure);
    $("#exp-unit-label").addEventListener("change", () => {
      const option = $("#exp-chapter-heading").querySelector('option[value="chapter"]');
      option.textContent = `${$("#exp-unit-label").value[0].toUpperCase()}${$("#exp-unit-label").value.slice(1)} 1`;
    });
    $("#c-chapters").addEventListener("input", syncStructure);
    $("#c-words").addEventListener("input", syncStructure);

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
        syncStructure();
      });
      chip.setAttribute("aria-checked", String(preset.id === presets.defaults.length));
      chips.append(chip);
    });
    state.length = presets.defaults.length;
    state.presets = presets;
    syncStructure();

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
      const automaticGroups = Math.max(1, Math.min(Math.round(Number(chap.value) / 4), 6));
      const groups = Math.min(Number($("#group-count").value) || automaticGroups, Number(chap.value));
      $("#custom-summary").textContent =
        `${quantity(Number(chap.value), unitLabel())} · ${quantity(groups, partLabel())} · ~${nf.format(total)} words`;
      $("#custom-units-label").textContent = pluralLabel(unitLabel());
      $("#custom-words-label").textContent = `Words / ${unitLabel()}`;
      updateEstimate(presets);
    };
    chap.addEventListener("input", syncCustom);
    words.addEventListener("input", syncCustom);

    // Editorial profiles
    const profiles = $("#profiles");
    const setProfile = (id) => {
      state.profile = id;
      [...profiles.children].forEach((c) =>
        c.setAttribute("aria-checked", String(c.dataset.id === id)));
    };
    presets.profiles.forEach((profile) => {
      const chip = el("button", "profile-chip", profile.label);
      chip.type = "button";
      chip.dataset.id = profile.id;
      chip.title = profile.description;
      chip.setAttribute("role", "radio");
      chip.setAttribute("aria-checked", "false");
      chip.addEventListener("click", () => {
        setProfile(profile.id);
        applyProfile(profile, presets);
      });
      profiles.append(chip);
      const guideLine = el("p", "guide-profile");
      guideLine.append(el("strong", "", profile.label), document.createTextNode(` — ${profile.description}`));
      $("#guide-profiles").append(guideLine);
    });
    setProfile(presets.default_profile || "custom");
    // Any hand-edit of the switches/strength means the profile no longer applies.
    const markCustom = () => {
      if (state.profile !== "custom") setProfile("custom");
    };

    // Banned phrases
    const banned = $("#banned-phrases");
    const parseBanned = () => banned.value
      .split(/[\n,]/).map((v) => v.trim()).filter(Boolean);
    banned.addEventListener("input", () => {
      state.banned = parseBanned().slice(0, presets.banned_phrases.max);
      $("#banned-count").textContent = state.banned.length;
      updateEstimate(presets);
    });

    // Point of view
    $("#review-outline").checked = Boolean(
      presets.review_outline.browser_default ?? presets.review_outline.default
    );

    const pov = $("#pov");
    presets.pov.options.forEach((option) => {
      const opt = new Option(option.label, option.id);
      opt.title = option.description || "";
      pov.add(opt);
    });
    pov.value = presets.pov.default || "auto";
    pov.addEventListener("change", () => {
      applyPovConflicts();
      updateEstimate(presets);
    });

    // Notes and voice sample
    const notes = $("#notes");
    notes.addEventListener("input", () => {
      $("#notes-count").textContent = notes.value.length;
      $("#grounding-count").textContent =
        notes.value.trim() || $("#voice-sample").value.trim() ? "in use" : "optional";
      updateEstimate(presets);
    });
    const voice = $("#voice-sample");
    voice.addEventListener("input", () => {
      $("#voice-count").textContent = voice.value.length;
      updateEstimate(presets);
    });

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
        markCustom();
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
      markCustom();
      updateEstimate(presets);
    });

    const stylistOn = $("#stylist-on");
    const stylistStrength = $("#stylist-strength");
    if (presets.stylist) {
      stylistOn.checked = Boolean(presets.stylist.default);
      stylistStrength.value = presets.stylist.default_strength || "standard";
    }
    stylistOn.addEventListener("change", () => {
      stylistStrength.disabled = !stylistOn.checked;
      updateEstimate(presets);
    });
    stylistStrength.addEventListener("change", () => {
      markCustom();
      updateEstimate(presets);
    });

    applyPovConflicts();
    updateEstimate(presets);
  }

  // A rule that forbids the person the author just chose would fight the writer
  // for the whole book, so the switch is turned off instead.
  const POV_BLOCKS = { first: "no_first_person", we: "no_first_person", you: "no_second_person" };
  function applyPovConflicts() {
    const blocked = POV_BLOCKS[$("#pov").value];
    const inputs = [...window.document.querySelectorAll("#anti-patterns input")];
    let dropped = false;
    inputs.forEach((input) => {
      const conflicts = input.value === blocked;
      if (conflicts && input.checked) {
        input.checked = false;
        dropped = true;
      }
      input.disabled = conflicts;
      input.closest(".toggle")?.classList.toggle("muted", conflicts);
    });
    if (dropped || blocked) {
      state.directives = inputs.filter((i) => i.checked).map((i) => i.value);
      paintStyleCount();
    }
    const povDescription = state.presets?.pov.options.find((option) => option.id === $("#pov").value)?.description;
    $("#pov-hint").textContent = blocked
      ? `“${blocked === "no_first_person" ? "No I" : "No you"}” is switched off — it contradicts this point of view.`
      : povDescription || "A rule that contradicts this is switched off rather than half-applied.";
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
    if ($("#stylist-on").checked) styleBits.push(`de-AI pass (${$("#stylist-strength").value})`);
    if (state.banned.length) styleBits.push(`${state.banned.length} banned phrases`);
    const notesChars = $("#notes") ? $("#notes").value.trim().length : 0;
    if (notesChars) styleBits.push(`${notesChars} chars of notes`);
    const suffix = styleBits.length ? ` · ${styleBits.join(" + ")}` : "";
    const words = compact(p.total_words);
    const automaticGroups = state.length === "custom"
      ? Math.max(1, Math.min(Math.round(p.chapters / 4), 6)) : (p.parts || 1);
    const groups = Math.min(Number($("#group-count")?.value) || automaticGroups, p.chapters);
    const groupName = $("#part-label")?.value || "part";
    const groupPhrase = quantity(groups, groupName);
    $("#estimate").textContent = state.key
      ? `Live mode · ${p.chapters} ${pluralLabel(unitLabel())} in ${groupPhrase}, about ${words} words of output${suffix}.`
      : `Demo mode · ${p.chapters} ${pluralLabel(unitLabel())} in ${groupPhrase}, fixture prose${suffix}. Add a Groq key for real writing.`;
  }

  function applyProfile(profile, presets) {
    const wanted = new Set(profile.directives);
    window.document.querySelectorAll("#anti-patterns input").forEach((input) => {
      input.checked = wanted.has(input.value);
    });
    state.directives = [...wanted].filter((id) =>
      window.document.querySelector(`#anti-patterns input[value="${id}"]`));
    $("#stylist-strength").value = profile.strength || "standard";
    if (profile.extra && !$("#extra-instructions").value.trim()) {
      $("#extra-instructions").value = profile.extra;
      $("#extra-count").textContent = profile.extra.length;
    }
    applyPovConflicts();
    paintStyleCount();
    updateEstimate(presets);
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
    payload.stylist = $("#stylist-on").checked;
    payload.stylist_strength = $("#stylist-strength").value;
    payload.banned_phrases = state.banned;
    payload.profile = state.profile || null;
    payload.notes = $("#notes").value.trim();
    payload.voice_sample = $("#voice-sample").value.trim();
    payload.pov = $("#pov").value || "auto";
    payload.review_outline = $("#review-outline").checked;
    payload.unit_label = unitLabel();
    payload.part_label = partLabel();
    payload.subheading_style = $("#subheading-style").value;
    payload.group_count = $("#group-count").value ? Number($("#group-count").value) : null;
    if (state.mode === "live") {
      payload.architect_model = $("#architect-model").value || null;
      payload.writer_model = $("#writer-model").value || null;
      payload.editor_model = $("#editor-model").value || null;
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

    stream.addEventListener("outline_review", (e) => {
      const data = JSON.parse(e.data);
      if (data.plan) openOutline(data.plan);
      setStatus(data.message || "Outline ready for review");
      $("#run").hidden = false;
      $("#cancel").hidden = false;
      $("#again").hidden = true;
    });

    stream.addEventListener("status", (e) => {
      const data = JSON.parse(e.data);
      state.status = data.stage || state.status;
      if (data.message) setStatus(data.message);
    });

    stream.addEventListener("plan", (e) => {
      const data = JSON.parse(e.data);
      renderPlan(data.plan);
      if (state.status !== "awaiting_outline") {
        setStatus(`Plan accepted — drafting ${pluralLabel(unitLabel())}`);
      }
    });

    stream.addEventListener("chapter_start", (e) => {
      const data = JSON.parse(e.data);
      ensureChapter(data.index, data.title);
      setChapterStatus(data.index, "writing");
      setStatus(`Writing ${unitLabel()} ${data.index}: ${data.title}`);
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

    stream.addEventListener("stylist_start", (e) => {
      const data = JSON.parse(e.data);
      setStatus(`Editing ${unitLabel()} ${data.index} — ${data.flags} pattern${data.flags === 1 ? "" : "s"} flagged`);
      const chapter = state.chapters.get(data.index);
      if (chapter && chapter.footNode) {
        const fixes = el("span", "fixes", "editing…");
        chapter.footNode.querySelector(".fixes")?.remove();
        chapter.footNode.prepend(fixes);
      }
    });

    stream.addEventListener("stylist_done", (e) => {
      const data = JSON.parse(e.data);
      const chapter = state.chapters.get(data.index);
      if (!chapter) return;
      chapter.original = chapter.original || chapter.text;
      if (data.text) chapter.text = data.text;
      chapter.diff = data.diff || null;
      chapter.fixes = Math.max(0, (data.before || 0) - (data.after || 0));
      renderChapter(data.index, true);
      applyStats(data.stats);
    });

    stream.addEventListener("chapter_reverted", (e) => {
      const data = JSON.parse(e.data);
      const chapter = state.chapters.get(data.index);
      if (!chapter) return;
      chapter.diff = null;
      chapter.fixes = 0;
      chapter.reverted = true;
      chapter.original = null;
      if (data.text) chapter.text = data.text;
      renderChapter(data.index, true);
    });

    stream.addEventListener("stylist_rejected", (e) => {
      const data = JSON.parse(e.data);
      toast(`Editorial pass skipped ${unitLabel()} ${data.index}: ${data.reason}`, 7000);
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
      toast(`${unitTitle()} ${data.index} failed: ${data.message}`, 9000);
      paintToc();
    });

    stream.addEventListener("stats", (e) => {
      applyStats(JSON.parse(e.data).stats);
    });

    stream.addEventListener("done", (e) => {
      const data = JSON.parse(e.data);
      state.status = data.status;
      state.chapters.forEach(paintChapterTools);
      const label =
        data.status === "partial"
          ? `Finished with ${data.failed.length} failed ${data.failed.length === 1 ? unitLabel() : pluralLabel(unitLabel())}`
          : `Book written in ${duration(data.elapsed)}`;
      setStatus(label);
      markRunDone(data.status === "partial" ? "error" : "done");
      closeOutline();
      applyStats(data.stats);
      if (data.style_report) renderStyleReport(data.style_report);
      closeStream();
      paintToc();
    });

    stream.addEventListener("error", (e) => {
      state.status = "error";
      state.chapters.forEach(paintChapterTools);
      if (e.data) {
        const data = JSON.parse(e.data);
        setStatus("Generation failed");
        toast(data.message || "Generation failed", 12000);
        markRunDone("error");
      }
      closeStream();
    });

    stream.addEventListener("cancelled", () => {
      state.status = "cancelled";
      state.chapters.forEach(paintChapterTools);
      setStatus("Stopped — what was written is kept");
      markRunDone("error");
      closeOutline();
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
    $("#report").hidden = true;
    $("#r-residue").replaceChildren();
    $("#outline").hidden = true;
    state.outline = null;
    state.status = null;
    $("#rail span").style.width = "0%";
    $("#meter-fill").style.width = "0%";
    markRunDone(null);
  }

  function applySnapshot(snap) {
    state.status = snap.status;
    if (snap.spec) {
      state.unitLabel = snap.spec.unit_label || state.unitLabel;
      state.partLabel = snap.spec.part_label || state.partLabel;
      $("#unit-label").value = state.unitLabel;
      $("#part-label").value = state.partLabel;
      $("#subheading-style").value = snap.spec.subheading_style || "standard";
      $("#group-count").value = snap.spec.group_count ? String(snap.spec.group_count) : "";
    }
    if (snap.plan) renderPlan(snap.plan, snap.chapters);
    if (snap.status === "awaiting_outline" && snap.plan) {
      openOutline(snap.plan);
      setStatus("Outline ready — edit it, then approve to start writing");
      $("#run").hidden = false;
      $("#cancel").hidden = false;
      $("#again").hidden = true;
    } else if (snap.status !== "awaiting_outline") {
      closeOutline();
    }
    for (const chapter of snap.chapters || []) {
      ensureChapter(chapter.index, chapter.title);
      const local = state.chapters.get(chapter.index);
      local.text = chapter.text || "";
      local.status = chapter.status;
      local.summary = chapter.summary || "";
      local.original = chapter.original || null;
      local.diff = chapter.diff || null;
      local.fixes = Math.max(0, (chapter.style_before || 0) - (chapter.style_after || 0));
      local.reverted = Boolean(chapter.reverted);
      local.regenerationCount = chapter.regeneration_count || 0;
      local.regenerating = Boolean(chapter.regenerating);
      renderChapter(chapter.index, true);
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
    const chapter = {
      index, title: title || `Chapter ${index}`, text: "", status: "pending", summary: "",
      words: 0, node: null, bodyNode: null, toolsNode: null,
      original: null, diff: null, fixes: 0, diffOpen: false, reverted: false,
      regenCommand: "", regenerationCount: 0, regenerating: false,
    };
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
    $("#exp-unit-label").value = state.unitLabel;
    $("#exp-part-label").value = state.partLabel;
    $("#exp-title").value = plan.title || "";
    $("#exp-subtitle").value = plan.subtitle || "";
    $("#exp-title").placeholder = plan.title || "Book title";
    $("#exp-subtitle").placeholder = plan.subtitle || "Subtitle (optional)";
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
      el("p", "meta", `${plan.audience} · ${plan.chapter_count} ${pluralLabel(state.unitLabel)} · ${compact(plan.total_words)} words`)
    );
    const styleSummary = $("#style-panel") ? state.directives.length : 0;
    if (styleSummary) {
      cover.append(el("p", "cover-style", `${styleSummary} style rules applied`));
    }
    if (plan.thesis) cover.append(el("p", "thesis", plan.thesis));
    book.append(cover);

    for (const part of plan.parts) {
      const divider = el("section", "part-divider");
      divider.append(el("div", "num", `${partTitle()} ${part.index}`), el("h3", "", part.title));
      if (part.summary) divider.append(el("p", "", part.summary));
      book.append(divider);

      for (const spec of part.chapters) {
        const chapter = ensureChapter(spec.index, spec.title);
        const article = el("article", "chapter pending");
        article.id = `chapter-${spec.index}`;
        const head = el("header");
        head.append(el("div", "ch-num", `${unitTitle()} ${spec.index}`), el("h3", "", spec.title));
        const body = el("div", "body");
        body.innerHTML = '<div class="skeleton w80"></div><div class="skeleton"></div><div class="skeleton w60"></div>';
        const foot = el("div", "ch-foot");
        foot.append(el("span", "flag", ""), el("span", "sep"));
        const tools = el("details", "chapter-tools");
        const toolsSummary = el("summary", "", `Regenerate this ${unitLabel()} with a command`);
        const command = document.createElement("textarea");
        command.className = "regen-command";
        command.rows = 2;
        command.maxLength = 1200;
        command.placeholder = "e.g. Tighten the opening, add a concrete example from the supplied notes, and make the ending less conclusive.";
        command.value = chapter.regenCommand || "";
        command.addEventListener("input", () => { chapter.regenCommand = command.value; });
        const toolActions = el("div", "chapter-tool-actions");
        const regenerate = el("button", "regen-btn", `Regenerate ${unitLabel()}`);
        regenerate.type = "button";
        regenerate.addEventListener("click", () => regenerateChapter(chapter.index));
        const undoRegeneration = el("button", "undo-regen-btn", "Undo regeneration");
        undoRegeneration.type = "button";
        undoRegeneration.addEventListener("click", () => undoChapterRegeneration(chapter.index));
        const note = el("p", "regen-note", `Only this ${unitLabel()} changes; later ${pluralLabel(unitLabel())} are not automatically rewritten. A Groq key is required for custom commands.`);
        toolActions.append(regenerate, undoRegeneration);
        tools.append(toolsSummary, command, toolActions, note);
        article.append(head, body, foot, tools);
        book.append(article);

        chapter.node = article;
        chapter.bodyNode = body;
        chapter.footNode = foot;
        chapter.toolsNode = tools;
        chapter.regenCommandNode = command;
        chapter.regenerateButton = regenerate;
        chapter.undoRegenerationButton = undoRegeneration;
        chapter.status = chapter.status || "pending";
        if (chapter.text) renderChapter(spec.index, true);
      }
    }

    $("#toc").hidden = false;
    paintToc();

    if (chapters && chapters.length) {
      for (const item of chapters) {
        const chapter = ensureChapter(item.index, item.title);
        chapter.text = item.text || "";
        chapter.status = item.status || chapter.status;
        chapter.summary = item.summary || "";
        chapter.regenerationCount = item.regeneration_count || 0;
        chapter.regenerating = Boolean(item.regenerating);
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
    chapter.bodyNode.innerHTML = html + caret + (chapter.diffOpen ? diffHtml(chapter) : "");
    chapter.node?.classList.toggle("pending", false);
    paintChapterFoot(chapter);
    paintChapterTools(chapter);
  }

  function paintChapterTools(chapter) {
    const tools = chapter.toolsNode;
    if (!tools) return;
    const finished = ["done", "partial"].includes(state.status);
    tools.hidden = chapter.status !== "done" || !finished;
    if (chapter.regenerateButton) {
      chapter.regenerateButton.disabled = Boolean(chapter.regenerating);
      chapter.regenerateButton.textContent = chapter.regenerating ? "Regenerating…" : "Regenerate chapter";
    }
    if (chapter.undoRegenerationButton) {
      chapter.undoRegenerationButton.hidden = !chapter.regenerationCount || Boolean(chapter.regenerating);
    }
  }

  function paintChapterFoot(chapter) {
    const foot = chapter.footNode;
    if (!foot) return;
    foot.querySelector(".fixes")?.remove();
    foot.querySelector(".diff-btn")?.remove();
    foot.querySelector(".revert-btn")?.remove();
    if (chapter.fixes > 0) {
      foot.prepend(el("span", "fixes", `${chapter.fixes} de-AI fix${chapter.fixes === 1 ? "" : "es"}`));
    }
    if (chapter.diff) {
      const btn = el("button", "diff-btn", chapter.diffOpen ? "Hide changes" : "See changes");
      btn.type = "button";
      btn.addEventListener("click", () => {
        chapter.diffOpen = !chapter.diffOpen;
        renderChapter(chapter.index, true);
      });
      const revert = el("button", "revert-btn", "Undo edits");
      revert.type = "button";
      revert.title = "Put the pre-editorial draft back for this chapter";
      revert.addEventListener("click", () => revertChapter(chapter.index));
      foot.prepend(btn, revert);
    }
    if (chapter.reverted) {
      foot.prepend(el("span", "fixes", "editorial edits undone"));
    }
  }

  function diffHtml(chapter) {
    const parts = ['<div class="diff"><p class="diff-note">What the editorial pass changed. Unchanged paragraphs are hidden.</p>'];
    let shown = 0;
    for (const op of chapter.diff) {
      if (op.op === "change") {
        shown++;
        parts.push(
          `<div class="pair"><p class="del">${escapeText(op.before)}</p>` +
          `<p class="ins">${escapeText(op.after)}</p></div>`
        );
      } else if (op.op === "del") {
        for (const p of op.before) { shown++; parts.push(`<div class="pair"><p class="del">${escapeText(p)}</p></div>`); }
      } else if (op.op === "ins") {
        for (const p of op.after) { shown++; parts.push(`<div class="pair"><p class="ins">${escapeText(p)}</p></div>`); }
      }
    }
    if (!shown) parts.push('<p class="diff-note">No paragraph-level changes.</p>');
    parts.push("</div>");
    return parts.join("");
  }

  async function revertChapter(index) {
    if (!state.job) return;
    try {
      const res = await fetch(`/api/books/${state.job}/chapters/${index}/revert`, { method: "POST" });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || `revert failed (${res.status})`);
      const chapter = state.chapters.get(index);
      if (chapter && body.text) chapter.text = body.text;
      if (chapter) {
        chapter.diff = null;
        chapter.fixes = 0;
        chapter.reverted = true;
        chapter.original = null;
        renderChapter(index, true);
      }
      if (body.report) renderStyleReport(body.report);
    } catch (err) {
      toast(err.message);
    }
  }

  function applyRegenerationResult(chapter, result) {
    const update = result.chapter || result;
    chapter.text = update.text || "";
    chapter.summary = update.summary || "";
    chapter.words = update.words || chapter.text.split(/\s+/).filter(Boolean).length;
    chapter.original = update.original || null;
    chapter.diff = update.diff || null;
    chapter.fixes = Math.max(0, (update.style_before || 0) - (update.style_after || 0));
    chapter.reverted = Boolean(update.reverted);
    chapter.regenerationCount = update.regeneration_count || 0;
    chapter.regenerating = false;
    chapter.status = "done";
    renderChapter(chapter.index, true);
    if (result.stats) applyStats(result.stats);
    if (result.style_report) renderStyleReport(result.style_report);
  }

  async function regenerateChapter(index) {
    const chapter = state.chapters.get(index);
    const instruction = (chapter?.regenCommand || "").trim();
    if (!chapter || instruction.length < 5) {
      toast("Enter a specific revision command (at least five characters).");
      return;
    }
    chapter.regenerating = true;
    paintChapterTools(chapter);
    setStatus(`Regenerating chapter ${index}…`);
    try {
      const result = await api(`/api/books/${state.job}/chapters/${index}/regenerate`, {
        method: "POST",
        body: JSON.stringify({ instruction }),
      });
      applyRegenerationResult(chapter, result);
      setStatus(`${unitTitle()} ${index} regenerated`);
    } catch (err) {
      chapter.regenerating = false;
      paintChapterTools(chapter);
      toast(err.message, 9000);
      setStatus(`${unitTitle()} regeneration failed; the existing text was kept`);
    }
  }

  async function undoChapterRegeneration(index) {
    const chapter = state.chapters.get(index);
    if (!chapter || !chapter.regenerationCount) return;
    chapter.regenerating = true;
    paintChapterTools(chapter);
    try {
      const result = await api(`/api/books/${state.job}/chapters/${index}/undo-regeneration`, {
        method: "POST",
      });
      applyRegenerationResult(chapter, result);
      setStatus(`Restored the previous version of chapter ${index}`);
    } catch (err) {
      chapter.regenerating = false;
      paintChapterTools(chapter);
      toast(err.message, 9000);
    }
  }

  const escapeText = (s) => String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

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

  function renderStyleReport(report) {
    if (!report || !report.available) return;
    $("#report").hidden = false;
    $("#r-flags").textContent = report.total ?? 0;
    const fixed = Math.max(0, (report.flags_before || 0) - (report.flags_after || 0));
    $("#r-fixed").textContent = fixed;
    const read = report.readability || {};
    $("#r-ease").textContent = read.flesch_ease !== undefined ? `${read.flesch_ease}` : "—";
    $("#r-grade").textContent = read.grade_level !== undefined ? `${read.grade_level}` : "—";
    $("#r-sentence").textContent = read.avg_sentence_words ? `${read.avg_sentence_words}w` : "—";
    $("#r-adverbs").textContent = read.adverbs_per_100_words !== undefined ? `${read.adverbs_per_100_words}` : "—";

    const list = $("#r-residue");
    list.replaceChildren();
    const byRule = report.by_rule || {};
    const entries = Object.entries(byRule).sort((a, b) => b[1] - a[1]);
    const max = entries.length ? entries[0][1] : 1;
    for (const [rule, count] of entries) {
      const li = el("li");
      li.append(
        el("span", "name", rule.replace(/_/g, " ")),
        (() => { const bar = el("span", "bar"); const fill = el("span"); fill.style.width = `${Math.round((count / max) * 100)}%`; bar.append(fill); return bar; })(),
        el("span", "n", String(count))
      );
      list.append(li);
    }
    $("#r-empty").hidden = entries.length > 0;
  }

  // ------------------------------------------------------------- outline
  // The plan arrives as data, is edited as data, and goes back as data. The
  // server re-validates it, so a bad edit is rejected, not written.
  function openOutline(plan) {
    if (!plan || !plan.parts) return;
    state.outline = JSON.parse(JSON.stringify(plan));
    $("#outline").hidden = false;
    $("#o-title").value = plan.title || "";
    $("#o-subtitle").value = plan.subtitle || "";
    paintOutline();
    if (state.chapters.size === 0) {
      // Preview rows so the reader pane shows the shape before any prose exists.
      renderPlan(plan, []);
    }
    if (typeof $("#outline").scrollIntoView === "function") {
      $("#outline").scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  function closeOutline() {
    $("#outline").hidden = true;
    $("#o-chapters").replaceChildren();
  }

  function outlineChapters() {
    return state.outline ? state.outline.parts.flatMap((part) => part.chapters) : [];
  }

  function paintOutline() {
    const host = $("#o-chapters");
    host.replaceChildren();
    let n = 0;
    for (const part of state.outline.parts) {
      const heading = el("p", "o-part", `${partTitle()} ${part.index} · ${part.title || "untitled"}`);
      host.append(heading);
      for (const chapter of part.chapters) {
        n += 1;
        const row = el("div", "o-chapter");
        row.dataset.index = String(chapter.index);

        const head = el("div", "o-head");
        head.append(el("span", "o-num", String(n)));
        const title = document.createElement("input");
        title.className = "o-title";
        title.maxLength = 120;
        title.value = chapter.title || "";
        title.setAttribute("aria-label", `${unitTitle()} ${n} title`);
        title.addEventListener("input", () => { chapter.title = title.value; });
        const drop = el("button", "o-drop", "×");
        drop.type = "button";
        drop.title = "Remove this chapter";
        drop.addEventListener("click", () => {
          if (outlineChapters().length <= 2) {
            toast(`A book needs at least two ${pluralLabel(unitLabel())}.`);
            return;
          }
          part.chapters = part.chapters.filter((c) => c !== chapter);
          renumberOutline();
          paintOutline();
        });
        head.append(title, drop);
        row.append(head);

        const purpose = document.createElement("textarea");
        purpose.className = "o-purpose";
        purpose.rows = 2;
        purpose.maxLength = 320;
        purpose.value = chapter.purpose || "";
        purpose.placeholder = "What this chapter must establish";
        purpose.addEventListener("input", () => { chapter.purpose = purpose.value; });

        const beats = document.createElement("textarea");
        beats.className = "o-beats";
        beats.rows = 3;
        beats.value = (chapter.beats || []).join("\n");
        beats.placeholder = "One concrete beat per line";
        beats.addEventListener("input", () => {
          chapter.beats = beats.value.split("\n").map((b) => b.trim()).filter(Boolean).slice(0, 6);
        });

        row.append(purpose, beats);
        host.append(row);
      }
    }
    $("#o-count").textContent = `${n} ${n === 1 ? unitLabel() : pluralLabel(unitLabel())} · edits apply to what gets written`;
  }

  function renumberOutline() {
    let index = 0;
    state.outline.parts.forEach((part, p) => {
      part.index = p + 1;
      part.chapters.forEach((chapter) => { index += 1; chapter.index = index; });
    });
    state.outline.parts = state.outline.parts.filter((p) => p.chapters.length);
  }

  $("#o-add").addEventListener("click", () => {
    if (!state.outline) return;
    if (outlineChapters().length >= 20) {
      toast("The 20-division limit has been reached.");
      return;
    }
    const last = state.outline.parts[state.outline.parts.length - 1];
    last.chapters.push({
      index: 0, title: `Untitled ${unitLabel()}`, purpose: "", beats: [], terms: [], words: 0,
    });
    renumberOutline();
    paintOutline();
  });

  $("#o-approve").addEventListener("click", async () => {
    if (!state.outline || !state.job) return;
    const empty = outlineChapters().some((c) => !String(c.title || "").trim());
    if (empty) {
      toast(`Every ${unitLabel()} needs a title before you approve the outline.`);
      return;
    }
    renumberOutline();
    const body = {
      title: $("#o-title").value.trim() || state.outline.title,
      subtitle: $("#o-subtitle").value.trim(),
      thesis: state.outline.thesis || "",
      voice: state.outline.voice || "",
      parts: state.outline.parts.map((part) => ({
        title: part.title,
        summary: part.summary || "",
        chapters: part.chapters.map((c) => ({
          title: String(c.title).trim(),
          purpose: String(c.purpose || "").trim() || "The chapter as titled.",
          beats: c.beats || [],
        })),
      })),
    };
    const button = $("#o-approve");
    button.disabled = true;
    try {
      const res = await api(`/api/books/${state.job}/outline`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      closeOutline();
      setStatus(`Outline approved — drafting ${pluralLabel(unitLabel())}`);
      if (res.plan) {
        state.chapters.clear();
        renderPlan(res.plan, []);
      }
    } catch (err) {
      toast(err.message);
    } finally {
      button.disabled = false;
    }
  });

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
          const options = new URLSearchParams({
            page_size: $("#exp-page-size").value,
            margins: $("#exp-margins").value,
            chapter_heading: $("#exp-chapter-heading").value,
            body_font_size: $("#exp-font-size").value,
            line_spacing: $("#exp-line-spacing").value,
            paragraph_style: $("#exp-paragraph-style").value,
            alignment: $("#exp-alignment").value,
            chapter_new_page: String($("#exp-chapter-break").checked),
            running_matter: String($("#exp-running-matter").checked),
            page_numbers: String($("#exp-page-numbers").checked),
            clean_scaffolding: String($("#exp-clean-scaffolding").checked),
            show_part_heading: String($("#exp-show-part-heading").checked),
            show_part_subtitle: String($("#exp-show-part-subtitle").checked),
            show_chapter_title: String($("#exp-show-chapter-title").checked),
            show_chapter_subtitle: String($("#exp-show-chapter-subtitle").checked),
            unit_label: $("#exp-unit-label").value,
            part_label: $("#exp-part-label").value,
            title: $("#exp-title").value.trim(),
            subtitle: $("#exp-subtitle").value.trim(),
            show_title_page: String($("#exp-title-page").checked),
            show_subtitle: String($("#exp-subtitle-visible").checked),
            cover_kicker: $("#exp-cover-kicker").value,
            cover_alignment: $("#exp-cover-align").value,
            cover_position: $("#exp-cover-position").value,
            part_alignment: $("#exp-part-align").value,
            part_position: $("#exp-part-position").value,
            chapter_alignment: $("#exp-chapter-align").value,
            chapter_position: $("#exp-chapter-position").value,
            end_imprint: $("#exp-end-imprint").value,
            header_left: $("#exp-header-left").value.trim(),
            header_right: $("#exp-header-right").value.trim(),
          });
          const res = await fetch(`/api/books/${state.job}/export/${fmt}?${options}`);
          if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.error || `export failed (${res.status})`);
          }
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = `${slugify($("#exp-title").value.trim() || state.plan?.title || "book")}.${fmt}`;
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
    if (value) loadGroqModels({ silent: true });
    else resetModelSelectors();
    toast(value ? "Key set for this tab. Next book will use Groq." : "Key cleared — back to demo mode.");
  });
  $("#key-clear").addEventListener("click", () => {
    $("#key-input").value = "";
    state.key = "";
    sessionStorage.removeItem("bookwright.key");
    state.mode = "demo";
    setMode("demo");
    if (state.presets) updateEstimate(state.presets);
    resetModelSelectors();
    closeKey();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#key-modal").hidden) closeKey();
  });

  boot();
})();
