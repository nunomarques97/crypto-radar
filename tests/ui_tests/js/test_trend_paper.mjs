// Regression tests for the Trend paper panel (ui/web/trend_paper.js): populated,
// empty, refused and unreadable rendering, the skipped-days line and the
// waiting-for-data catch-up answer, text-only insertion, out-of-order and
// failed polls, the "Catch up now" control (pending state, overlapping
// completions, retry, TEST MODE) and the poller's start/stop/start. Same
// hand-rolled DOM stub as test_pilot_shadow.mjs.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const TP = require("../../../ui/web/trend_paper.js");
const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

// -- a minimal DOM ----------------------------------------------------------------
let focusCalls = 0;

class FakeText {
  constructor(data) { this.nodeType = 3; this.data = String(data); this.parentNode = null; }
  get textContent() { return this.data; }
}

class FakeElement {
  constructor(tag) {
    this.nodeType = 1; this.tagName = String(tag).toLowerCase(); this.childNodes = [];
    this.attributes = {}; this.parentNode = null; this.listeners = {}; this.writes = 0;
  }
  appendChild(child) { child.parentNode = this; this.childNodes.push(child); return child; }
  removeChild(child) { this.childNodes = this.childNodes.filter((c) => c !== child); child.parentNode = null; return child; }
  get firstChild() { return this.childNodes[0] || null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  click() { (this.listeners.click || []).forEach((fn) => fn({ type: "click" })); }
  focus() { focusCalls += 1; }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(value) { this.writes += 1; this.childNodes = [new FakeText(value)]; }
  set innerHTML(_value) { throw new Error("innerHTML must never be used"); }
  set outerHTML(_value) { throw new Error("outerHTML must never be used"); }
  insertAdjacentHTML() { throw new Error("insertAdjacentHTML must never be used"); }
}

const fakeDoc = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (data) => new FakeText(data),
};

function all(node, out = []) {
  if (node.nodeType === 1) {
    out.push(node);
    node.childNodes.forEach((c) => all(c, out));
  }
  return out;
}

function byTag(node, tag) {
  return all(node).filter((n) => n.tagName === tag);
}

function byClass(node, cls) {
  return all(node).filter((n) => (n.getAttribute("class") || "").split(" ").includes(cls));
}

function slots() {
  return { summary: new FakeElement("div"), body: new FakeElement("div"), refresh: new FakeElement("div") };
}

function view(s) {
  return TP.createView(s, { doc: fakeDoc });
}

const HOSTILE = "<img src=x onerror=alert(1)>";
const flush = () => new Promise((r) => setImmediate(r));

function at(minute) {
  return `2026-10-17T08:${String(minute).padStart(2, "0")}:00+00:00`;
}

// -- readings as ui/trend_reader.py builds them -----------------------------------------
const STRATEGIES = [
  ["ENS", "ENS", "BH_5050", ["BTC", "ETH"]],
  ["ENS_VT", "ENS_VT", "BH_5050", ["BTC", "ETH"]],
  ["BTC_TREND5", "btc_trend5", "BH_BTC", ["BTC"]],
  ["BTC_TREND5_VT", "btc_trend5_vt", "BH_BTC", ["BTC"]],
];

function bookRow(quote, rule, label, comparator, assets, fee, i) {
  const loss = i === 2; // btc_trend5 trails its buy-and-hold
  return {
    book: `${quote}|${rule}|${fee === "0.1" ? "0.001" : "0.004"}`, rule, label, fee_pct: fee, currency: quote,
    days: 14, last_date: "2026-10-17",
    equity: fee === "0.1" ? "6670.70" : "6640.08", return_pct: fee === "0.1" ? "-4.70" : "-5.14",
    max_drawdown_pct: "-5.27", trades: 5, fees: fee === "0.1" ? "10.32" : "41.25",
    comparator: { rule: comparator, book: `${quote}|${comparator}|x`, equity: "6650.00", return_pct: "-5.00" },
    vs_buy_hold_pp: loss ? "-0.03" : "0.30",
    assets: assets.map((asset) => ({
      asset, exposure_pct: asset === "BTC" ? "100.00" : "40.00", target_pct: asset === "BTC" ? "80.00" : "40.00",
      signal_close_date: "2026-10-16", signal: [{ name: rule, value: "1.0000" }],
    })),
  };
}

function reading(overrides) {
  return {
    state: "ok", honesty_label: TP.TEXT.honesty, pre_tax_note: "Results are pre-tax.", paper_start: "2026-10-04",
    capital: "7000.00", reason: null, error: null, days_booked: 14, first_day: "2026-10-04", last_day: "2026-10-17",
    last_record_ts: "2026-10-17T08:00:00+00:00",
    quotes: ["EUR", "USDT"].map((quote) => ({
      quote, currency: quote,
      rows: STRATEGIES.flatMap(([rule, label, comparator, assets], i) =>
        ["0.1", "0.4"].map((fee) => bookRow(quote, rule, label, comparator, assets, fee, i))),
    })),
    generated_at: at(0),
    ...overrides,
  };
}

function emptyReading(atTime = at(0)) {
  return {
    ...reading(), state: "empty", quotes: [], days_booked: 0, first_day: null, last_day: null, last_record_ts: null,
    reason: "No paper day yet: the first fill is at the 2026-10-04 00:00 UTC open (decided on the 2026-10-03 close).",
    generated_at: atTime,
  };
}

function refusedReading(atTime = at(0)) {
  return {
    ...reading(), state: "refused", quotes: [], days_booked: null, first_day: null, last_day: null,
    last_record_ts: null, reason: "The trend paper ledger was refused; it is never rewritten.",
    error: { code: "LEDGER_EDITED", detail: "record 3: hash mismatch" }, generated_at: atTime,
  };
}

function rowsOf(block) {
  return byTag(block, "tbody").flatMap((tb) => tb.childNodes.filter((tr) => (tr.getAttribute("class") || "") !== "tp-group"));
}

// -- the contract with the backend and the page ------------------------------------------
test("every catch-up outcome of the reader has an English label and a fallback sentence", () => {
  const source = readFileSync(path.join(ROOT, "ui", "trend_reader.py"), "utf8");
  const block = source.slice(source.indexOf("class CatchUpOutcome"), source.indexOf("_OK_OUTCOMES"));
  const outcomes = [...block.matchAll(/^\s+([A-Z_]+) = "([A-Z_]+)"$/gm)].map((m) => m[2]);
  assert.ok(outcomes.length >= 7, "outcomes parsed from ui/trend_reader.py");
  assert.deepEqual(Object.keys(TP.CATCH_UP_TEXT).sort(), outcomes.slice().sort());
  outcomes.forEach((o) => {
    assert.ok(TP.CATCH_UP_TEXT[o].label.length > 3, o);
    assert.ok(TP.CATCH_UP_TEXT[o].fallback.endsWith("."), o);
  });
  const ok = /_OK_OUTCOMES = frozenset\(\{([^}]*)\}\)/.exec(source)[1];
  outcomes.forEach((o) => assert.equal(TP.CATCH_UP_TEXT[o].ok, ok.includes(`CatchUpOutcome.${o}`), o));
});

test("the honesty label and the pre-tax note are fixed in the page, inside the panel, with one control", () => {
  const source = readFileSync(path.join(ROOT, "ui", "trend_reader.py"), "utf8");
  assert.ok(source.includes(`HONESTY_LABEL = "${TP.TEXT.honesty}"`));
  const html = readFileSync(path.join(ROOT, "ui", "web", "index.html"), "utf8");
  const start = html.indexOf('id="trend-paper"');
  const panel = html.slice(start, html.indexOf("</section>", start));
  assert.ok(start > html.indexOf('id="pilot-shadow"'), "after the pilot shadow");
  assert.ok(start < html.indexOf('id="pg-glossary-h"'), "before the glossary");
  assert.ok(panel.includes(TP.TEXT.honesty));
  assert.ok(panel.includes("Results are pre-tax"));
  assert.equal((panel.match(/<button/g) || []).length, 1, "the only control");
  assert.ok(/<button type="button" class="btn" id="tp-catch-up"[^>]*>Catch up now<\/button>/.test(panel));
  assert.ok(/id="tp-live" aria-live="polite"/.test(panel));
  assert.ok(html.indexOf('src="trend_paper.js"') > html.indexOf('src="paper_game.js"'));
  assert.ok(html.indexOf('src="trend_paper.js"') < html.indexOf('src="app.js"'));
});

test("selectView: loading, ok, empty, refused, unavailable and unreadable", () => {
  assert.equal(TP.selectView(undefined).kind, "loading");
  assert.equal(TP.selectView(null).kind, "unavailable");
  assert.equal(TP.selectView("x").kind, "unavailable");
  assert.equal(TP.selectView({ state: "weird" }).kind, "unavailable");
  assert.equal(TP.selectView({ state: "ok", quotes: null }).kind, "unavailable");
  assert.equal(TP.selectView(reading()).kind, "ok");
  assert.equal(TP.selectView(emptyReading()).kind, "empty");
  assert.equal(TP.selectView(refusedReading()).kind, "refused");
  const unavailable = TP.selectView({ state: "unavailable", reason: "The trend paper ledger could not be read." });
  assert.deepEqual(unavailable, { kind: "unavailable", message: "The trend paper ledger could not be read." });
  assert.equal(TP.selectView({ state: "empty", reason: null }).message, TP.TEXT.empty);
});

test("formatting keeps the backend's digits and signs; nothing else is a number", () => {
  assert.equal(TP.amount("7000.00"), "7,000.00");
  assert.equal(TP.amount("-0.35"), "-0.35");
  assert.equal(TP.amount(7000), null, "a JSON number is not a backend amount");
  assert.equal(TP.amount(null), null);
  assert.equal(TP.signedPercent("2.27"), "+2.27%");
  assert.equal(TP.signedPercent("0.00"), "0.00%");
  assert.equal(TP.points("-0.81"), "-0.81 pp");
  assert.equal(TP.points("0.30"), "+0.30 pp");
  assert.equal(TP.points(null), null);
  assert.equal(TP.points("abc"), null);
});

// -- rendering ---------------------------------------------------------------------------
test("a populated reading lists every strategy at both fees, EUR then USDT", () => {
  const s = slots();
  view(s).render(reading());
  const blocks = byClass(s.body, "tp-book");
  assert.deepEqual(blocks.map((b) => byTag(b, "h4")[0].textContent), ["EUR books", "USDT books"]);
  blocks.forEach((block) => {
    const groups = byClass(block, "tp-group");
    assert.deepEqual(groups.map((g) => byClass(g, "tp-name")[0].textContent), ["ENS", "ENS_VT", "btc_trend5", "btc_trend5_vt"]);
    assert.equal(rowsOf(block).length, 8);
    assert.deepEqual(rowsOf(block).map((tr) => tr.childNodes[0].textContent), ["0.1%", "0.4%", "0.1%", "0.4%", "0.1%", "0.4%", "0.1%", "0.4%"]);
    const head = byTag(byTag(block, "thead")[0], "th").map((th) => th.textContent);
    const cur = byTag(block, "h4")[0].textContent.split(" ")[0];
    assert.deepEqual(head, ["Fee per leg", `Equity (${cur})`, "Return since 2026-10-04", "vs buy-and-hold", "Max drawdown",
      "Trades", `Fees (${cur})`, "Exposure (target)"]);
    assert.ok(byTag(block, "caption")[0].textContent.includes(`7,000.00 ${cur} on 2026-10-04`));
  });
  const first = rowsOf(blocks[0])[0].childNodes.map((td) => td.textContent);
  assert.deepEqual(first, ["0.1%", "6,670.70as of 2026-10-17", "-4.70%", "+0.30 ppbuy-and-hold -5.00%", "-5.27%", "5", "10.32",
    "BTC 100.00%target 80.00%ETH 40.00%target 40.00%"]);
  assert.ok(byClass(blocks[0], "tp-desc")[0].textContent.includes("compared with buy-and-hold of BTC and ETH, half each"));
  assert.ok(byClass(blocks[0], "tp-desc")[2].textContent.includes("Holds BTC; compared with buy-and-hold of BTC"));
  assert.ok(s.summary.textContent.includes("14 paper days booked, from 2026-10-04 to 2026-10-17."));
  assert.ok(s.body.textContent.includes("Exposure is the share of the book held after the last daily fill"));
  // Every number is in mono; money, percentages and dates are never plain text.
  const monos = byClass(s.body, "mono").map((n) => n.textContent);
  ["6,670.70", "-4.70%", "+0.30 pp", "-5.27%", "10.32", "100.00%", "2026-10-17", "7,000.00"].forEach((v) => assert.ok(monos.includes(v), v));
});

test("gains and losses take --ok/--err only from the backend sign", () => {
  const s = slots();
  view(s).render(reading());
  const block = byClass(s.body, "tp-book")[0];
  const rows = rowsOf(block);
  const tone = (tr, col) => byClass(tr.childNodes[col], "mono")[0].getAttribute("class");
  assert.equal(tone(rows[0], 2), "mono tone-down", "a negative return");
  assert.equal(tone(rows[0], 3), "mono tone-up", "ahead of buy-and-hold");
  assert.equal(tone(rows[4], 3), "mono tone-down", "behind buy-and-hold");
  assert.equal(tone(rows[0], 4), "mono", "drawdown is not coloured");
  const flat = slots();
  const r = reading();
  r.quotes[0].rows[0].return_pct = "0.00";
  r.quotes[0].rows[0].vs_buy_hold_pp = "0.00";
  view(flat).render(r);
  const row = rowsOf(byClass(flat.body, "tp-book")[0])[0];
  assert.equal(byClass(row.childNodes[2], "mono")[0].getAttribute("class"), "mono tone-flat");
  assert.equal(row.childNodes[3].textContent.startsWith("0.00 pp"), true);
});

test("values not recorded read '—', never 0", () => {
  const r = reading();
  Object.assign(r.quotes[0].rows[0], {
    days: 0, last_date: null, equity: null, return_pct: null, max_drawdown_pct: null, trades: null, fees: null,
    vs_buy_hold_pp: null, comparator: { rule: "BH_5050", book: "x", equity: null, return_pct: null }, assets: [],
  });
  r.quotes[0].rows[1].trades = 0; // a recorded zero stays a zero
  const s = slots();
  view(s).render(r);
  const rows = rowsOf(byClass(s.body, "tp-book")[0]);
  const cells = rows[0].childNodes.map((td) => td.textContent);
  assert.deepEqual(cells, ["0.1%", "—last day —", "—", "—buy-and-hold —", "—", "—", "—", "—"]);
  assert.ok(cells.slice(1).every((c) => !/\d/.test(c)), "no figure in a book without days");
  assert.equal(rows[1].childNodes[5].textContent, "0");
  // A number where a decimal string belongs is not shown as a figure either.
  const odd = reading();
  odd.quotes[0].rows[0].equity = 6670.7;
  odd.capital = 7000;
  const t = slots();
  view(t).render(odd);
  assert.equal(rowsOf(byClass(t.body, "tp-book")[0])[0].childNodes[1].textContent, "—as of 2026-10-17");
  assert.ok(byTag(t.body, "caption")[0].textContent.includes("started with — EUR"));
});

test("before the first paper day one calm line says why and shows no book", () => {
  const s = slots();
  view(s).render(emptyReading());
  assert.equal(byTag(s.body, "table").length, 0);
  assert.equal(s.summary.childNodes.length, 0);
  assert.equal(byClass(s.body, "pg-empty-title")[0].textContent, emptyReading().reason);
  assert.ok(byClass(s.body, "pg-empty-sub")[0].textContent.includes("Each book starts with 7,000.00 in its currency"));
  assert.equal(byClass(byClass(s.body, "pg-empty-sub")[0], "mono")[0].textContent, "7,000.00");
});

test("a refused ledger shows the refusal, its code and detail, and no figure", () => {
  const s = slots();
  view(s).render(refusedReading());
  assert.equal(byTag(s.body, "table").length, 0);
  assert.equal(byClass(s.body, "pill")[0].textContent, "Ledger refused");
  assert.equal(byClass(s.body, "pill")[0].getAttribute("class"), "pill err");
  assert.ok(s.body.textContent.includes("The trend paper ledger was refused; it is never rewritten."));
  assert.ok(s.body.textContent.includes("LEDGER_EDITED: record 3: hash mismatch"));
  assert.ok(s.body.textContent.includes("No figure is shown from a refused ledger."));
  assert.ok(!/\d,\d{3}\.\d{2}/.test(s.body.textContent));
});

test("an unavailable or unreadable reading shows one calm line", () => {
  const s = slots();
  const v = view(s);
  v.render({ state: "unavailable", reason: "The trend paper ledger could not be read.", quotes: [], generated_at: at(0) });
  assert.equal(byClass(s.body, "pg-empty-title")[0].textContent, "The trend paper ledger could not be read.");
  v.render("garbage");
  assert.equal(byClass(s.body, "pg-empty-title")[0].textContent, TP.TEXT.unreadable);
  const loading = slots();
  view(loading).render(undefined);
  assert.equal(byClass(loading.body, "pg-empty-title")[0].textContent, TP.TEXT.loading);
});

test("hostile backend strings are inserted as text only", () => {
  const r = reading({ last_record_ts: HOSTILE, first_day: HOSTILE });
  const row = r.quotes[0].rows[0];
  row.label = HOSTILE;
  row.assets[0].asset = HOSTILE;
  row.comparator.rule = HOSTILE;
  r.quotes[1].currency = HOSTILE;
  const s = slots();
  view(s).render(r);
  assert.ok(s.body.textContent.includes(HOSTILE));
  assert.equal(all(s.body).filter((n) => n.tagName === "img").length, 0);
  assert.equal(all(s.summary).filter((n) => n.tagName === "img").length, 0);
  const refused = refusedReading();
  refused.reason = HOSTILE;
  refused.error = { code: HOSTILE, detail: HOSTILE };
  const t = slots();
  view(t).render(refused);
  assert.equal(t.body.textContent.split(HOSTILE).length - 1, 3);
  assert.equal(all(t.body).filter((n) => n.tagName === "img").length, 0);
  const e = slots();
  view(e).render({ ...emptyReading(), reason: HOSTILE });
  assert.equal(byClass(e.body, "pg-empty-title")[0].textContent, HOSTILE);
  // Every attribute set comes from this file, never from the backend.
  [s.body, t.body, e.body].forEach((root) => all(root).forEach((n) => {
    Object.values(n.attributes).forEach((v) => assert.ok(!v.includes("<"), v));
  }));
});

// -- polls ---------------------------------------------------------------------------------
test("a reading older than one already applied is dropped; a newer identical one rebuilds nothing", () => {
  const s = slots();
  const v = view(s);
  assert.equal(v.render(reading({ generated_at: at(5) })), true);
  const shown = s.body.firstChild;
  const older = reading({ generated_at: at(4) });
  older.quotes[0].rows[0].equity = "1.00";
  assert.equal(v.render(older), false);
  assert.equal(s.body.firstChild, shown);
  assert.ok(!s.body.textContent.includes("1.00as of"));
  assert.equal(v.render(reading({ generated_at: at(6) })), true);
  assert.equal(s.body.firstChild, shown, "same figures, no rebuild");
  assert.equal(v.render(emptyReading(at(7))), true);
  assert.equal(byTag(s.body, "table").length, 0);
});

test("a failed poll keeps the last reading with a visible note; the next success clears it", () => {
  const s = slots();
  const v = view(s);
  v.render(reading({ generated_at: "2026-10-17T08:00:05+00:00" }));
  const before = s.body.textContent;
  v.renderError();
  assert.equal(s.body.textContent, before);
  assert.match(s.refresh.textContent, /^Could not refresh the trend paper; showing the reading from \d\d:\d\d:05\.$/);
  v.render(reading({ generated_at: at(1) }));
  assert.equal(s.refresh.textContent, "");
});

test("a failed first poll shows the unreadable state; a later success replaces it", () => {
  const s = slots();
  const v = view(s);
  v.render(undefined);
  v.renderError();
  assert.equal(byClass(s.body, "pg-empty-title")[0].textContent, TP.TEXT.unreadable);
  assert.equal(s.refresh.textContent, "");
  v.render(reading());
  assert.equal(byClass(s.body, "tp-book").length, 2);
});

test("a failed poll after an unavailable reading shows the unreadable state, not a stale note", () => {
  const s = slots();
  const v = view(s);
  v.render({ state: "unavailable", reason: "The trend paper ledger could not be read.", quotes: [], generated_at: at(0) });
  v.renderError();
  assert.equal(s.refresh.textContent, "");
  assert.equal(byClass(s.body, "pg-empty-title")[0].textContent, TP.TEXT.unreadable);
});

function fakeTimers() {
  const pending = new Map();
  let id = 0;
  return {
    setTimeout: (fn, ms) => { id += 1; pending.set(id, { fn, ms }); return id; },
    clearTimeout: (t) => { pending.delete(t); },
    pending,
    fire() {
      const entries = [...pending.entries()];
      pending.clear();
      entries.forEach(([, t]) => t.fn());
    },
  };
}

function deferredApi() {
  const calls = [];
  return {
    calls,
    fetch: () => new Promise((resolve, reject) => calls.push({ resolve, reject })),
  };
}

test("the poller survives start/stop/start: one timer, responses of a stopped generation dropped", async () => {
  const timers = fakeTimers();
  const api = deferredApi();
  const applied = [];
  const poller = TP.createPoller({
    intervalMs: 15000, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    fetchState: api.fetch, onState: (s) => applied.push(s.generated_at), onError: () => applied.push("error"),
  });
  poller.start();
  poller.stop();
  poller.start();
  assert.equal(poller.start(), null, "start while running does nothing");
  assert.equal(api.calls.length, 2);
  api.calls[1].resolve({ generated_at: at(2) });
  api.calls[0].resolve({ generated_at: at(1) });
  await flush();
  assert.deepEqual(applied, [at(2)]);
  assert.equal(timers.pending.size, 1);
  assert.equal([...timers.pending.values()][0].ms, 15000);
  poller.stop();
  assert.equal(timers.pending.size, 0);
  poller.start();
  poller.stop();
  poller.start();
  api.calls[3].reject(new Error("bridge"));
  api.calls[2].resolve({ generated_at: at(9) });
  await flush();
  assert.deepEqual(applied, [at(2), "error"]);
  assert.equal(timers.pending.size, 1, "still exactly one next poll");
  poller.stop();
  assert.equal(poller.isRunning(), false);
});

// -- the catch-up control ----------------------------------------------------------------
function control(overrides = {}) {
  const button = new FakeElement("button");
  const result = new FakeElement("p");
  const live = new FakeElement("div");
  const api = deferredApi();
  const done = [];
  const c = TP.createCatchUp({
    button, result, live, call: api.fetch, allowed: () => true, onDone: () => done.push("refresh"), ...overrides,
  });
  return { c, button, result, live, api, done };
}

test("Catch up now is inert while pending, re-enabled after, and announces the result once without moving focus", async () => {
  focusCalls = 0;
  const { c, button, result, live, api, done } = control();
  assert.equal(button.getAttribute("aria-disabled"), "false");
  button.click();
  assert.equal(c.isPending(), true);
  assert.equal(button.getAttribute("aria-disabled"), "true");
  assert.equal(button.getAttribute("aria-busy"), "true");
  assert.equal(result.textContent, TP.TEXT.catchingUp);
  assert.equal(live.writes, 0, "nothing announced while pending");
  button.click();
  assert.equal(c.run(), null);
  assert.equal(api.calls.length, 1, "one request while pending");
  api.calls[0].resolve({ ok: true, status: "BOOKED", days_booked: 2, detail: "Booked 2 paper day(s): 2026-10-04 to 2026-10-05." });
  await flush();
  assert.equal(button.getAttribute("aria-disabled"), "false");
  assert.equal(button.getAttribute("aria-busy"), "false");
  assert.equal(live.textContent, "Catch-up: Caught up. Booked 2 paper day(s): 2026-10-04 to 2026-10-05.");
  assert.equal(live.writes, 1);
  assert.equal(result.textContent, live.textContent);
  assert.equal(result.getAttribute("class"), "tp-result");
  assert.deepEqual(done, ["refresh"]);
  assert.equal(focusCalls, 0);
});

test("each outcome reads as its label and the backend's detail; failures are marked", () => {
  Object.keys(TP.CATCH_UP_TEXT).forEach((status) => {
    const m = TP.catchUpMessage({ ok: TP.CATCH_UP_TEXT[status].ok, status, days_booked: 0, detail: "Detail here." });
    assert.equal(m.text, `Catch-up: ${TP.CATCH_UP_TEXT[status].label}. Detail here.`);
    const bare = TP.catchUpMessage({ status, detail: null });
    assert.equal(bare.text, `Catch-up: ${TP.CATCH_UP_TEXT[status].label}. ${TP.CATCH_UP_TEXT[status].fallback}`);
  });
  ["BUSY", "MARKET_FAILED", "WAITING_FOR_DATA", "LEDGER_REFUSED", "IN_PROGRESS"]
    .forEach((s) => assert.equal(TP.catchUpMessage({ status: s }).ok, false));
  ["BOOKED", "UP_TO_DATE", "BEFORE_START"].forEach((s) => assert.equal(TP.catchUpMessage({ status: s }).ok, true));
  assert.deepEqual(TP.catchUpMessage({ status: "SOMETHING" }), { text: TP.TEXT.catchUpUnknown, ok: false });
  assert.deepEqual(TP.catchUpMessage(null), { text: TP.TEXT.catchUpUnknown, ok: false });
  assert.equal(TP.catchUpMessage({ status: "BUSY", detail: HOSTILE }).text, `Catch-up: Busy. ${HOSTILE}`);
});

test("a failed catch-up re-enables the control, says so, refreshes, and a retry works", async () => {
  const { c, button, result, live, api, done } = control();
  c.run();
  api.calls[0].reject(new Error("bridge down"));
  await flush();
  assert.equal(c.isPending(), false);
  assert.equal(button.getAttribute("aria-disabled"), "false");
  assert.equal(live.textContent, TP.TEXT.catchUpFailed);
  assert.equal(result.getAttribute("class"), "tp-result tp-warn");
  assert.deepEqual(done, ["refresh"]);
  c.run();
  assert.equal(api.calls.length, 2, "retry after a failure sends a new request");
  api.calls[1].resolve({ ok: true, status: "UP_TO_DATE", days_booked: 0, detail: "Every due paper day is already booked." });
  await flush();
  assert.equal(live.textContent, "Catch-up: Up to date. Every due paper day is already booked.");
  assert.equal(live.writes, 2, "one announcement per completed request");
  assert.equal(result.getAttribute("class"), "tp-result");
  assert.deepEqual(done, ["refresh", "refresh"]);
});

test("a call that throws at once is a failure, not a stuck pending state", async () => {
  const { c, button, live } = control({ call: () => { throw new Error("no bridge"); } });
  await c.run();
  assert.equal(c.isPending(), false);
  assert.equal(button.getAttribute("aria-disabled"), "false");
  assert.equal(live.textContent, TP.TEXT.catchUpFailed);
});

test("a refused or busy answer is announced as such and re-enables the control", async () => {
  const { c, live, api, result } = control();
  c.run();
  api.calls[0].resolve({ ok: false, status: "LEDGER_REFUSED", days_booked: null, detail: "The ledger was refused (LEDGER_EDITED: x); it is never rewritten." });
  await flush();
  assert.equal(live.textContent, "Catch-up: Ledger refused. The ledger was refused (LEDGER_EDITED: x); it is never rewritten.");
  assert.equal(result.getAttribute("class"), "tp-result tp-warn");
  c.run();
  api.calls[1].resolve({ ok: false, status: "BUSY", days_booked: 0, detail: null });
  await flush();
  assert.equal(live.textContent, `Catch-up: Busy. ${TP.CATCH_UP_TEXT.BUSY.fallback}`);
  assert.equal(c.isPending(), false);
});

test("in TEST MODE the catch-up is never called", () => {
  let testMode = true;
  let calls = 0;
  const { c, button, live, result } = control({ allowed: () => !testMode, call: () => { calls += 1; return Promise.resolve({}); } });
  button.click();
  assert.equal(c.run(), null);
  assert.equal(calls, 0);
  assert.equal(c.isPending(), false);
  assert.equal(button.getAttribute("aria-disabled"), "false");
  assert.equal(live.writes, 0, "nothing announced");
  assert.equal(result.textContent, TP.TEXT.catchUpBlocked);
  testMode = false;
  c.run();
  assert.equal(calls, 1);
});

// The options object app.js really passes to createCatchUp, evaluated against stubs.
function appCatchUpOptions(env) {
  const source = readFileSync(path.join(ROOT, "ui", "web", "app.js"), "utf8");
  const marker = "window.RadarTrendPaper.createCatchUp(";
  assert.equal(source.split(marker).length, 2, "app.js creates the control exactly once");
  const open = source.indexOf(marker) + marker.length;
  let depth = 0;
  let end = open;
  for (; end < source.length; end += 1) {
    if (source[end] === "(") depth += 1;
    if (source[end] === ")") { if (depth === 0) break; depth -= 1; }
  }
  // eslint-disable-next-line no-new-func
  return new Function("testMode", "window", "document", "trendPoller", `return (${source.slice(open, end)});`)(
    env.testMode, env.window, env.document, env.trendPoller,
  );
}

test("app.js wires Catch up now so that TEST MODE never reaches the bridge", () => {
  // The unit test above passes its own allowed(); this one
  // checks the wiring the page uses (TEST MODE is browser-local; the bridge has no guard).
  const app = readFileSync(path.join(ROOT, "ui", "web", "app.js"), "utf8");
  assert.equal(app.split("trend_paper_catch_up(").length, 2, "the only call path to the bridge");
  let calls = 0;
  const nodes = {};
  const env = (testMode) => ({
    testMode,
    window: { pywebview: { api: { trend_paper_catch_up: () => { calls += 1; return Promise.resolve({}); } } } },
    document: { getElementById: (id) => (nodes[id] = nodes[id] || new FakeElement(id === "tp-catch-up" ? "button" : "div")) },
    trendPoller: { isRunning: () => false, stop() {}, start() {} },
  });
  const blocked = appCatchUpOptions(env(true));
  assert.equal(typeof blocked.allowed, "function", "app.js passes an allowed() guard");
  assert.equal(blocked.allowed(), false);
  const c = TP.createCatchUp(blocked);
  assert.equal(c.run(), null);
  nodes["tp-catch-up"].click();
  assert.equal(calls, 0, "never called in TEST MODE");
  assert.equal(nodes["tp-result"].textContent, TP.TEXT.catchUpBlocked);
  const open = appCatchUpOptions(env(false));
  assert.equal(open.allowed(), true);
  TP.createCatchUp(open).run();
  assert.equal(calls, 1, "called once TEST MODE is off");
});

// -- the panel as app.js wires it ----------------------------------------------------------
function panel() {
  const timers = fakeTimers();
  const s = slots();
  const v = view(s);
  const polls = deferredApi();
  const poller = TP.createPoller({
    intervalMs: 15000, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    fetchState: polls.fetch, onState: (state) => v.render(state), onError: () => v.renderError(),
  });
  const ctl = control({
    onDone: () => {
      if (poller.isRunning()) { poller.stop(); poller.start(); }
    },
  });
  return { timers, s, v, polls, poller, ...ctl };
}

test("catch-up success while a poll fails: last reading kept with the note, then the refresh shows the new days", async () => {
  const p = panel();
  p.poller.start();
  p.polls.calls[0].resolve(emptyReading(at(0)));
  await flush();
  p.timers.fire(); // the next poll is in flight
  p.c.run();
  p.polls.calls[1].reject(new Error("bridge"));
  await flush();
  assert.equal(byClass(p.s.body, "pg-empty-title")[0].textContent, emptyReading().reason, "last reading kept");
  assert.match(p.s.refresh.textContent, /^Could not refresh the trend paper/);
  p.api.calls[0].resolve({ ok: true, status: "BOOKED", days_booked: 14, detail: "Booked 14 paper day(s): 2026-10-04 to 2026-10-17." });
  await flush();
  assert.equal(p.live.textContent, "Catch-up: Caught up. Booked 14 paper day(s): 2026-10-04 to 2026-10-17.");
  assert.equal(p.polls.calls.length, 3, "the panel reads the ledger again at once");
  p.polls.calls[2].resolve(reading({ generated_at: at(3) }));
  await flush();
  assert.equal(byClass(p.s.body, "tp-book").length, 2);
  assert.equal(p.s.refresh.textContent, "");
  assert.equal(p.live.writes, 1, "polls never announce");
  assert.equal(p.timers.pending.size, 1);
  p.poller.stop();
});

test("catch-up failure while a poll succeeds: the failure is announced once and the panel reads again", async () => {
  const p = panel();
  p.poller.start();
  p.polls.calls[0].resolve(emptyReading(at(0)));
  await flush();
  p.timers.fire(); // the next poll is in flight
  p.c.run();
  p.api.calls[0].reject(new Error("bridge"));
  const early = reading({ generated_at: at(1) });
  early.quotes[0].rows[0].equity = "1.00";
  p.polls.calls[1].resolve(early); // answered while the catch-up ran: it may predate it, so it is dropped
  await flush();
  assert.equal(p.live.textContent, TP.TEXT.catchUpFailed);
  assert.equal(p.live.writes, 1);
  assert.equal(p.button.getAttribute("aria-disabled"), "false");
  assert.equal(p.polls.calls.length, 3, "the restart polls again");
  assert.equal(byClass(p.s.body, "pg-empty-title")[0].textContent, emptyReading().reason);
  p.polls.calls[2].resolve(reading({ generated_at: at(2) }));
  await flush();
  assert.equal(byClass(p.s.body, "tp-book").length, 2);
  assert.ok(!p.s.body.textContent.includes("1.00as of"));
  // The next poll fails: the reading stays, with the note; no announcement.
  p.timers.fire();
  p.polls.calls[3].reject(new Error("bridge"));
  await flush();
  assert.equal(byClass(p.s.body, "tp-book").length, 2);
  assert.match(p.s.refresh.textContent, /^Could not refresh/);
  assert.equal(p.live.writes, 1);
  p.poller.stop();
});

test("a poll answered before the catch-up but arriving after the refresh is dropped", async () => {
  const p = panel();
  p.poller.start();
  p.c.run();
  p.api.calls[0].resolve({ ok: true, status: "BOOKED", days_booked: 14, detail: "Booked." });
  await flush();
  assert.equal(p.polls.calls.length, 2);
  p.polls.calls[1].resolve(reading({ generated_at: at(5) }));
  await flush();
  p.polls.calls[0].resolve(emptyReading(at(1))); // the old generation's answer, late
  await flush();
  assert.equal(byClass(p.s.body, "tp-book").length, 2, "the newer reading stays");
  // Even through the view alone, an older reading never replaces a newer one.
  assert.equal(p.v.render(emptyReading(at(4))), false);
  assert.equal(byClass(p.s.body, "tp-book").length, 2);
  p.poller.stop();
});

test("a catch-up finishing while the panel is hidden does not start polling", async () => {
  const p = panel();
  p.poller.start();
  p.c.run();
  p.poller.stop(); // the tab was left
  p.api.calls[0].resolve({ ok: true, status: "UP_TO_DATE", detail: "Every due paper day is already booked." });
  await flush();
  assert.equal(p.poller.isRunning(), false);
  assert.equal(p.polls.calls.length, 1);
  assert.equal(p.timers.pending.size, 0);
  assert.equal(p.live.writes, 1);
});

test("every block is a labelled section; tables have captions and header scopes", () => {
  const s = slots();
  view(s).render(reading());
  byClass(s.body, "tp-book").forEach((block) => {
    assert.equal(block.tagName, "section");
    const id = block.getAttribute("aria-labelledby");
    assert.equal(byTag(block, "h4")[0].getAttribute("id"), id);
    assert.equal(byTag(block, "caption").length, 1);
    byTag(byTag(block, "thead")[0], "th").forEach((th) => assert.equal(th.getAttribute("scope"), "col"));
    byClass(block, "tp-group").forEach((tr) => {
      assert.equal(tr.childNodes[0].getAttribute("scope"), "rowgroup");
      assert.equal(tr.childNodes[0].getAttribute("colspan"), "8");
    });
    rowsOf(block).forEach((tr) => {
      assert.equal(tr.childNodes[0].tagName, "th");
      assert.equal(tr.childNodes[0].getAttribute("scope"), "row");
      assert.equal(tr.childNodes.length, 8);
    });
  });
  const ids = byClass(s.body, "tp-book").map((b) => b.getAttribute("aria-labelledby"));
  assert.equal(new Set(ids).size, ids.length);
});

// -- skipped paper days and waiting for data --------------------
const SKIPS = [
  { day: "2026-10-06", reason: "no BTCUSDT candle on 2026-10-06 (no fill price)" },
  { day: "2026-10-07", reason: "no BTCEUR or EURUSDT candle on 2026-10-07 (no EUR price)" },
];

// A tree as tags, attributes and text, to compare two renders exactly.
function shape(node) {
  if (node.nodeType === 3) return node.data;
  return [node.tagName, node.attributes, node.childNodes.map(shape)];
}

test("skipped paper days show one calm line in the summary with each day and the backend's reason", () => {
  const s = slots();
  view(s).render(reading({ days_booked: 12, days_skipped: 2, skipped_days: SKIPS }));
  const lines = byTag(s.summary, "p");
  assert.equal(lines.length, 2, "the booked line, then one skipped line");
  assert.ok(lines[0].textContent.startsWith("12 paper days booked"));
  const line = byClass(s.summary, "tp-skipped");
  assert.equal(line.length, 1);
  assert.equal(line[0], lines[1]);
  assert.equal(line[0].getAttribute("class"), "tp-summary tp-dim tp-skipped", "existing calm summary styles only");
  assert.equal(line[0].textContent,
    "2 paper days skipped (a public candle is missing for good; no record, the books carry over unchanged): " +
    "2026-10-06 — no BTCUSDT candle on 2026-10-06 (no fill price); " +
    "2026-10-07 — no BTCEUR or EURUSDT candle on 2026-10-07 (no EUR price).");
  const monos = byClass(line[0], "mono").map((n) => n.textContent);
  assert.deepEqual(monos, ["2", "2026-10-06", "2026-10-07"]);
  assert.equal(byClass(line[0], "pill").length, 0, "no pill");
  assert.ok(!all(line[0]).some((n) => /tone-|err|warn/.test(n.getAttribute("class") || "")), "no colour");
  // The books themselves are drawn exactly as without skipped days.
  const plain = slots();
  view(plain).render(reading({ days_booked: 12 }));
  assert.deepEqual(shape(s.body), shape(plain.body));
  // One skipped day reads in the singular.
  const one = slots();
  view(one).render(reading({ days_skipped: 1, skipped_days: SKIPS.slice(0, 1) }));
  assert.ok(byClass(one.summary, "tp-skipped")[0].textContent.startsWith("1 paper day skipped ("));
});

test("with days_skipped 0 or the field missing the panel renders exactly as before", () => {
  const before = slots();
  view(before).render(reading());
  assert.equal(byTag(before.summary, "p").length, 1);
  [{ days_skipped: 0, skipped_days: [] }, { days_skipped: 0, skipped_days: SKIPS }, { skipped_days: SKIPS },
    { days_skipped: null }, { days_skipped: "2", skipped_days: SKIPS }, { days_skipped: -1 }].forEach((extra) => {
    const s = slots();
    view(s).render(reading(extra));
    assert.equal(byClass(s.summary, "tp-skipped").length, 0, JSON.stringify(extra));
    assert.deepEqual(shape(s.summary), shape(before.summary), JSON.stringify(extra));
    assert.deepEqual(shape(s.body), shape(before.body), JSON.stringify(extra));
  });
  const empty = slots();
  view(empty).render({ ...emptyReading(), days_skipped: 0, skipped_days: [] });
  assert.equal(empty.summary.childNodes.length, 0);
});

test("before any booked day, skipped days show the same line above the backend's empty reason", () => {
  const s = slots();
  const reason = "No paper day booked yet: 2 paper day(s) skipped because a public candle is missing.";
  view(s).render({ ...emptyReading(), reason, days_skipped: 2, skipped_days: SKIPS });
  assert.equal(byTag(s.summary, "p").length, 1);
  assert.ok(byClass(s.summary, "tp-skipped")[0].textContent.includes("2026-10-07 — no BTCEUR or EURUSDT"));
  assert.equal(byClass(s.body, "pg-empty-title")[0].textContent, reason);
  assert.equal(byTag(s.body, "table").length, 0, "no book is shown");
});

test("skipped-day strings from the backend are inserted as text only", () => {
  const s = slots();
  view(s).render(reading({
    days_skipped: 2,
    skipped_days: [{ day: HOSTILE, reason: HOSTILE }, { day: "2026-10-07", reason: null }, "junk", null],
  }));
  const line = byClass(s.summary, "tp-skipped")[0];
  assert.equal(line.textContent.split(HOSTILE).length - 1, 1, "the reason as text; a day that is not a date reads —");
  assert.ok(line.textContent.endsWith(": — — " + HOSTILE + "; 2026-10-07 — —."));
  assert.equal(all(s.summary).filter((n) => n.tagName === "img").length, 0);
  all(s.summary).forEach((n) => Object.values(n.attributes).forEach((v) => assert.ok(!v.includes("<"), v)));
});

test("a refused ledger renders unchanged, with no skipped line, whatever the skip fields say", () => {
  const plain = slots();
  view(plain).render(refusedReading());
  const s = slots();
  view(s).render({ ...refusedReading(), days_skipped: 2, skipped_days: SKIPS });
  assert.equal(s.summary.childNodes.length, 0);
  assert.deepEqual(shape(s.body), shape(plain.body));
  assert.equal(byClass(s.body, "pill")[0].getAttribute("class"), "pill err");
  assert.ok(s.body.textContent.includes("LEDGER_EDITED: record 3: hash mismatch"));
  assert.ok(!s.body.textContent.includes("2026-10-06"));
  const gone = slots();
  view(gone).render({ state: "unavailable", reason: "x", quotes: [], days_skipped: 2, skipped_days: SKIPS, generated_at: at(0) });
  assert.equal(gone.summary.childNodes.length, 0);
});

test("a skipped day that appears between two polls redraws the summary; the same reading again does not", () => {
  const s = slots();
  const v = view(s);
  v.render(reading({ generated_at: at(1) }));
  v.render(reading({ generated_at: at(2), days_skipped: 1, skipped_days: SKIPS.slice(0, 1) }));
  assert.equal(byClass(s.summary, "tp-skipped").length, 1);
  const shown = s.summary.firstChild;
  v.render(reading({ generated_at: at(3), days_skipped: 1, skipped_days: SKIPS.slice(0, 1) }));
  assert.equal(s.summary.firstChild, shown, "nothing rebuilt");
});

test("WAITING_FOR_DATA is not a success, invents no figure and is announced once without moving focus", async () => {
  focusCalls = 0;
  const { button, result, live, api, done } = control();
  assert.equal(TP.CATCH_UP_TEXT.WAITING_FOR_DATA.ok, false);
  assert.ok(!/\d/.test(TP.CATCH_UP_TEXT.WAITING_FOR_DATA.label + TP.CATCH_UP_TEXT.WAITING_FOR_DATA.fallback));
  button.click();
  const detail = "Waiting for public data (no BTCUSDT candle on 2026-10-06 yet (none after it either)); nothing was " +
    "written. The next start or Catch up now tries again.";
  api.calls[0].resolve({ ok: false, status: "WAITING_FOR_DATA", days_booked: 0, detail });
  await flush();
  assert.equal(live.textContent, `Catch-up: Waiting for data. ${detail}`);
  assert.equal(live.writes, 1, "announced once");
  assert.equal(result.textContent, live.textContent);
  assert.equal(result.getAttribute("class"), "tp-result tp-warn", "not shown as a success");
  assert.ok(!/Caught up|Booked/.test(live.textContent));
  assert.equal(button.getAttribute("aria-disabled"), "false");
  assert.equal(focusCalls, 0, "focus never moved");
  assert.deepEqual(done, ["refresh"]);
  const bare = TP.catchUpMessage({ status: "WAITING_FOR_DATA", detail: null });
  assert.equal(bare.text,
    "Catch-up: Waiting for data. The public candles do not cover every due paper day yet; nothing was written.");
  assert.equal(bare.ok, false);
});
