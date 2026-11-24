/* Fragment reassembly UI.
 *
 * All state comes from the JSON API; nothing is recomputed in the browser
 * except display geometry.  The solver's own provenance (status, bounds,
 * engines) is shown verbatim so a bound is never mistaken for an optimum.
 */
"use strict";

const S = {
  datasetId: null,
  payload: null,          // last /results/<v> payload
  resultVersion: null,
  zoom: 1,
  selectedPosition: null,
  errorsByFragment: new Map(),
  conflicts: new Set(),
  unavoidable: new Set(),
  orientation: {},
  fragmentsById: new Map(),
  hoveredFragment: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.class) node.setAttribute("class", opts.class);
  if (opts.text !== undefined) node.textContent = opts.text;
  if (opts.html !== undefined) node.innerHTML = opts.html;
  if (opts.value !== undefined) node.value = String(opts.value);
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) node.setAttribute(k, v);
  if (opts.on) for (const [k, v] of Object.entries(opts.on)) node.addEventListener(k, v);
  for (const c of children) if (c) node.appendChild(c);
  return node;
}

function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.setAttribute("class", "toast " + kind);
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 4200);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let body = null;
  const text = await res.text();
  try { body = text ? JSON.parse(text) : null; } catch { body = { message: text }; }
  if (!res.ok) {
    const message = (body && (body.message || body.error)) || `HTTP ${res.status}`;
    throw new Error(message);
  }
  return body;
}

function busy(button, on, label) {
  if (!button) return;
  if (on) {
    button.dataset.label = button.textContent;
    button.disabled = true;
    button.innerHTML = `<span class="spinner"></span>${label || "working…"}`;
  } else {
    button.disabled = false;
    button.textContent = button.dataset.label || button.textContent;
  }
}

/* ---------------- tabs ---------------- */
function showTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tabpanel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "reconstruct") drawCoverage();
}
$("#tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (btn) showTab(btn.dataset.tab);
});
$("#goto-data").addEventListener("click", () => showTab("data"));

/* ---------------- samples + datasets ---------------- */
async function loadSamples() {
  const data = await api("/api/samples");
  const wrap = $("#samples");
  wrap.innerHTML = "";
  for (const s of data) {
    wrap.appendChild(el("div", {
      class: "sample",
      on: {
        click: () => {
          $("#import-text").value = s.text;
          $("#import-name").value = s.name;
          toast(`Loaded sample “${s.title}” — press Import & solve`, "ok");
        },
        dblclick: () => importNow(s.text, s.name),
      },
    }, [
      el("h3", { text: s.title }),
      el("p", { text: s.description }),
      el("div", { class: "expect", text: "Expected: " + s.expectation }),
    ]));
  }
}

async function loadDatasets() {
  const data = await api("/api/datasets");
  const wrap = $("#dataset-list");
  wrap.innerHTML = "";
  if (!data.datasets.length) {
    wrap.appendChild(el("p", { class: "muted", text: "No datasets yet." }));
    return;
  }
  for (const d of data.datasets) {
    const latest = d.latest_result;
    const meta = latest
      ? `v${latest.version} · ${latest.status} · cost ${latest.cost}${latest.proven ? "" : " (bound)"}`
      : "no result";
    wrap.appendChild(el("div", {
      class: "dataset-item" + (d.id === S.datasetId ? " active" : ""),
      on: { click: () => openDataset(d.id) },
    }, [
      el("div", {}, [
        el("div", { text: d.name }),
        el("div", { class: "meta", text: `${d.n_versions} input version(s) · ${d.n_results} result(s)` }),
      ]),
      el("div", { class: "meta", text: meta }),
    ]));
  }
}

/* ---------------- import ---------------- */
async function importNow(text, name) {
  const btn = $("#btn-import");
  busy(btn, true, "solving…");
  $("#import-error").classList.add("hidden");
  try {
    const payload = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({
        text,
        name: name || $("#import-name").value || "synthetic import",
        options: collectOptions(),
      }),
    });
    S.datasetId = payload.dataset_id;
    applyPayload(payload);
    showTab("reconstruct");
    toast(`Solved: ${payload.result.status}, cost ${payload.result.cost}`, "ok");
    loadDatasets();
  } catch (err) {
    const box = $("#import-error");
    box.textContent = err.message;
    box.classList.remove("hidden");
    toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
}

function collectOptions() {
  return {
    engine: $("#import-engine") ? $("#import-engine").value : "auto",
    time_limit_s: Number($("#import-time") ? $("#import-time").value : 20),
  };
}

$("#btn-import").addEventListener("click", () => {
  const text = $("#import-text").value;
  if (!text.trim()) {
    toast("Paste some fragment text first (or click a sample).", "bad");
    return;
  }
  importNow(text, $("#import-name").value.trim());
});
$("#btn-clear").addEventListener("click", () => {
  $("#import-text").value = "";
  $("#import-name").value = "";
  $("#import-error").classList.add("hidden");
});

/* ---------------- open dataset / result ---------------- */
async function openDataset(datasetId, resultVersion) {
  S.datasetId = datasetId;
  try {
    let version = resultVersion;
    if (!version) {
      const list = await api(`/api/datasets/${encodeURIComponent(datasetId)}/results`);
      if (!list.results.length) throw new Error("this dataset has no results yet");
      version = list.results[0].version;
    }
    const payload = await api(
      `/api/datasets/${encodeURIComponent(datasetId)}/results/${version}`
    );
    applyPayload(payload);
    showTab("reconstruct");
    loadDatasets();
  } catch (err) {
    toast(err.message, "bad");
  }
}

function applyPayload(payload) {
  S.payload = payload;
  S.resultVersion = payload.result_version;
  S.selectedPosition = null;
  S.errorsByFragment = new Map();
  S.conflicts = new Set(payload.result.conflicts.conflict_fragments || []);
  S.unavoidable = new Set((payload.result.conflicts.unavoidable || []).map((u) => u.fragment));
  S.orientation = {};
  S.errorsByFragment = new Map();
  const primary = payload.result.solutions[0];
  if (primary) {
    Object.assign(S.orientation, primary.orientation || {});
    for (const [fid, positions] of Object.entries(primary.errors || {})) {
      S.errorsByFragment.set(fid, new Set(positions));
    }
  }
  S.fragmentsById = new Map((payload.dataset.fragments || []).map((f) => [f.fid, f]));

  $("#empty-state").classList.add("hidden");
  $("#workspace").classList.remove("hidden");
  renderStatus();
  renderSolveControls();
  renderSequences();
  renderFragmentTable();
  renderUndetermined();
  renderConflicts();
  renderEquivalence();
  renderHints();
  renderLocks();
  renderVersionTab();
  drawCoverage();
  loadJobLog();
  $("#export-input").href = `/api/datasets/${encodeURIComponent(S.datasetId)}/export/input`;
  $("#export-result").href =
    `/api/datasets/${encodeURIComponent(S.datasetId)}/export/result?version=${S.resultVersion}`;
}

/* ---------------- status ---------------- */
function renderStatus() {
  const r = S.payload.result;
  const strip = $("#status-strip");
  strip.innerHTML = "";
  const eq = r.equivalence;

  const statusKind = r.status === "optimal" && r.proven ? "good" : r.status === "timeout" ? "bad" : "warn";
  const stat = (k, v, sub, kind = "") =>
    el("div", { class: "stat " + kind }, [
      el("div", { class: "k", text: k }),
      el("div", { class: "v", text: v }),
      sub ? el("div", { class: "sub", text: sub }) : null,
    ]);

  strip.appendChild(stat("status", r.status, r.proven ? "proven optimal" : "bounds only", statusKind));
  strip.appendChild(stat("cost", r.cost, r.proven ? "errors in optimum" : `bound ${r.lower_bound}–${r.upper_bound}`));
  strip.appendChild(stat("blocks", r.n_components, `${r.reused_components} reused`, r.n_components > 1 ? "warn" : ""));
  strip.appendChild(stat("positions", r.n_positions, `${r.positions_detail.length} observed`));
  strip.appendChild(stat("fragments", r.dataset_summary.n_fragments,
    `${r.dataset_summary.n_connectors} connector(s)`));
  strip.appendChild(stat("undetermined", r.undetermined.length, "regions listed",
    r.undetermined.some((u) => u.kind === "gap") ? "warn" : ""));
  strip.appendChild(stat("flip-free blocks",
    (eq.per_component || []).filter((c) => c.flip_free).length,
    eq.labels_fixed_by_lock ? "labels fixed by lock" : "labels not fixed", eq.global_flip_free ? "warn" : ""));
  const solvable = eq.n_solutions_modulo_labels;
  strip.appendChild(stat("solutions", solvable == null ? "unknown" : solvable,
    eq.classes_known ? "modulo label swap" : "not enumerated", eq.classes_known ? "" : "warn"));

  if (r.warnings && r.warnings.length) {
    strip.appendChild(el("div", { class: "stat warn" }, [
      el("div", { class: "k", text: "warnings" }),
      el("div", { class: "sub", text: r.warnings.join(" · ") }),
    ]));
  }
}

/* ---------------- coverage canvas ---------------- */
function drawCoverage() {
  const canvas = $("#coverage-canvas");
  if (!canvas || !S.payload) return;
  const dpr = window.devicePixelRatio || 1;
  const cssWidth = canvas.parentElement.clientWidth || 900;
  const cssHeight = 330;
  canvas.width = Math.floor(cssWidth * dpr);
  canvas.height = Math.floor(cssHeight * dpr);
  canvas.style.width = cssWidth + "px";
  canvas.style.height = cssHeight + "px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const r = S.payload.result;
  const fragments = S.payload.dataset.fragments || [];
  const positions = r.positions;
  if (!positions.length) return;
  const minP = positions[0];
  const maxP = positions[positions.length - 1];
  const span = Math.max(1, maxP - minP);

  const pad = { l: 96, r: 16, t: 26, b: 34 };
  const rowH = Math.max(7, Math.min(20, (cssHeight - pad.t - pad.b) / Math.max(fragments.length, 1)));
  const rowsHeight = rowH * fragments.length;
  const x0 = pad.l;
  const plotW = cssWidth - pad.l - pad.r;
  const baseScale = plotW / span;
  const scale = baseScale * S.zoom;
  const visible = plotW / scale;
  const maxStart = Math.max(minP, maxP - visible);
  const viewStart = clampView(S.viewStart == null ? minP : S.viewStart, minP, maxStart);
  const xOf = (p) => x0 + (p - viewStart) * scale;

  // background blocks and gaps
  const blocks = r.coverage_blocks || [];
  ctx.fillStyle = "rgba(255,255,255,0.02)";
  for (const b of blocks) {
    const bx = xOf(b.start);
    const bw = Math.max(1, (b.end - b.start + 1) * scale);
    if (bx + bw < x0 || bx > x0 + plotW) continue;
    ctx.fillRect(bx, pad.t - 8, bw, rowsHeight + 16);
  }
  ctx.fillStyle = "rgba(240,180,41,0.10)";
  for (const g of r.gaps || []) {
    const gx = xOf(g.after + 1);
    const gw = Math.max(1, g.length * scale);
    ctx.fillRect(gx, pad.t - 8, gw, rowsHeight + 16);
  }

  // fragments as rows
  const order = fragments.slice().sort((a, b) => a.start - b.start || a.fid.localeCompare(b.fid));
  const compForPos = new Map();
  for (const c of r.components) for (const p of c.positions) compForPos.set(p, c);

  const fragRow = new Map();
  order.forEach((f, i) => fragRow.set(f.fid, i));

  for (const f of order) {
    const i = fragRow.get(f.fid);
    const y = pad.t + i * rowH;
    if (rowH < 9) continue;
    ctx.fillStyle = i % 2 ? "rgba(255,255,255,0.012)" : "transparent";
    ctx.fillRect(x0, y, plotW, rowH);
  }

  for (const f of order) {
    const i = fragRow.get(f.fid);
    const y = pad.t + i * rowH;
    const h = Math.max(3, rowH - 2);
    const errSet = S.errorsByFragment.get(f.fid) || new Set();
    for (const [ps, allele] of Object.entries(f.alleles)) {
      const p = Number(ps);
      const x = xOf(p);
      if (x < x0 - 2 || x > x0 + plotW + 2) continue;
      const comp = compForPos.get(p);
      let color = "#3d4a59";
      if (comp) {
        color = S.orientation[f.fid] === 0 ? "#3f7fae" : "#7d5aa0";
      }
      if (errSet.has(p)) color = "#ef6461";
      const w = Math.max(1.2, scale * 0.86);
      ctx.fillStyle = color;
      ctx.fillRect(x, y, w, h);
      if (S.selectedPosition === p) {
        ctx.strokeStyle = "#f0b429";
        ctx.lineWidth = 1;
        ctx.strokeRect(x - 1, y - 1, w + 2, h + 2);
      }
    }
    // error markers
    for (const p of errSet) {
      const x = xOf(p);
      if (x < x0 || x > x0 + plotW) continue;
      ctx.fillStyle = "#ef6461";
      ctx.beginPath();
      ctx.arc(x + Math.max(1, scale * 0.4), y + h / 2, Math.max(2, rowH * 0.28), 0, Math.PI * 2);
      ctx.fill();
    }
  }

  // locked positions
  ctx.strokeStyle = "rgba(240,180,41,0.75)";
  ctx.setLineDash([3, 3]);
  ctx.lineWidth = 1;
  for (const lock of S.payload.dataset.locks || []) {
    const x = xOf(lock.position);
    if (x < x0 || x > x0 + plotW) continue;
    ctx.beginPath();
    ctx.moveTo(x, pad.t - 10);
    ctx.lineTo(x, pad.t + rowsHeight + 8);
    ctx.stroke();
  }
  ctx.setLineDash([]);

  // hap track at bottom
  const trackY = pad.t + rowsHeight + 12;
  const trackH = 14;
  for (const c of r.components) {
    for (const p of c.positions) {
      const x = xOf(p);
      if (x < x0 - 1 || x > x0 + plotW + 1) continue;
      const w = Math.max(1, scale);
      const pair = c.alleles[String(p)];
      if (!pair) continue;
      ctx.fillStyle = pair[0] === 0 ? "#2f5c79" : "#5aa9e6";
      ctx.fillRect(x, trackY, w, trackH / 2);
      ctx.fillStyle = pair[1] === 0 ? "#5b3f70" : "#b07be0";
      ctx.fillRect(x, trackY + trackH / 2, w, trackH / 2);
      if (c.flip_free) {
        ctx.fillStyle = "rgba(240,180,41,0.16)";
        ctx.fillRect(x, trackY + trackH + 1, w, 3);
      }
    }
  }
  ctx.fillStyle = "#5c6b7a";
  ctx.font = "10px ui-monospace, Menlo, monospace";
  ctx.fillText("hap0/hap1", 6, trackY + 10);

  // axis
  const tickStep = niceStep(visible / 10);
  ctx.fillStyle = "#61707f";
  ctx.font = "10px ui-monospace, Menlo, monospace";
  ctx.strokeStyle = "rgba(255,255,255,0.07)";
  const firstTick = Math.ceil(viewStart / tickStep) * tickStep;
  for (let p = firstTick; p <= viewStart + visible; p += tickStep) {
    const x = xOf(p);
    if (x < x0 || x > x0 + plotW) continue;
    ctx.fillText(String(p), x - 6, pad.t + rowsHeight + 44);
    ctx.beginPath();
    ctx.moveTo(x, pad.t + rowsHeight + 30);
    ctx.lineTo(x, pad.t + rowsHeight + 34);
    ctx.stroke();
  }

  // fragment labels (only when there is room)
  if (rowH >= 11) {
    ctx.fillStyle = "#7b8b9c";
    ctx.font = "10px ui-monospace, Menlo, monospace";
    for (const f of order) {
      const i = fragRow.get(f.fid);
      const y = pad.t + i * rowH + rowH / 2 + 3;
      const marks = [];
      if ((S.errorsByFragment.get(f.fid) || new Set()).size) marks.push("!");
      if (S.conflicts.has(f.fid)) marks.push("C");
      ctx.fillText(f.fid + (marks.length ? " " + marks.join("") : ""), 6, y);
    }
  }

  canvas._geom = { x0, plotW, scale, viewStart, pad, rowH, rowsHeight, order, fragRow, minP, maxP, visible, baseScale };

  function clampView(v, lo, hi) {
    if (hi < lo) return lo;
    return Math.max(lo, Math.min(hi, v));
  }
  function niceStep(raw) {
    const pow = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1))));
    const mult = [1, 2, 5, 10].find((m) => pow * m >= raw) || 10;
    return pow * mult;
  }
}

$("#coverage-canvas").addEventListener("mousemove", (e) => {
  const canvas = e.currentTarget;
  const g = canvas._geom;
  const tip = $("#canvas-tip");
  if (!g || !S.payload) return;
  const rect = canvas.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const y = e.clientY - rect.top;
  const p = Math.round(g.viewStart + (x - g.x0) / g.scale);
  const row = Math.floor((y - g.pad.t) / g.rowH);
  const frag = g.order[row];
  const posRow = S.payload.result.positions_detail.find((r) => r.position === p);
  const inside = x >= g.x0 && x <= g.x0 + g.plotW && y >= g.pad.t - 8 && y <= g.pad.t + g.rowsHeight + 34;
  if (!inside) { tip.classList.add("hidden"); return; }
  let html = `<b>position ${p}</b>`;
  if (posRow) {
    html += `<br>coverage ${posRow.coverage} · block ${posRow.component}`;
    html += `<br>hap0 ${posRow.hap0 ?? "–"} · hap1 ${posRow.hap1 ?? "–"}`;
    if (posRow.lock) html += `<br>lock: ${escapeHtml(posRow.lock)}`;
    if (posRow.orientation_ambiguous) html += `<br>pair ambiguous`;
    if (posRow.fragments.length) html += `<br>fragments: ${posRow.fragments.join(", ")}`;
  } else {
    html += `<br>no observation here`;
  }
  if (frag && y >= g.pad.t && y < g.pad.t + g.rowsHeight) {
    html += `<br><span style="color:#8b9aab">row: ${frag.fid}</span>`;
  }
  tip.innerHTML = html;
  tip.classList.remove("hidden");
  const maxX = canvas.parentElement.clientWidth - 290;
  tip.style.left = Math.min(Math.max(6, x + 14), Math.max(6, maxX)) + "px";
  tip.style.top = Math.min(y + 12, 210) + "px";
});
$("#coverage-canvas").addEventListener("mouseleave", () => $("#canvas-tip").classList.add("hidden"));
$("#coverage-canvas").addEventListener("click", (e) => {
  const g = e.currentTarget._geom;
  if (!g) return;
  const rect = e.currentTarget.getBoundingClientRect();
  const p = Math.round(g.viewStart + (e.clientX - rect.left - g.x0) / g.scale);
  S.selectedPosition = p;
  $("#lock-pos").value = p;
  drawCoverage();
});

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function zoomAround(factor, anchorPosition) {
  const prev = S.zoom;
  S.zoom = Math.max(1, Math.min(64, S.zoom * factor));
  if (S.zoom !== prev && S.payload) {
    const positions = S.payload.result.positions;
    const minP = positions[0];
    const maxP = positions[positions.length - 1];
    const g = $("#coverage-canvas")._geom;
    const anchor = anchorPosition != null ? anchorPosition
      : (g ? g.viewStart + (g.visible / 2) : minP);
    const plotWidth = g ? g.plotW : 800;
    const base = plotWidth / Math.max(1, maxP - minP);
    const visible = plotWidth / (base * S.zoom);
    S.viewStart = Math.max(minP, Math.min(maxP - visible, anchor - visible / 2));
  }
  if (S.zoom === 1) S.viewStart = null;
  drawCoverage();
  updateZoom();
}
$("#zoom-in").addEventListener("click", () => zoomAround(1.6, S.selectedPosition));
$("#zoom-out").addEventListener("click", () => zoomAround(1 / 1.6, S.selectedPosition));
$("#zoom-reset").addEventListener("click", () => { S.zoom = 1; S.viewStart = null; drawCoverage(); updateZoom(); });

// drag to pan when zoomed in
(function panSetup() {
  const canvas = $("#coverage-canvas");
  let drag = null;
  canvas.addEventListener("mousedown", (e) => {
    const g = canvas._geom;
    if (!g || S.zoom === 1) return;
    drag = { x: e.clientX, start: g.viewStart, scale: g.scale };
    canvas.style.cursor = "grabbing";
  });
  window.addEventListener("mouseup", () => { if (drag) { drag = null; canvas.style.cursor = "crosshair"; } });
  window.addEventListener("mousemove", (e) => {
    if (!drag) return;
    S.viewStart = drag.start - (e.clientX - drag.x) / drag.scale;
    drawCoverage();
  });
})();
function updateZoom() { $("#zoom-label").textContent = `zoom ${S.zoom.toFixed(1)}×`; }

/* ---------------- sequences ---------------- */
function renderSequences() {
  const r = S.payload.result;
  const view = $("#sequence-view");
  const select = $("#sequence-select");
  view.innerHTML = "";
  select.innerHTML = "";
  r.solutions.forEach((sol, i) => {
    select.appendChild(el("option", {
      value: String(i),
      text: `solution ${sol.id} · cost ${sol.cost}${i === 0 ? " (representative)" : ""}`,
    }));
  });
  select._solutions = r.solutions;
  select.onchange = () => paintSequence(Number(select.value));

  const blocks = r.coverage_blocks || [];
  const blockWrap = $("#block-summary");
  blockWrap.innerHTML = "";
  const compByPos = new Map();
  for (const c of r.components) for (const p of c.positions) compByPos.set(p, c);

  blocks.forEach((b, bi) => {
    const comp = compByPos.get(b.start);
    const row = el("div", { class: "block-row" }, [
      el("div", { class: "bid", text: `block ${bi}` }),
      el("div", { class: "cell" }, [el("span", { class: "k", text: "coords" }), el("span", { text: `${b.start}–${b.end}` })]),
      el("div", { class: "cell" }, [el("span", { class: "k", text: "positions" }), el("span", { text: String(b.n_positions) })]),
      el("div", { class: "cell" }, [el("span", { class: "k", text: "component" }), el("span", { text: comp ? String(comp.index) : "–" })]),
      el("div", { class: "cell" }, [el("span", { class: "k", text: "determination" }),
        el("span", {}, [
          comp && comp.flip_free ? el("span", { class: "badge warn", text: "up to flip" }) : el("span", { class: "badge ok", text: "ordered" }),
          comp && comp.status !== "optimal" ? el("span", { class: "badge bad", text: "bound only" }) : null,
        ])]),
    ]);
    blockWrap.appendChild(row);
  });

  paintSequence(0);
}

function paintSequence(index) {
  const sols = $("#sequence-select")._solutions || [];
  const sol = sols[index];
  if (!sol) return;
  const r = S.payload.result;
  const view = $("#sequence-view");
  view.innerHTML = "";
  const positions = r.positions;
  const lockSet = new Set((S.payload.dataset.locks || []).map((l) => l.position));
  const errPositionsBySide = {};
  for (const [fid, ps] of Object.entries(sol.errors)) {
    const side = sol.orientation[fid];
    (errPositionsBySide[side] = errPositionsBySide[side] || new Set());
    for (const p of ps) errPositionsBySide[side].add(p);
  }

  const blocks = r.coverage_blocks || [];
  blocks.forEach((b, bi) => {
    const startIdx = positions.indexOf(b.start);
    const endIdx = positions.indexOf(b.end);
    if (startIdx < 0) return;
    const block = el("div", { class: "seq-block" });
    block.appendChild(el("h3", { text: `block ${bi} · positions ${b.start}–${b.end}` }));
    for (const [name, seq, side] of [["hap0", sol.hap0, 0], ["hap1", sol.hap1, 1]]) {
      const cells = el("div", { class: "cells" });
      for (let i = startIdx; i <= endIdx; i++) {
        const p = positions[i];
        const ch = seq[i];
        const cls = ch === "-" ? "gap" : ch === "0" ? "c0" : "c1";
        const isErr = errPositionsBySide[side] && errPositionsBySide[side].has(p);
        const span = el("span", { class: cls + (isErr ? " err" : "") + (lockSet.has(p) ? " lk" : ""), text: ch });
        cells.appendChild(span);
      }
      block.appendChild(el("div", { class: "seq-line" }, [
        el("div", { class: "label", text: name }),
        cells,
      ]));
    }
    // ruler: show first / middle / last coordinate
    const mid = positions[Math.floor((startIdx + endIdx) / 2)];
    const ruler = el("div", { class: "seq-ruler" }, [
      el("div", { class: "label", text: "" }),
      el("div", { class: "cells", text: `${positions[startIdx]}${" ".repeat(Math.max(0, Math.floor((endIdx - startIdx) / 2) - String(positions[startIdx]).length))}${mid} … ${positions[endIdx]}` }),
    ]);
    block.appendChild(ruler);
    view.appendChild(block);
  });

  if (sol.per_component_flip && sol.per_component_flip.length) {
    const note = el("div", { class: "item flip" }, [
      el("div", { class: "head" }, [el("b", { text: "free flip available" })]),
      el("p", { text: "These blocks can have hap0/hap1 swapped with identical cost: " +
        sol.per_component_flip.map((x) => `block ${x.component}`).join(", ") }),
    ]);
    view.appendChild(note);
  }
}

/* ---------------- fragment table ---------------- */
function renderFragmentTable() {
  const r = S.payload.result;
  const tbody = $("#fragment-table").querySelector("tbody");
  tbody.innerHTML = "";
  const positions = r.positions;
  const frags = S.payload.dataset.fragments || [];
  const compOfFrag = new Map();
  for (const c of r.components) for (const fid of c.fragment_ids) compOfFrag.set(fid, c.index);
  const filter = $("#fragment-filter").value;
  let shown = 0;
  const sol = r.solutions[0];

  for (const f of frags) {
    const errs = (sol.errors[f.fid] || []);
    const isConflict = S.conflicts.has(f.fid);
    const isUnavoidable = S.unavoidable.has(f.fid);
    if (filter === "errors" && !errs.length) continue;
    if (filter === "conflicts" && !isConflict && !isUnavoidable) continue;
    if (filter === "connectors" && !f.connector) continue;
    shown++;
    const side = sol.orientation[f.fid];
    const errSet = new Set(errs);
    const coordStart = Math.max(0, positions.indexOf(f.start) - 1);
    const coordEnd = Math.min(positions.length, positions.indexOf(f.end) + 2);
    const cmp = el("td", { class: "compare-cells" });
    const cmpText = [];
    for (let i = coordStart; i < coordEnd; i++) {
      const p = positions[i];
      const a = f.alleles[String(p)];
      if (a === undefined) { cmpText.push("·"); continue; }
      const hap = side === 0 ? (sol.hap0[i] || "-") : (sol.hap1[i] || "-");
      const bad = errSet.has(p);
      cmpText.push(`<span class="${bad ? "m" : ""}" title="p${p}: call ${a}, haplotype ${hap}">${a}</span>`);
    }
    cmp.innerHTML = cmpText.join("");
    const rowClass = [errs.length ? "is-error" : "", isUnavoidable ? "is-unavoidable" : ""]
      .filter(Boolean).join(" ");
    const tr = el("tr", {
      class: rowClass,
      on: {
        mouseenter: () => { S.hoveredFragment = f.fid; },
        mouseleave: () => { S.hoveredFragment = null; },
      },
    }, [
      el("td", {}, [
        el("span", { class: "mono", text: f.fid }),
        f.connector ? el("span", { class: "badge", text: " connector" }) : null,
        isUnavoidable ? el("span", { class: "badge bad", text: " conflict" }) : null,
      ]),
      el("td", { text: compOfFrag.has(f.fid) ? String(compOfFrag.get(f.fid)) : "–" }),
      el("td", {}, [
        el("span", { class: "badge " + (side === 0 ? "" : "warn"), text: side === 0 ? "hap0" : "hap1" }),
      ]),
      el("td", { class: "mono", text: `${f.start}–${f.end}` }),
      el("td", { class: "mono", text: `${Object.keys(f.alleles).length}` }),
      el("td", {}, errs.length
        ? errs.map((p) => el("span", { class: "chip bad", text: "p" + p }))
        : [el("span", { class: "ok-pos", text: "none" })]),
      cmp,
    ]);
    tbody.appendChild(tr);
  }
  if (!shown) {
    tbody.appendChild(el("tr", {}, [el("td", {
      class: "muted",
      attrs: { colspan: "7" },
      text: "No fragments match this filter.",
    })]));
  }
  $("#fragment-count").textContent = `${shown} of ${frags.length} shown`;
}

$("#fragment-filter").addEventListener("change", renderFragmentTable);

/* ---------------- undetermined / conflicts ---------------- */
function renderUndetermined() {
  const r = S.payload.result;
  const wrap = $("#undetermined");
  wrap.innerHTML = "";
  if (!r.undetermined.length) {
    wrap.appendChild(el("div", { class: "item" }, [el("p", { text: "Nothing undetermined: every block is ordered by locks and every coordinate is covered." })]));
    return;
  }
  for (const u of r.undetermined) {
    const kindClass = u.kind === "gap" ? "gap" : u.kind === "flip" ? "flip" : u.kind === "unproven" ? "unproven" : "pairs";
    const title = {
      gap: "coordinate gap",
      flip: "free flip",
      unproven: "unproven optimum",
      position_pairs: "ambiguous allele pairs",
    }[u.kind] || u.kind;
    const item = el("div", { class: "item " + kindClass }, [
      el("div", { class: "head" }, [
        el("b", { text: title + (u.start != null ? ` · ${u.start}–${u.end}` : "") }),
        u.component != null ? el("span", { class: "badge", text: `block ${u.component}` }) : null,
      ]),
      el("p", { text: u.note || "" }),
    ]);
    if (u.positions) {
      const chips = el("div", { class: "chips" });
      for (const p of u.positions.slice(0, 40)) chips.appendChild(el("span", { class: "chip", text: "p" + p }));
      if (u.positions.length > 40) chips.appendChild(el("span", { class: "chip", text: `+${u.positions.length - 40} more` }));
      item.appendChild(chips);
    }
    wrap.appendChild(item);
  }
}

function renderConflicts() {
  const r = S.payload.result;
  const c = r.conflicts;
  const wrap = $("#conflicts");
  wrap.innerHTML = "";
  if (!c.conflict_fragments.length && !c.unavoidable.length) {
    wrap.appendChild(el("div", { class: "item" }, [el("p", { text: "No fragment contradicts the current locks." })]));
  }
  for (const u of c.unavoidable) {
    wrap.appendChild(el("div", { class: "item unavoidable" }, [
      el("div", { class: "head" }, [
        el("b", { text: "unavoidable: " + u.fragment }),
        el("span", { class: "badge warn", text: "no orientation works" }),
      ]),
      el("p", { text: u.explanation }),
      el("div", { class: "chips" }, u.conflicting_positions.map((p) => el("span", { class: "chip bad", text: "p" + p }))),
    ]));
  }
  for (const lock of c.locks) {
    if (lock.status !== "contradicted") continue;
    wrap.appendChild(el("div", { class: "item contradicted" }, [
      el("div", { class: "head" }, [
        el("b", { text: "lock contradicted: " + lock.lock }),
        el("span", { class: "badge bad", text: `${lock.n_conflicts} fragment(s)` }),
      ]),
      el("div", { class: "chips" }, lock.conflict_fragments.map((fid) => el("span", { class: "chip bad", text: fid }))),
    ]));
  }
}

/* ---------------- equivalence / compare ---------------- */
function renderEquivalence() {
  const r = S.payload.result;
  const eq = r.equivalence;
  const wrap = $("#equivalence");
  wrap.innerHTML = "";
  const summary = el("div", { class: "item" }, [
    el("div", { class: "head" }, [
      el("b", { text: "solution count" }),
      el("span", { class: "badge " + (eq.classes_known ? "ok" : "warn"),
        text: eq.classes_known ? String(eq.n_solutions_modulo_labels) : "unknown" }),
    ]),
    el("p", { text: eq.classes_known
      ? `Distinct (hap0, hap1) reconstructions across all blocks: ${eq.raw_classes_product}` +
        `${eq.label_quotient > 1 ? `, halved by the free global label swap (${eq.palindromic_reconstructions} reconstruction(s) are their own swap)` : ""}.`
      : "Some block's optimal set was not fully enumerated, so the count is unknown." }),
  ]);
  wrap.appendChild(summary);

  for (const pc of eq.per_component) {
    wrap.appendChild(el("div", { class: "item" }, [
      el("div", { class: "head" }, [
        el("b", { text: `block ${pc.component}` }),
        el("span", {}, [
          pc.flip_free ? el("span", { class: "badge warn", text: "labels swap freely" }) : null,
          pc.classes_known ? el("span", { class: "badge", text: `${pc.classes} reconstruction(s)` })
                           : el("span", { class: "badge warn", text: "count unknown" }),
        ]),
      ]),
      el("p", { text: `${pc.variants_listed} reconstruction(s) listed${pc.variants_truncated ? ", truncated" : ""}.` }),
    ]));
  }

  const selectA = $("#cmp-a"), selectB = $("#cmp-b");
  selectA.innerHTML = ""; selectB.innerHTML = "";
  r.solutions.forEach((sol, i) => {
    const label = `solution ${sol.id} · cost ${sol.cost}`;
    selectA.appendChild(el("option", { value: String(sol.id), text: label }));
    selectB.appendChild(el("option", { value: String(sol.id), text: label }));
  });
  if (r.solutions.length > 1) selectB.value = String(r.solutions[1].id);
}

async function compareSolutions() {
  const a = $("#cmp-a").value, b = $("#cmp-b").value;
  const out = $("#compare-output");
  out.innerHTML = "";
  try {
    const data = await api(
      `/api/datasets/${encodeURIComponent(S.datasetId)}/compare?result=${S.resultVersion}&a=${a}&b=${b}`
    );
    if (!data.n_differing_positions) {
      out.appendChild(el("div", { class: "summary" }, [
        el("span", { class: "badge ok", text: "identical" }),
        el("span", { text: " The two listed solutions have the same haplotype pair at every position." }),
      ]));
      return;
    }
    out.appendChild(el("div", { class: "summary" }, [
      el("span", { class: "badge warn", text: `${data.n_differing_positions} differing position(s)` }),
      el("span", { text: data.flip_only_difference ? " — all differences are an exact hap0/hap1 swap (free flip)." : "" }),
    ]));
    const grid = el("div", { class: "diff-grid" });
    const lines = data.differing_positions.slice(0, 80).map((d) =>
      `p${String(d.position).padStart(4)}  A=${d.a[0]}${d.a[1]}  B=${d.b[0]}${d.b[1]}${d.flipped_only ? "  (swap)" : ""}`
    );
    grid.textContent = lines.join("\n");
    out.appendChild(grid);
  } catch (err) {
    out.appendChild(el("div", { class: "alert", text: err.message }));
  }
}
$("#btn-compare").addEventListener("click", compareSolutions);

/* ---------------- hints ---------------- */
async function renderHints() {
  try {
    const data = await api(`/api/datasets/${encodeURIComponent(S.datasetId)}/hints?result=${S.resultVersion}`);
    const wrap = $("#hints");
    wrap.innerHTML = "";
    if (!data.hints.length) {
      wrap.appendChild(el("p", { class: "muted", text: "Nothing to suggest right now." }));
      return;
    }
    for (const h of data.hints) {
      wrap.appendChild(el("div", { class: "item" }, [
        el("div", { class: "head" }, [el("b", { text: h.kind.replace("_", " ") })]),
        el("p", { text: h.suggestion }),
      ]));
    }
  } catch (err) {
    $("#hints").textContent = "";
  }
}

/* ---------------- locks ---------------- */
function renderLocks() {
  const wrap = $("#lock-list");
  wrap.innerHTML = "";
  const locks = S.payload.dataset.locks || [];
  if (!locks.length) {
    wrap.appendChild(el("p", { class: "muted", text: "No positions pinned." }));
    return;
  }
  for (const lock of locks) {
    wrap.appendChild(el("div", { class: "lock-row" }, [
      el("span", {}, [
        el("span", { class: "chip lock", text: lock.description }),
      ]),
      el("button", {
        class: "ghost small",
        text: "unpin",
        on: { click: () => unlockPosition(lock.position) },
      }),
    ]));
  }
}

async function unlockPosition(position) {
  if (!S.datasetId) return;
  try {
    const payload = await api(`/api/datasets/${encodeURIComponent(S.datasetId)}/unlock`, {
      method: "POST",
      body: JSON.stringify({ position, options: { engine: $("#solve-engine").value, time_limit_s: Number($("#solve-time").value) } }),
    });
    applyPayload(payload);
    toast(`Unpinned position ${position}`, "ok");
    loadDatasets();
  } catch (err) { toast(err.message, "bad"); }
}

$("#btn-lock").addEventListener("click", async () => {
  if (!S.datasetId) return;
  const position = Number($("#lock-pos").value);
  if (!Number.isInteger(position)) { toast("Enter a position number", "bad"); return; }
  const hap0 = $("#lock-hap0").value === "" ? null : Number($("#lock-hap0").value);
  const hap1 = $("#lock-hap1").value === "" ? null : Number($("#lock-hap1").value);
  const distinct = $("#lock-distinct").checked;
  if (hap0 === null && hap1 === null && !distinct) {
    toast("Choose hap0, hap1, or distinct", "bad");
    return;
  }
  const btn = $("#btn-lock");
  busy(btn, true, "re-solving…");
  try {
    const payload = await api(`/api/datasets/${encodeURIComponent(S.datasetId)}/locks`, {
      method: "POST",
      body: JSON.stringify({ position, hap0, hap1, distinct, options: { engine: $("#solve-engine").value, time_limit_s: Number($("#solve-time").value) } }),
    });
    applyPayload(payload);
    toast(`Pinned p${position} · ${payload.job.report.summary}`, "ok");
    loadDatasets();
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
});

/* ---------------- connector ---------------- */
$("#btn-connector").addEventListener("click", async () => {
  if (!S.datasetId) return;
  const fid = $("#conn-fid").value.trim();
  const raw = $("#conn-alleles").value.trim();
  if (!fid) { toast("Give the connector an id", "bad"); return; }
  const alleles = {};
  for (const part of raw.split(/[,\s]+/).filter(Boolean)) {
    const m = part.match(/^(-?\d+)\s*[:=]\s*([01])$/);
    if (!m) { toast(`Cannot parse “${part}”. Use position:allele, e.g. 12:0`, "bad"); return; }
    alleles[m[1]] = Number(m[2]);
  }
  if (!Object.keys(alleles).length) { toast("Add at least one observation", "bad"); return; }
  const btn = $("#btn-connector");
  busy(btn, true, "re-solving…");
  try {
    const payload = await api(`/api/datasets/${encodeURIComponent(S.datasetId)}/connectors`, {
      method: "POST",
      body: JSON.stringify({ fid, alleles, options: { engine: $("#solve-engine").value, time_limit_s: Number($("#solve-time").value) } }),
    });
    applyPayload(payload);
    toast(payload.job.report.summary, "ok");
    loadDatasets();
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
});

/* ---------------- re-solve ---------------- */
$("#btn-resolve").addEventListener("click", async () => {
  if (!S.datasetId) return;
  const btn = $("#btn-resolve");
  busy(btn, true, "solving…");
  try {
    // A no-op edit of the same input still exercises the incremental path:
    // re-posting the current document through the same lock endpoint would
    // change inputs.  Instead, ask for the latest result with new options by
    // importing nothing: use the unlock endpoint on a non-existent lock is an
    // error, so the honest option is to re-solve through the API job route.
    const payload = await api(`/api/datasets/${encodeURIComponent(S.datasetId)}/resolve`, {
      method: "POST",
      body: JSON.stringify({ options: { engine: $("#solve-engine").value, time_limit_s: Number($("#solve-time").value) } }),
    });
    applyPayload(payload);
    toast(`Re-solved: ${payload.result.status}, cost ${payload.result.cost}`, "ok");
    loadDatasets();
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
});

function renderSolveControls() {
  const opts = S.payload.solve || {};
  if (opts.engine) $("#solve-engine").value = opts.engine;
  if (opts.time_limit_s) $("#solve-time").value = opts.time_limit_s;
}

/* ---------------- job log ---------------- */
async function loadJobLog() {
  if (!S.datasetId) return;
  try {
    const count = Number($("#job-count").value || 25);
    const data = await api(`/api/datasets/${encodeURIComponent(S.datasetId)}/jobs`);
    const wrap = $("#job-log");
    wrap.innerHTML = "";
    const jobs = (data.jobs || []).slice(0, count);
    if (!jobs.length) { wrap.appendChild(el("p", { class: "muted", text: "No jobs yet." })); return; }
    for (const j of jobs) {
      const rep = j.report || {};
      const change = rep.change || {};
      wrap.appendChild(el("div", { class: "job item" }, [
        el("div", { class: "when", text: (j.created_at || "").replace("T", " ").replace("+00:00", "Z") }),
        el("div", {}, [el("span", { class: "badge " + (j.status === "ok" ? "ok" : "bad"), text: j.action })]),
        el("div", { class: "what", text: rep.summary || j.error || "—" }),
        el("div", { class: "muted", text: `${j.elapsed_ms} ms` }),
      ]));
    }
  } catch (err) { /* non-fatal */ }
}
$("#job-count").addEventListener("change", loadJobLog);

/* ---------------- versions tab ---------------- */
function renderVersionTab() {
  const wrapIn = $("#input-versions");
  wrapIn.innerHTML = "";
  const dsId = S.datasetId;
  api(`/api/datasets/${encodeURIComponent(dsId)}`).then((view) => {
    for (const v of view.versions.slice().reverse()) {
      wrapIn.appendChild(el("div", { class: "item vr" }, [
        el("div", {}, [
          el("b", { text: `input v${v.seq}` }),
          el("span", { class: "muted", text: " · " + (v.label || "edit") }),
        ]),
        el("div", { class: "muted", text: `${v.n_results} result(s) · ${v.created_at.replace("T", " ")}` }),
      ]));
    }
    if (!view.versions.length) wrapIn.appendChild(el("p", { class: "muted", text: "No versions." }));
  });

  const wrapOut = $("#result-versions");
  wrapOut.innerHTML = "";
  const results = S.payload.result_versions || [];
  for (const rv of results) {
    const isCurrent = rv.version === S.resultVersion;
    const row = el("div", {
      class: "item vr clickable" + (isCurrent ? " current" : ""),
      on: { click: () => openDataset(dsId, rv.version) },
    }, [
      el("div", {}, [
        el("b", { text: `result v${rv.version}` }),
        el("span", { class: "muted", text: " · " + (rv.action || "") }),
        isCurrent ? el("span", { class: "badge ok", text: " shown" }) : null,
      ]),
      el("div", { class: "muted",
        text: `${rv.summary.status} · cost ${rv.summary.cost}${rv.summary.proven ? "" : " (bound)"} · input v${rv.input_seq ?? "?"}` }),
    ]);
    wrapOut.appendChild(row);
  }

  const selA = $("#ver-a"), selB = $("#ver-b");
  selA.innerHTML = ""; selB.innerHTML = "";
  for (const rv of results.slice().reverse()) {
    const label = `v${rv.version} · ${rv.summary.status} · cost ${rv.summary.cost}`;
    selA.appendChild(el("option", { value: String(rv.version), text: label }));
    selB.appendChild(el("option", { value: String(rv.version), text: label }));
  }
  if (results.length > 1) {
    selA.value = String(results[1].version);
    selB.value = String(results[0].version);
  }
}

$("#btn-compare-versions").addEventListener("click", async () => {
  if (!S.datasetId) return;
  const a = $("#ver-a").value, b = $("#ver-b").value;
  const out = $("#version-compare-output");
  out.innerHTML = "";
  if (a === b) { toast("Pick two different versions", "bad"); return; }
  try {
    const data = await api(`/api/datasets/${encodeURIComponent(S.datasetId)}/versions/compare?a=${a}&b=${b}`);
    out.appendChild(el("div", { class: "summary" }, [
      el("span", { class: "badge " + (data.identical ? "ok" : "warn"),
        text: data.identical ? "identical sequences" : `${data.n_differing_positions} differing position(s)` }),
      el("span", { text: ` cost ${data.a.cost} → ${data.b.cost}; blocks ${data.a.n_components} → ${data.b.n_components}` }),
    ]));
    if (data.differing_positions.length) {
      const grid = el("div", { class: "diff-grid" });
      grid.textContent = data.differing_positions.slice(0, 120)
        .map((d) => `p${String(d.position).padStart(4)}  v${a}=${fmtPair(d.a)}  v${b}=${fmtPair(d.b)}`)
        .join("\n");
      out.appendChild(grid);
    }
  } catch (err) {
    out.appendChild(el("div", { class: "alert", text: err.message }));
  }
  function fmtPair(pair) {
    return pair ? `${pair[0]}${pair[1]}` : "—";
  }
});

/* ---------------- boot ---------------- */
window.addEventListener("resize", () => { if (S.payload) drawCoverage(); });

// Expose a tiny handle for headless integration tests.  Read-only in practice:
// the UI itself never depends on this object.
window.__app = {
  state: S,
  openDataset,
  showTab,
  importNow,
  compareSolutions,
  renderFragmentTable,
  drawCoverage,
  loadDatasets,
};
document.addEventListener("DOMContentLoaded", async () => {
  await loadSamples();
  await loadDatasets();
  updateZoom();
});
