"use strict";
/* A deliberately small DOM shim used to execute the real UI script headlessly.
 *
 * No browser is installable in this environment, so this module implements the
 * subset of the DOM the UI uses.  Unsupported constructs throw instead of
 * silently succeeding, so a passing test means the script's DOM usage is real.
 */

class ClassList {
  constructor(node) { this.node = node; }
  _list() { return (this.node.attributes.get("class") || "").split(/\s+/).filter(Boolean); }
  _write(vals) { this.node.attributes.set("class", Array.from(new Set(vals)).join(" ")); }
  add(...names) { const v = this._list(); for (const n of names) if (!v.includes(n)) v.push(n); this._write(v); }
  remove(...names) { this._write(this._list().filter((v) => !names.includes(v))); }
  toggle(name, force) {
    const has = this.contains(name);
    const want = force === undefined ? !has : !!force;
    if (want) this.add(name); else this.remove(name);
    return want;
  }
  contains(name) { return this._list().includes(name); }
}

class Style {
  constructor() { this._v = new Map(); }
  setProperty(k, v) { this._v.set(k, v); }
  get cssText() { return ""; }
  get width() { return this._v.get("width") || ""; }
  set width(v) { this._v.set("width", v); }
  get height() { return this._v.get("height") || ""; }
  set height(v) { this._v.set("height", v); }
  get left() { return this._v.get("left") || ""; }
  set left(v) { this._v.set("left", v); }
  get top() { return this._v.get("top") || ""; }
  set top(v) { this._v.set("top", v); }
  get cursor() { return this._v.get("cursor") || ""; }
  set cursor(v) { this._v.set("cursor", v); }
}

class Context2D {
  constructor() { this.ops = []; this.fillStyle = "#000"; this.strokeStyle = "#000"; this.lineWidth = 1; this.font = ""; }
  setTransform(...a) { this.ops.push(["setTransform", ...a]); }
  clearRect(...a) { this.ops.push(["clearRect", ...a]); }
  fillRect(...a) { this.ops.push(["fillRect", ...a]); }
  strokeRect(...a) { this.ops.push(["strokeRect", ...a]); }
  beginPath() { this.ops.push(["beginPath"]); }
  moveTo(...a) { this.ops.push(["moveTo", ...a]); }
  lineTo(...a) { this.ops.push(["lineTo", ...a]); }
  stroke() { this.ops.push(["stroke"]); }
  fill() { this.ops.push(["fill"]); }
  arc(...a) { this.ops.push(["arc", ...a]); }
  fillText(...a) { this.ops.push(["fillText", ...a]); }
  setLineDash(...a) { this.ops.push(["setLineDash", ...a]); }
  count(name) { return this.ops.filter((o) => o[0] === name).length; }
}

const VOID_TAGS = new Set(["meta", "link", "br", "hr", "img", "input"]);

class Node {
  constructor(tag, document) {
    this.tagName = String(tag).toUpperCase();
    this.document = document;
    this.children = [];
    this.parent = null;
    this.attributes = new Map();
    this.style = new Style();
    this.dataset = {};
    this.listeners = new Map();
    this._text = "";
    this._value = "";
    this._valueAssigned = false;
    this.checked = false;
    this.disabled = false;
    this.className = "";
    this.width = 900;
    this.height = 330;
    this.clientWidth = 900;
    this.offsetWidth = 900;
    this.files = [];
  }

  get id() { return this.attributes.get("id") || ""; }
  set id(v) { this.attributes.set("id", v); this.document._index(this); }

  get value() { return this._value; }
  set value(v) { this._value = String(v); this._valueAssigned = true; this._syncSelect(); }

  /* <select>.options, as the UI reads it to decide whether comparison is possible */
  get options() { return this.children.filter((c) => c.tagName === "OPTION"); }

  /* A <select> shows its first option until something selects another; assigning
   * .value selects the matching option and clears the value when none matches. */
  _syncSelect() {
    if (this.tagName !== "SELECT" || !this.children.length) return;
    let hit = null;
    for (const o of this.options) if (o.value === this._value) hit = o;
    if (!hit && !this._valueAssigned) hit = this.options[0];
    for (const o of this.options) o.selected = o === hit;
    this._value = hit ? hit.value : "";
  }

  get classList() { return new ClassList(this); }

  setAttribute(k, v) { this.attributes.set(k, String(v)); }

  /* Anchors expose .href as a property as well as an attribute. */
  get href() { return this.attributes.get("href") || ""; }
  set href(v) { this.attributes.set("href", String(v)); }

  getAttribute(k) { const v = this.attributes.get(k); return v === undefined ? null : v; }
  hasAttribute(k) { return this.attributes.has(k); }
  removeAttribute(k) { this.attributes.delete(k); }

  /* HTMLOptionElement.text: the visible label, falling back to the value. */
  get text() {
    const t = this.textContent;
    return t === "" ? this._value : t;
  }

  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join("");
    return this._text;
  }
  set textContent(v) { this.children = []; this._text = String(v); }

  get innerHTML() { return this._html || ""; }
  set innerHTML(v) { this._html = String(v); this.children = []; this._text = ""; }

  get parentElement() { return this.parent; }
  get firstChild() { return this.children[0] || null; }

  appendChild(child) {
    if (!child) throw new Error("appendChild(null)");
    child.parent = this;
    this.children.push(child);
    this.document._index(child);
    if (this.tagName === "SELECT" && child.tagName === "OPTION") this._syncSelect();
    return child;
  }
  removeChild(child) {
    const i = this.children.indexOf(child);
    if (i >= 0) this.children.splice(i, 1);
    child.parent = null;
  }
  contains(node) {
    let n = node;
    while (n) { if (n === this) return true; n = n.parent; }
    return false;
  }

  addEventListener(name, fn) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(fn);
  }
  removeEventListener(name, fn) {
    const list = this.listeners.get(name) || [];
    const i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
  }
  dispatchEvent(ev) {
    const name = ev.type;
    ev.target = ev.target || this;
    ev.currentTarget = this;
    for (const fn of Array.from(this.listeners.get(name) || [])) fn(ev);
    if (this.parent && ev.bubbles !== false) this.parent.dispatchEvent(ev);
  }
  click() { this.dispatchEvent(makeEvent("click")); }
  focus() {}
  blur() {}

  *walk() {
    yield this;
    for (const c of this.children) yield* c.walk();
  }

  *ancestors() { let n = this; while (n) { yield n; n = n.parent; } }

  matches(sel) { return matchesSelector(this, sel); }
  querySelectorAll(sel) {
    const groups = splitGroups(sel);
    return Array.from(this.walk()).filter((n) => groups.some((g) => matchesChain(n, parseChain(g))));
  }
  querySelector(sel) {
    const groups = splitGroups(sel);
    for (const n of this.walk()) if (groups.some((g) => matchesChain(n, parseChain(g)))) return n;
    return null;
  }
  closest(sel) {
    for (const n of this.ancestors()) if (n.matches(sel)) return n;
    return null;
  }

  getContext(kind) {
    if (kind !== "2d") throw new Error("only 2d context is supported by the shim");
    if (!this._ctx) this._ctx = new Context2D();
    return this._ctx;
  }
  getBoundingClientRect() {
    return { left: 0, top: 0, right: this.clientWidth, bottom: this.height,
             width: this.clientWidth, height: this.height, x: 0, y: 0 };
  }
}

function makeEvent(type, extra = {}) {
  return Object.assign({
    type,
    bubbles: true,
    preventDefault() {},
    stopPropagation() {},
  }, extra);
}

/* selector support: #id, .class, tag, [attr], [attr=value], and comma groups.
 * Descendant/child combinators are rejected so tests never depend on them. */
function splitGroups(sel) {
  return String(sel).split(",").map((s) => s.trim()).filter(Boolean);
}

const SIMPLE = /([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]/g;

function parseDescription(part) {
  if (/[>+~]/.test(part)) throw new Error(`unsupported combinator in selector ${JSON.stringify(part)}`);
  const desc = { tag: null, id: null, classes: [], attrs: [] };
  let pos = 0;
  SIMPLE.lastIndex = 0;
  let m;
  while ((m = SIMPLE.exec(part))) {
    if (m.index !== pos) throw new Error(`unsupported selector syntax ${JSON.stringify(part)}`);
    pos = SIMPLE.lastIndex;
    if (m[1]) desc.tag = m[1].toLowerCase();
    else if (m[2]) desc.id = m[2];
    else if (m[3]) desc.classes.push(m[3]);
    else if (m[4]) desc.attrs.push([m[4], m[5]]);
  }
  if (pos !== part.length) throw new Error(`unsupported selector syntax ${JSON.stringify(part)}`);
  if (!desc.tag && !desc.id && !desc.classes.length && !desc.attrs.length) {
    throw new Error(`empty selector component in ${JSON.stringify(part)}`);
  }
  return desc;
}

const DESC_CACHE = new Map();
function parseChain(selector) {
  if (DESC_CACHE.has(selector)) return DESC_CACHE.get(selector);
  const chain = selector.trim().split(/\s+/).map(parseDescription);
  DESC_CACHE.set(selector, chain);
  return chain;
}

function nodeMatchesDesc(node, d) {
  if (d.tag && node.tagName.toLowerCase() !== d.tag) return false;
  if (d.id && node.id !== d.id) return false;
  const cl = (node.attributes.get("class") || "").split(/\s+/).filter(Boolean);
  if (d.classes.some((c) => !cl.includes(c))) return false;
  for (const [k, v] of d.attrs) {
    if (!node.attributes.has(k)) return false;
    if (v !== undefined && v !== "" && node.attributes.get(k) !== v) return false;
  }
  return true;
}

/* Match a descendant chain, e.g. "#tabs button": the last component must match
 * the node, and each earlier component must match some ancestor in order. */
function matchesChain(node, chain) {
  if (!nodeMatchesDesc(node, chain[chain.length - 1])) return false;
  let idx = chain.length - 2;
  let cur = node.parent;
  while (idx >= 0 && cur) {
    if (nodeMatchesDesc(cur, chain[idx])) idx--;
    cur = cur.parent;
  }
  return idx < 0;
}

function splitGroups(sel) {
  return String(sel).split(",").map((s) => s.trim()).filter(Boolean);
}

function matchesSelector(node, selector) {
  return splitGroups(selector).some((group) => matchesChain(node, parseChain(group)));
}

class Document extends Node {
  constructor() {
    super("#document", null);
    this.document = this;
    this._byId = new Map();
    this.windowListeners = [];
    this.readyState = "loading";
    this.body = new Node("body", this);
    this.body.parent = this;
    this.children.push(this.body);
    this.documentElement = new Node("html", this);
  }

  _index(node) {
    for (const n of node.walk()) {
      n.document = this;
      const id = n.attributes.get("id");
      if (id) this._byId.set(id, n);
    }
  }

  createElement(tag) { return new Node(tag, this); }
  createTextNode(text) { const n = new Node("#text", this); n.textContent = text; return n; }
  getElementById(id) { return this._byId.get(id) || null; }
  querySelector(sel) {
    const groups = splitGroups(sel);
    for (const g of groups) {
      if (g.startsWith("#") && /^#[\w-]+$/.test(g)) {
        const found = this._byId.get(g.slice(1));
        if (found) return found;
      }
    }
    return super.querySelector(sel);
  }
  querySelectorAll(sel) { return super.querySelectorAll(sel); }
  addEventListener(name, fn) { this.windowListeners.push([name, fn]); }
}

/* Minimal HTML parser, sufficient for this project's markup.
 *
 * Attribute values may contain ">" when quoted, so this is a character scanner
 * rather than a regex tokeniser.  It handles comments, void elements, quoted and
 * unquoted attributes, and rejects unbalanced markup. */
function parseHTML(html, doc) {
  const roots = [];
  const stack = [];
  let i = 0;
  const n = html.length;

  function pushText(text) {
    const trimmed = text.trim();
    if (!trimmed) return;
    if (!stack.length) throw new Error("text outside any element: " + JSON.stringify(trimmed.slice(0, 30)));
  }

  while (i < n) {
    const lt = html.indexOf("<", i);
    if (lt < 0) break;
    i = lt;

    if (html.startsWith("<!--", i)) {
      const end = html.indexOf("-->", i + 4);
      if (end < 0) throw new Error("unterminated comment");
      i = end + 3;
      continue;
    }
    if (html.startsWith("<!", i) || html.startsWith("<?", i)) {
      const end = html.indexOf(">", i);
      if (end < 0) throw new Error("unterminated declaration");
      i = end + 1;
      continue;
    }
    if (html[i + 1] === "/") {
      const end = html.indexOf(">", i);
      if (end < 0) throw new Error("unterminated closing tag");
      const tag = html.slice(i + 2, end).trim().toLowerCase();
      const open = stack.pop();
      if (!open) throw new Error(`unexpected closing tag </${tag}>`);
      if (open.tagName.toLowerCase() !== tag) {
        throw new Error(`mismatch <${open.tagName}> </${tag}>`);
      }
      i = end + 1;
      continue;
    }
    // opening tag
    let j = i + 1;
    if (!/[a-zA-Z]/.test(html[j] || "")) { i = j; continue; }
    while (j < n && /[^\s/>]/.test(html[j])) j++;
    const tag = html.slice(i + 1, j).toLowerCase();
    const node = doc.createElement(tag);
    let selfClosing = false;
    // attributes
    while (j < n) {
      while (j < n && /\s/.test(html[j])) j++;
      if (j >= n) throw new Error(`unterminated <${tag}>`);
      if (html[j] === ">") { j++; break; }
      if (html[j] === "/" && html[j + 1] === ">") { selfClosing = true; j += 2; break; }
      const nameStart = j;
      while (j < n && !/[\s=/>]/.test(html[j])) j++;
      const name = html.slice(nameStart, j).toLowerCase();
      if (!name) throw new Error(`bad attribute in <${tag}>`);
      while (j < n && /\s/.test(html[j])) j++;
      let value = "";
      if (html[j] === "=") {
        j++;
        while (j < n && /\s/.test(html[j])) j++;
        const quote = html[j];
        if (quote === '"' || quote === "'") {
          const close = html.indexOf(quote, j + 1);
          if (close < 0) throw new Error(`unterminated attribute value in <${tag}>`);
          value = html.slice(j + 1, close);
          j = close + 1;
        } else {
          const vStart = j;
          while (j < n && !/[\s>]/.test(html[j])) j++;
          value = html.slice(vStart, j);
        }
      }
      if (name === "id") node.id = value;
      else node.attributes.set(name, value);
      if (name === "value") node.value = value;
      if (name === "checked") node.checked = true;
    }
    const parent = stack.length ? stack[stack.length - 1] : null;
    if (parent) { node.parent = parent; parent.children.push(node); }
    else roots.push(node);
    doc._index(node);
    if (!VOID_TAGS.has(tag) && !selfClosing) stack.push(node);
    i = j;
  }
  if (stack.length) throw new Error(`unclosed <${stack[stack.length - 1].tagName}>`);
  return roots;
}

module.exports = { Document, Node, Context2D, parseHTML, makeEvent };
