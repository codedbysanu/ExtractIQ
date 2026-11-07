/* ExtractIQ front end — vanilla JS, no build step.
 *
 * Flow: pick PDF -> POST /api/extract -> render editable fields -> human verifies -> export JSON.
 * Status model per field:  extracted | needs_review | verified   (no confidence scores).
 * Editing a field counts as human verification; "Verify" can be toggled off again.
 * All server text is inserted with textContent/value only (never innerHTML).
 */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);

  /** Tiny DOM builder: el("div", {class: "x", onclick: fn}, "text", childNode) */
  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (k === "value") node.value = v;
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  // ---------------------------------------------------------------- config
  const MODE_LABELS = { resume: "Resume / Candidate", generic: "Generic Document" };
  const STATUS_LABELS = { extracted: "Extracted", needs_review: "Needs review", verified: "Verified" };
  const KEY_INFO_TYPES = ["Email", "Phone", "URL", "Date", "Amount", "Percentage", "Frequent term", "Other"];

  const COLUMNS = {
    education: [
      { key: "degree", label: "Degree / level" },
      { key: "institution", label: "Institution / board", wide: true },
      { key: "period", label: "Period" },
      { key: "result", label: "Result" },
    ],
    experience: [
      { key: "title_company", label: "Role / company", wide: true },
      { key: "period", label: "Period" },
      { key: "months", label: "Months", narrow: true },
      { key: "note", label: "Parser note", readonly: true },
    ],
    key_information: [
      { key: "type", label: "Type", options: KEY_INFO_TYPES },
      { key: "value", label: "Value" },
    ],
  };
  const NEW_ROW = {
    education: () => ({ degree: "", institution: "", period: "", result: "" }),
    experience: () => ({ title_company: "", period: "", months: "", note: "" }),
    key_information: () => ({ type: "Other", value: "" }),
  };
  const EXPORT_KEY = { total_experience: "total_experience_months", page_count: "pages" };

  // ----------------------------------------------------------------- state
  const state = { file: null, mode: "resume", result: null, fields: {} };

  const dom = {
    viewUpload: $("#view-upload"), viewReview: $("#view-review"),
    form: $("#upload-form"), dropzone: $("#dropzone"), fileInput: $("#file-input"),
    dzText: $("#dz-text"), dzSub: $("#dz-sub"), processBtn: $("#process-btn"), errorBox: $("#error-box"),
    fields: $("#fields"), grid: $("#review-grid"), sourcePanel: $("#source-panel"), sourceText: $("#source-text"),
    warnBox: $("#warning-box"), exportBtn: $("#export-btn"), verifyAllBtn: $("#verify-all-btn"),
    exportHint: $("#export-hint"), toast: $("#toast"), maxMb: Number($("#upload-form").dataset.maxMb || 10),
  };
  const defaultDzText = dom.dzText.innerHTML;
  const defaultDzSub = dom.dzSub.textContent;

  // --------------------------------------------------------------- helpers
  const isEmpty = (v) =>
    v === null || v === undefined || (typeof v === "string" && v.trim() === "") ||
    (Array.isArray(v) && v.every((row) => (typeof row === "object" && row !== null
      ? Object.entries(row).every(([k, x]) => k === "note" || k === "type" || String(x ?? "").trim() === "")
      : String(row).trim() === "")));

  let toastTimer;
  function toast(message) {
    dom.toast.textContent = message;
    dom.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (dom.toast.hidden = true), 2600);
  }

  function showError(message) {
    dom.errorBox.textContent = message;
    dom.errorBox.hidden = !message;
  }

  // ---------------------------------------------------------------- upload
  function setFile(file) {
    showError("");
    if (!file) return;
    if (!/\.pdf$/i.test(file.name)) {
      return rejectFile("Only PDF files are supported. Please choose a file ending in .pdf.");
    }
    if (file.size > dom.maxMb * 1024 * 1024) {
      return rejectFile(`That file is larger than ${dom.maxMb} MB.`);
    }
    if (file.size === 0) return rejectFile("That file is empty.");
    state.file = file;
    dom.dropzone.classList.add("has-file");
    dom.dzText.textContent = file.name;
    dom.dzSub.textContent = `${(file.size / 1024).toFixed(file.size > 1024 * 100 ? 0 : 1)} KB · ready to extract`;
    dom.processBtn.disabled = false;
  }

  function rejectFile(message) {
    state.file = null;
    dom.dropzone.classList.remove("has-file");
    dom.dzText.innerHTML = defaultDzText;
    dom.dzSub.textContent = defaultDzSub;
    dom.processBtn.disabled = true;
    dom.fileInput.value = "";
    showError(message);
  }

  dom.dropzone.addEventListener("click", () => dom.fileInput.click());
  dom.dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); dom.fileInput.click(); }
  });
  dom.fileInput.addEventListener("change", () => setFile(dom.fileInput.files[0]));
  ["dragenter", "dragover"].forEach((t) =>
    dom.dropzone.addEventListener(t, (e) => { e.preventDefault(); dom.dropzone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((t) =>
    dom.dropzone.addEventListener(t, (e) => { e.preventDefault(); dom.dropzone.classList.remove("drag"); }));
  dom.dropzone.addEventListener("drop", (e) => {
    const files = e.dataTransfer && e.dataTransfer.files;
    if (files && files.length) setFile(files[0]);
  });
  document.querySelectorAll('input[name="mode"]').forEach((r) =>
    r.addEventListener("change", () => (state.mode = r.value)));

  dom.form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.file) return showError("Choose a PDF file first.");
    state.mode = $('input[name="mode"]:checked').value;
    showError("");
    dom.processBtn.disabled = true;
    dom.processBtn.classList.add("loading");
    dom.processBtn.querySelector(".btn-label").textContent = "Extracting…";
    try {
      const body = new FormData();
      body.append("file", state.file, state.file.name);
      body.append("mode", state.mode);
      const res = await fetch("/api/extract", { method: "POST", body });
      let data = null;
      try { data = await res.json(); } catch { /* non-JSON error page */ }
      if (!res.ok || !data || !data.ok) {
        showError((data && data.error && data.error.message) || `The server returned an error (${res.status}).`);
        return;
      }
      render(data);
    } catch {
      showError("Could not reach the ExtractIQ server. Is it still running?");
    } finally {
      dom.processBtn.classList.remove("loading");
      dom.processBtn.disabled = !state.file;
      dom.processBtn.querySelector(".btn-label").textContent = "Extract data";
    }
  });

  // Sample documents (fictional files shipped in sample_documents/)
  document.querySelectorAll("[data-sample]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      showError("");
      try {
        const res = await fetch(`/samples/${encodeURIComponent(btn.dataset.sample)}`);
        if (!res.ok) throw new Error();
        const blob = await res.blob();
        const radio = $(`input[name="mode"][value="${btn.dataset.mode}"]`);
        if (radio) { radio.checked = true; state.mode = btn.dataset.mode; }
        setFile(new File([blob], btn.dataset.sample, { type: "application/pdf" }));
        if (state.file) dom.form.requestSubmit();
      } catch {
        showError("Could not load that sample file.");
      }
    }));

  // ---------------------------------------------------------------- render
  function render(data) {
    state.result = data;
    state.fields = {};
    for (const [key, f] of Object.entries(data.fields)) {
      state.fields[key] = {
        ...f,
        value: JSON.parse(JSON.stringify(f.value)),
        origStatus: f.status,
        edited: false,
      };
    }

    $("#meta-mode").textContent = MODE_LABELS[data.mode] || data.mode;
    $("#meta-filename").textContent = data.document.filename;
    $("#meta-sub").textContent =
      `${data.document.pages} page${data.document.pages === 1 ? "" : "s"} · ` +
      `${data.document.characters.toLocaleString()} characters extracted`;

    dom.warnBox.hidden = !(data.warnings && data.warnings.length);
    dom.warnBox.textContent = (data.warnings || []).join(" ");

    const hasSource = Boolean(data.document.text);
    dom.sourcePanel.hidden = !hasSource;
    dom.grid.classList.toggle("with-source", hasSource);
    dom.sourceText.textContent = data.document.text || "";

    dom.fields.replaceChildren(...Object.keys(state.fields).map(renderField));
    updateSummary();

    dom.viewUpload.hidden = true;
    dom.viewReview.hidden = false;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function renderField(key) {
    const f = state.fields[key];
    const card = el("article", { class: "card field", "data-key": key });
    const badge = el("span", { class: "badge" });
    const tags = el("span", { class: "tags" });
    const verifyBtn = el("button", { class: "verify-btn", type: "button", onclick: () => toggleVerified(key) });

    card.append(
      el("div", { class: "field-head" }, el("h3", {}, f.label), badge, tags, el("span", { class: "spacer" }), verifyBtn),
    );

    const body = el("div", { class: "field-body" });
    card.append(body);

    switch (f.type) {
      case "readonly":
        body.append(el("p", { class: "readonly-value" }, f.value));
        break;
      case "list":
        body.append(chipsEditor(key));
        break;
      case "records":
      case "keyinfo":
        body.append(recordsEditor(key));
        break;
      case "textarea":
      case "longtext": {
        const ta = el("textarea", {
          class: f.type === "longtext" ? "mono" : "", rows: f.type === "textarea" ? 4 : 10,
          "aria-label": f.label, placeholder: "Not found — type it here",
          oninput: (e) => { f.value = e.target.value; markEdited(key); },
        });
        ta.value = f.value || "";
        body.append(ta);
        break;
      }
      default: {
        body.append(el("input", {
          type: "text", value: f.value || "", "aria-label": f.label,
          placeholder: "Not found — type it here", autocomplete: "off",
          oninput: (e) => { f.value = e.target.value; markEdited(key); },
        }));
      }
    }

    if (f.issues && f.issues.length) {
      card.append(el("ul", { class: "issues" }, f.issues.map((i) => el("li", {}, i))));
    }
    if (f.source && f.type !== "readonly" && f.type !== "longtext") {
      card.append(el("details", { class: "source" },
        el("summary", {}, "Where this came from"), el("code", {}, f.source)));
    }

    card._parts = { badge, tags, verifyBtn };
    refreshCard(key, card);
    return card;
  }

  // -- editors ------------------------------------------------------------
  function chipsEditor(key) {
    const f = state.fields[key];
    const box = el("div", { class: "chips" });
    const input = el("input", { type: "text", placeholder: "Add a skill, press Enter", "aria-label": `Add to ${f.label}` });

    const paint = () => {
      box.replaceChildren(
        ...f.value.map((item, i) => el("span", { class: "chip" }, item,
          el("button", {
            type: "button", "aria-label": `Remove ${item}`, title: "Remove",
            onclick: () => { f.value.splice(i, 1); paint(); markEdited(key); },
          }, "×"))),
        input,
      );
    };
    const commit = () => {
      const parts = input.value.split(",").map((s) => s.trim()).filter(Boolean);
      if (!parts.length) return;
      for (const p of parts) if (!f.value.some((v) => v.toLowerCase() === p.toLowerCase())) f.value.push(p);
      input.value = "";
      paint(); input.focus(); markEdited(key);
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") { e.preventDefault(); commit(); }
      else if (e.key === "Backspace" && !input.value && f.value.length) {
        f.value.pop(); paint(); input.focus(); markEdited(key);
      }
    });
    input.addEventListener("blur", commit);
    paint();
    return box;
  }

  function recordsEditor(key) {
    const f = state.fields[key];
    const cols = COLUMNS[key];
    const wrap = el("div");
    const paint = () => {
      const empty = !f.value.length;
      const table = el("div", { class: "table-wrap" }, el("table", { class: "records" },
        el("thead", {}, el("tr", {}, cols.map((c) => el("th", { style: c.narrow ? "width:84px" : c.wide ? "width:34%" : "" }, c.label)),
          el("th", { style: "width:34px" }))),
        el("tbody", {}, f.value.map((row, i) => el("tr", {},
          cols.map((c) => el("td", {}, cell(row, c, i))),
          el("td", {}, el("button", {
            class: "del", type: "button", title: "Remove row", "aria-label": `Remove row ${i + 1}`,
            onclick: () => { f.value.splice(i, 1); paint(); markEdited(key); },
          }, "×")),
        ))),
      ));
      wrap.replaceChildren(
        empty ? el("p", { class: "empty-note" }, "No entries detected. Add one, or leave empty and verify if there are none.") : table,
        el("button", {
          class: "add-row", type: "button",
          onclick: () => { f.value.push(NEW_ROW[key]()); paint(); markEdited(key); },
        }, "+ Add row"),
      );
    };
    const cell = (row, c, i) => {
      if (c.readonly) return el("div", { class: "note" }, row[c.key] || "");
      if (c.options) {
        const sel = el("select", {
          "aria-label": `${c.label} ${i + 1}`,
          onchange: (e) => { row[c.key] = e.target.value; markEdited(key); },
        }, c.options.map((o) => el("option", { value: o }, o)));
        sel.value = c.options.includes(row[c.key]) ? row[c.key] : "Other";
        return sel;
      }
      return el("input", {
        type: "text", value: row[c.key] ?? "", "aria-label": `${c.label} ${i + 1}`,
        oninput: (e) => { row[c.key] = e.target.value; markEdited(key); },
      });
    };
    paint();
    return wrap;
  }

  // -- status handling ----------------------------------------------------
  function refreshCard(key, card = dom.fields.querySelector(`[data-key="${key}"]`)) {
    if (!card) return;
    const f = state.fields[key];
    card.dataset.status = f.status;
    card.classList.toggle("is-missing", f.type !== "readonly" && isEmpty(f.value));
    const { badge, tags, verifyBtn } = card._parts;
    badge.className = `badge badge-${f.status}`;
    badge.textContent = STATUS_LABELS[f.status];
    tags.replaceChildren(
      ...(f.type !== "readonly" && isEmpty(f.value) ? [el("span", { class: "badge badge-tag" }, "Missing")] : []),
      ...(f.edited ? [el("span", { class: "badge badge-tag" }, "Edited")] : []),
    );
    verifyBtn.textContent = f.status === "verified" ? "✓ Verified" : "Mark verified";
    verifyBtn.setAttribute("aria-pressed", String(f.status === "verified"));
  }

  function markEdited(key) {
    const f = state.fields[key];
    f.edited = true;
    f.status = "verified"; // a human touched it
    refreshCard(key);
    updateSummary();
  }

  function toggleVerified(key) {
    const f = state.fields[key];
    f.status = f.status === "verified" ? f.origStatus : "verified";
    refreshCard(key);
    updateSummary();
  }

  function updateSummary() {
    const all = Object.values(state.fields);
    const count = (s) => all.filter((f) => f.status === s).length;
    $("#c-extracted").textContent = count("extracted");
    $("#c-review").textContent = count("needs_review");
    $("#c-verified").textContent = count("verified");
    const verified = count("verified");
    $("#progress-fill").style.width = `${all.length ? (verified / all.length) * 100 : 0}%`;
    const ready = all.length > 0 && verified === all.length;
    dom.exportBtn.disabled = !ready;
    dom.verifyAllBtn.disabled = ready;
    dom.exportHint.textContent = ready
      ? "All fields verified. Ready to export."
      : `${all.length - verified} field${all.length - verified === 1 ? "" : "s"} still to verify before export.`;
  }

  dom.verifyAllBtn.addEventListener("click", () => {
    for (const key of Object.keys(state.fields)) {
      state.fields[key].status = "verified";
      refreshCard(key);
    }
    updateSummary();
    toast("All fields marked as verified.");
  });

  // ---------------------------------------------------------------- export
  function cleanRecords(key, rows) {
    const keep = rows.filter((r) => !isEmpty([r]));
    if (key === "education") return keep.map((r) => ({
      degree: r.degree.trim(), institution: r.institution.trim(), period: r.period.trim(), result: r.result.trim(),
    }));
    if (key === "experience") return keep.map((r) => ({
      role_company: r.title_company.trim(), period: r.period.trim(),
      months: String(r.months).trim() !== "" && !Number.isNaN(Number(r.months)) ? Number(r.months) : null,
    }));
    return keep.map((r) => ({ type: r.type, value: String(r.value).trim() })).filter((r) => r.value);
  }

  function buildExport() {
    const { result } = state;
    const data = {};
    const review = {};
    for (const [key, f] of Object.entries(state.fields)) {
      const name = EXPORT_KEY[key] || key;
      let value = f.value;
      if (f.type === "records" || f.type === "keyinfo") value = cleanRecords(key, value);
      else if (f.type === "list") value = value.map((s) => s.trim()).filter(Boolean);
      else if (typeof value === "string") value = value.trim();
      if (key === "total_experience" || key === "page_count") {
        value = value !== "" && !Number.isNaN(Number(value)) ? Number(value) : value;
      }
      data[name] = value;
      review[name] = { status: f.status, edited: f.edited };
    }
    return {
      tool: "ExtractIQ",
      exported_at: new Date().toISOString(),
      source: { filename: result.document.filename, pages: result.document.pages, mode: result.mode },
      all_fields_verified: Object.values(state.fields).every((f) => f.status === "verified"),
      data,
      review,
    };
  }

  dom.exportBtn.addEventListener("click", () => {
    if (dom.exportBtn.disabled) return;
    const payload = buildExport();
    const base = payload.source.filename.replace(/\.pdf$/i, "");
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }));
    const a = el("a", { href: url, download: `${base}.extractiq.json` });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("Exported verified data as JSON.");
  });

  // ----------------------------------------------------------------- reset
  $("#new-btn").addEventListener("click", () => {
    state.result = null; state.fields = {};
    rejectFile("");
    dom.viewReview.hidden = true;
    dom.viewUpload.hidden = false;
    window.scrollTo({ top: 0 });
  });

  // Exposed for testing/debugging in the console.
  window.ExtractIQ = { state, render, buildExport };
})();
