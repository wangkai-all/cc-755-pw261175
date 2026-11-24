"use strict";
/* Full UI flow against a live server.
 *
 * Drives the real app.js through the import → inspect → connector → lock →
 * versions sequence and returns a compact observation record.  The Python test
 * asserts on the returned object, so every check runs against the actual DOM
 * the UI built, not against a mock.
 */
const S = window.__app.state;
const q = (sel) => document.querySelector(sel);
const qq = (sel) => Array.from(document.querySelectorAll(sel));
const txt = (n) => (n ? n.textContent : null);
const click = (id) => q(id).dispatchEvent({ type: "click", bubbles: true });
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const out = {};

async function importSample(name) {
  const sample = await (await fetch(`/api/samples/${name}`)).json();
  await importNow(sample.text, name);
}

// 1. import a two-block fixture through the real import handler
await importSample("two_blocks");
await wait(300);
out.imported = {
  status: S.payload.result.status,
  cost: S.payload.result.cost,
  blocks: S.payload.result.n_components,
  version: S.resultVersion,
  activeTab: qq(".tabpanel.active").map((p) => p.id),
  canvasOps: q("#coverage-canvas").getContext("2d").ops.length,
  stats: qq("#status-strip .stat").map((s) => `${txt(s.querySelector(".k"))}=${txt(s.querySelector(".v"))}`),
  sequenceBlocks: qq("#sequence-view .seq-block").length,
  fragmentRows: qq("#fragment-table tbody tr").length,
  solutionOptions: q("#cmp-a").options.length,
  hints: qq("#hints .item").length,
};

// 2. compare two equivalent solutions before any lock
q("#cmp-a").value = "0";
q("#cmp-b").value = "1";
await compareSolutions();
out.compareBeforeLock = txt(q("#compare-output")).slice(0, 200);
out.datasetId = S.datasetId;

// 3. add a connector spanning the gap: blocks merge, unrelated work is reused
q("#conn-fid").value = "bridge";
q("#conn-alleles").value = "4:0 5:1 6:0 7:1";
click("#btn-connector");
await wait(900);
out.afterConnector = {
  version: S.resultVersion,
  blocks: S.payload.result.n_components,
  reused: S.payload.result.reused_components,
  action: S.payload.job.action,
  report: S.payload.job.report.summary,
  connectorRows: qq("#fragment-table tr").filter((tr) => tr.textContent.includes("bridge")).length,
};

// 4. pin a position: it becomes a constraint, and the labels are now fixed
const before = S.payload.result.equivalence.n_solutions_modulo_labels;
q("#lock-pos").value = "3";
q("#lock-hap0").value = "1";
q("#lock-hap1").value = "0";
q("#lock-distinct").checked = true;
click("#btn-lock");
await wait(900);
out.afterLock = {
  locks: S.payload.dataset.locks.map((l) => l.description),
  lockRows: qq("#lock-list .lock-row").length,
  classesBefore: before,
  classesAfter: S.payload.result.equivalence.n_solutions_modulo_labels,
  labelsFixed: S.payload.result.equivalence.labels_fixed_by_lock,
  action: S.payload.job.action,
};

// 5. per-fragment errors on the single_error fixture
await importSample("single_error");
out.errorFixture = {
  cost: S.payload.result.cost,
  errorRows: qq("#fragment-table tbody tr.is-error").length,
  errorChips: qq("#fragment-table .chip.bad").map(txt),
  undeterminedKinds: [...new Set(S.payload.result.undetermined.map((u) => u.kind))].sort(),
};

// 6. reopen the dataset that accumulated connector + lock versions
showTab("versions");
await openDataset(out.datasetId);
await wait(600);
showTab("versions");
out.versions = {
  inputs: qq("#input-versions .vr").length,
  results: qq("#result-versions .vr").length,
  optionValues: q("#ver-a").options.map((o) => o.value),
};
const opts = q("#ver-a").options;
if (opts.length > 1) {
  q("#ver-a").value = opts[0].value;
  q("#ver-b").value = opts[1].value;
  click("#btn-compare-versions");
  await wait(600);
  out.versionCompare = txt(q("#version-compare-output")).slice(0, 200);
}

// 7. the dataset side was dropped by opening another import: go back and check
// the job log and hints render for the multi-version dataset.
await openDataset(out.datasetId);
await wait(600);
out.jobRows = qq("#job-log .item, #job-log .jr").length;
out.hintsAfterMerge = qq("#hints .item").length;
out.exportLinks = {
  input: q("#export-input").href,
  result: q("#export-result").href,
};

return out;
