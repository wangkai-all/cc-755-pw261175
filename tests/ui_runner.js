"use strict";
/* Headless UI runner.
 *
 * Loads the real index.html and app.js, executes the script in a vm context
 * with a fetch proxy pointing at a live server, then evaluates a probe script
 * and prints the probe's return value as JSON.
 *
 * Usage: node tests/ui_runner.js <baseUrl> <probeFile>
 *
 * No browser is available here; tests/domshim.js implements the DOM subset the
 * UI uses and throws on anything it does not support, so a green run means the
 * script's real DOM usage works.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { Document, parseHTML, makeEvent } = require("./domshim");

const ROOT = path.resolve(__dirname, "..");

async function main() {
  const baseUrl = String(process.argv[2] || "").replace(/\/$/, "");
  const probeFile = process.argv[3];
  if (!baseUrl || !probeFile) {
    console.error("usage: node tests/ui_runner.js <baseUrl> <probeFile>");
    process.exit(2);
  }

  const html = fs.readFileSync(path.join(ROOT, "haplorecon/static/index.html"), "utf8");
  const appJs = fs.readFileSync(path.join(ROOT, "haplorecon/static/app.js"), "utf8");
  const probe = fs.readFileSync(probeFile, "utf8");

  const doc = new Document();
  const roots = parseHTML(html, doc);
  for (const r of roots) {
    r.parent = doc.body;
    doc.body.children.push(r);
    doc._index(r);
  }
  doc.readyState = "interactive";

  const fetchCalls = [];
  async function fetchShim(input, init) {
    const url = typeof input === "string" ? input : input.url;
    const absolute = url.startsWith("http") ? url : baseUrl + url;
    const opts = Object.assign({}, init);
    if (typeof options !== "undefined") { /* no-op, keeps linters quiet */ }
    const res = await fetch(absolute, opts);
    const body = await res.text();
    fetchCalls.push({ url, status: res.status });
    return {
      ok: res.ok,
      status: res.status,
      text: async () => body,
      json: async () => JSON.parse(body),
    };
  }

  const timers = [];
  const sandbox = {
    document: doc,
    window: null,
    console,
    fetch: fetchShim,
    setTimeout: (fn, ms) => {
      const t = setTimeout(fn, ms);
      timers.push(t);
      return t;
    },
    clearTimeout,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    Promise,
    Map,
    Set,
    JSON,
    Math,
    Number,
    String,
    Array,
    Object,
    Error,
    Date,
  };
  sandbox.window = {
    document: doc,
    addEventListener: (name, fn) => doc.addEventListener(name, fn),
    devicePixelRatio: 1,
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    location: { href: baseUrl + "/" },
  };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox.window;
  const context = vm.createContext(sandbox);

  // The UI script is a classic script: it runs at top level against globals.
  vm.runInContext(appJs, context, { filename: "app.js" });

  // Fire DOMContentLoaded and let the async boot settle.
  for (const fn of doc.windowListeners.filter(([n]) => n === "DOMContentLoaded")) {
    await fn[1](makeEvent("DOMContentLoaded"));
  }
  await new Promise((r) => setTimeout(r, 300));

  const runner = `(async () => { ${probe} })()`;
  let result;
  try {
    result = await vm.runInContext(runner, context, { filename: probeFile });
  } finally {
    for (const t of timers) clearTimeout(t);
  }

  console.log(JSON.stringify({
    ok: true,
    result,
    fetchCalls,
    sandboxKeys: Object.keys(sandbox).sort(),
  }, null, 2));
}

main().catch((err) => {
  console.log(JSON.stringify({ ok: false, error: String(err && err.stack || err) }));
  process.exit(1);
});
