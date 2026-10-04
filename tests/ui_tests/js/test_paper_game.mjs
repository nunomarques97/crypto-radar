// Regression tests for the Game tab's pure helpers and view (ui/web/paper_game.js).
// The view is exercised with a tiny hand-rolled DOM stub (createElement,
// createTextNode, appendChild, setAttribute, textContent) - enough to prove that
// every backend string lands as text and never as markup.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const PG = require("../../../ui/web/paper_game.js");
const OV = require("../../../ui/web/paper_office.js");
const ENGINE = require("../../../ui/web/paper_office_engine.js");
const WEB = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "ui", "web");

// -- a minimal DOM ----------------------------------------------------------------
class FakeText {
  constructor(data) { this.nodeType = 3; this.data = String(data); this.parentNode = null; }
  get textContent() { return this.data; }
}

class FakeElement {
  constructor(tag) {
    this.nodeType = 1; this.tagName = String(tag).toLowerCase(); this.childNodes = [];
    this.attributes = {}; this.parentNode = null; this.className = "";
    this.style = { setProperty(name, value) { this[name] = value; } };
  }
  appendChild(child) { child.parentNode = this; this.childNodes.push(child); return child; }
  removeChild(child) { this.childNodes = this.childNodes.filter((c) => c !== child); child.parentNode = null; return child; }
  get firstChild() { return this.childNodes[0] || null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(value) { this.childNodes = [new FakeText(value)]; }
  set innerHTML(_value) { throw new Error("innerHTML must never be used"); }
}

const fakeDoc = {
  createElement: (tag) => new FakeElement(tag),
  createElementNS: (_ns, tag) => new FakeElement(tag),
  createTextNode: (data) => new FakeText(data),
};

function all(node, out = []) {
  if (node.nodeType === 1) {
    out.push(node);
    node.childNodes.forEach((c) => all(c, out));
  }
  return out;
}

function slots() {
  const names = ["radar", "game", "wallet", "playStatus", "play", "history", "office", "live", "refresh"];
  return Object.fromEntries(names.map((n) => [n, new FakeElement("div")]));
}

const HOSTILE = "<img src=x onerror=alert(1)>";
const NOW = Date.parse("2026-09-29T10:30:00Z");

function play(overrides) {
  return {
    play_id: 1, asset: "SOL", pair: "SOL/EUR", quote: "EUR", direction: "LONG", direction_text: "Bets it goes up",
    opened_at: "2026-09-29T10:00:00Z", due_at: "2026-09-29T11:00:00Z", status: "OPEN", stake: "100.00",
    entry_bid: "142.10", entry_ask: "142.18", entry_mid: "142.14", now_bid: "142.61", now_ask: "142.70",
    now_ts: "2026-09-29T10:29:30Z", gross_now: "0.30", price_line: [], why: "The price rose fast.",
    steps: [
      { title: "1. The radar noticed", text: "Looked at 600 coins.", state: "done" },
      { title: "2. The AI checked", text: "Confirmed on 3 time frames.", state: "done" },
      { title: "3. Now it waits", text: "It closes by itself after 1 hour.", state: "now" },
    ],
    ...overrides,
  };
}

function state(overrides) {
  return {
    enabled: true, available: true, reason: null, pretend_money: true, currency: "EUR",
    params: { start_balance: "1000.00", stake: "100.00", max_open: 3, hold_minutes: 60, fee_bps: "26" },
    wallet: {
      balance: "987.40", start_balance: "1000.00", change: "-12.60", change_pct: "-1.26", open_stakes: "100.00",
      available_cash: "887.40", wins: 1, losses: 2, flats: 0, fees_total: "1.56",
      series: [
        { ts: "2026-09-29T08:00:00Z", balance: "1000.00" },
        { ts: "2026-09-29T09:00:00Z", balance: "1000.35" },
        { ts: "2026-09-29T09:30:00Z", balance: "987.40" },
      ],
      last_results: ["WIN", "LOSS", "LOSS"], cost_sentence: "Every buy and every sell pays a commission of 0.26%.",
    },
    open_plays: [play()],
    history: [{ play_id: 9, asset: "DOGE", closed_at: "2026-09-29T09:30:00Z", net: "0.35", outcome: "WIN", sentence: "It won." }],
    history_total: 1,
    agents: [{ id: "scout", name: "Scout", color: "#5B9DF6", source: "radar_runs", enabled: true, last_activity_ts: null }],
    activity: [],
    generated_at: "2026-09-29T10:30:00Z",
    ...overrides,
  };
}

// -- money and percentages -----------------------------------------------------------
test("money is formatted from the bridge's decimal strings in English format", () => {
  assert.equal(PG.formatMoney("1000.00", "EUR"), "€1,000.00");
  assert.equal(PG.formatMoney("1234567.5", "EUR"), "€1,234,567.5");
  assert.equal(PG.formatMoney("0.35", "EUR", { signed: true }), "+€0.35");
  assert.equal(PG.formatMoney("-0.58", "EUR", { signed: true }), "-€0.58");
  assert.equal(PG.formatMoney("-12.60", "EUR"), "-€12.60");
  assert.equal(PG.formatMoney("0.00", "EUR", { signed: true }), "€0.00");
  assert.equal(PG.formatMoney("-0.00", "EUR", { signed: true }), "€0.00");
  assert.equal(PG.formatMoney("142.18", "USD"), "$142.18");
  assert.equal(PG.formatMoney("0.5", "USDT"), "0.5 USDT");
});

test("the digits shown are exactly the backend's digits (no float rounding)", () => {
  assert.equal(PG.formatMoney("0.1000000000000000055", "EUR"), "€0.1000000000000000055");
  assert.equal(PG.formatPrice("0.00001234", "EUR"), "€0.00001234");
  assert.equal(PG.formatMoney("007.10", "EUR"), "€7.10");
});

test("anything that is not a decimal string is not shown as money", () => {
  for (const bad of [null, undefined, 12.5, "", "abc", "1e3", "1,000.00", "NaN", "Infinity", HOSTILE]) {
    assert.equal(PG.formatMoney(bad, "EUR"), null, String(bad));
  }
});

test("percentages and tones", () => {
  assert.equal(PG.formatPercent("-1.26", { signed: true }), "-1.26%");
  assert.equal(PG.formatPercent("0.30", { signed: true }), "+0.30%");
  assert.equal(PG.formatPercent("x"), null);
  assert.equal(PG.toneOf("0.01"), "up");
  assert.equal(PG.toneOf("-0.01"), "down");
  assert.equal(PG.toneOf("0.00"), "flat");
  assert.equal(PG.toneOf(null), "flat");
});

// -- the balance chart ----------------------------------------------------------------
test("chart geometry spreads the recorded balances and draws the start line", () => {
  const g = PG.chartGeometry(state().wallet.series, "1000.00", { width: 600, height: 120, pad: 10 });
  assert.deepEqual(g.points.map((p) => p.x), [0, 300, 600]);
  assert.equal(g.points[1].y, 10, "the highest balance touches the top padding");
  assert.equal(g.points[2].y, 110, "the lowest balance touches the bottom padding");
  assert.equal(g.startY, g.points[0].y, "the dashed start line passes through the starting balance");
  assert.ok(g.startY > 10 && g.startY < 110);
  assert.equal(g.trend, "down");
  assert.match(g.line, /^M0,[\d.]+ L300,10 L600,110$/);
  assert.ok(g.area.endsWith("L600,120 L0,120 Z"));
});

test("a single recorded balance is a flat line at the start", () => {
  const g = PG.chartGeometry([{ ts: "t", balance: "1000.00" }], "1000.00", { width: 600, height: 120, pad: 10 });
  assert.equal(g.points.length, 1);
  assert.equal(g.startY, 60);
  assert.equal(g.line, "M0,60 L600,60");
  assert.equal(g.trend, "flat");
});

test("the chart skips unusable points and draws nothing without any", () => {
  assert.equal(PG.chartGeometry([], "1000.00"), null);
  assert.equal(PG.chartGeometry(null, "1000.00"), null);
  assert.equal(PG.chartGeometry([{ balance: 12 }, { balance: "x" }, null], "1000.00"), null);
  const g = PG.chartGeometry([{ balance: "1000.00" }, { balance: "bad" }, { balance: "1010.00" }], "1000.00");
  assert.equal(g.points.length, 2);
  assert.equal(g.trend, "up");
});

// -- the time bar ------------------------------------------------------------------------
test("time-bar fraction and the closing text", () => {
  const p = play();
  assert.equal(PG.timeFraction(p.opened_at, p.due_at, NOW), 0.5);
  assert.equal(PG.timeFraction(p.opened_at, p.due_at, NOW - 3600e3), 0);
  assert.equal(PG.timeFraction(p.opened_at, p.due_at, NOW + 3600e3), 1);
  assert.equal(PG.timeFraction("bad", p.due_at, NOW), null);
  assert.equal(PG.timeFraction(p.due_at, p.opened_at, NOW), null);
  assert.equal(PG.minutesLeft(p.due_at, NOW), 30);
  assert.match(PG.closingText(p, NOW), /^Closes in 30 min \(at \d\d:\d\d\)\.$/);
  assert.equal(PG.closingText(p, NOW + 3600e3), "Closing now.");
  assert.equal(PG.closingText(play({ status: "PENDING_EXIT" }), NOW), "Closing time passed: it closes at the first valid price.");
  assert.equal(PG.closingText(play({ due_at: null }), NOW), "Closes at the planned time.");
});

// -- which state to show ------------------------------------------------------------------
test("empty, unavailable and disabled states are selected calmly", () => {
  assert.equal(PG.selectView(undefined).kind, "loading");
  assert.equal(PG.selectView(null).kind, "unavailable");
  assert.equal(PG.selectView([]).kind, "unavailable");
  const waiting = PG.selectView({ enabled: true, available: false, reason: "No radar data yet.", wallet: null });
  assert.deepEqual(waiting, { kind: "waiting", message: "No radar data yet.", disabled: false });
  assert.equal(PG.selectView({ available: false, reason: null }).message, PG.TEXT.noPlays);
  const off = PG.selectView(state({ enabled: false, reason: "The game is switched off." }));
  assert.equal(off.kind, "ready");
  assert.equal(off.disabled, true);
  assert.equal(PG.selectView(state()).kind, "ready");
});

test("the radar pill maps the process states and nothing else", () => {
  assert.deepEqual(PG.radarPill("RUNNING"), { cls: "ok", label: "Radar on" });
  assert.equal(PG.radarPill("ERROR").cls, "err");
  assert.equal(PG.radarPill("STOPPED").cls, "idle");
  assert.equal(PG.radarPill(undefined).label, "Radar state unknown");
});

// -- stale responses ---------------------------------------------------------------------
test("a response older than one already applied is ignored", () => {
  const guard = PG.createStaleGuard();
  const a = guard.next();
  const b = guard.next();
  assert.equal(guard.accept(b, "2026-09-29T10:00:02Z"), true);
  assert.equal(guard.accept(a, "2026-09-29T10:00:01Z"), false, "older request resolving late");
  assert.equal(guard.acceptFailure(a), false, "an older failure does not replace a newer success");
  const c = guard.next();
  assert.equal(guard.accept(c, "2026-09-29T10:00:00Z"), false, "newer request but older reading");
  const d = guard.next();
  assert.equal(guard.acceptFailure(d), true);
  const e = guard.next();
  assert.equal(guard.accept(e, "2026-09-29T10:00:03Z"), true, "refresh succeeds after a failed one");
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

const flush = () => new Promise((r) => setImmediate(r));

test("the poller drops a response that arrives after stop/start, and keeps a single timer", async () => {
  const timers = fakeTimers();
  const resolvers = [];
  const applied = [];
  const poller = PG.createPoller({
    intervalMs: 2000, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    fetchState: () => new Promise((resolve) => resolvers.push(resolve)),
    onState: (s) => applied.push(s.generated_at), onError: () => applied.push("error"),
  });
  poller.start();
  poller.stop();
  poller.start();
  assert.equal(poller.start(), null, "start while running does nothing");
  assert.equal(resolvers.length, 2);
  resolvers[1]({ generated_at: "2026-09-29T10:00:02Z" });
  resolvers[0]({ generated_at: "2026-09-29T10:00:01Z" });
  await flush();
  assert.deepEqual(applied, ["2026-09-29T10:00:02Z"]);
  assert.equal(timers.pending.size, 1, "exactly one next poll is scheduled");
  poller.stop();
  assert.equal(timers.pending.size, 0);
  assert.equal(poller.isRunning(), false);
  // StrictMode-like repeated setup/cleanup/setup
  for (let i = 0; i < 3; i += 1) { poller.start(); poller.stop(); }
  poller.start();
  resolvers.slice(2).forEach((r, i) => r({ generated_at: `2026-09-29T10:00:1${i}Z` }));
  await flush();
  assert.equal(timers.pending.size, 1);
  assert.equal(applied.length, 2, "only the last generation's response is applied");
  poller.stop();
});

test("the poller retries after a failed refresh and recovers", async () => {
  const timers = fakeTimers();
  const events = [];
  let calls = 0;
  const poller = PG.createPoller({
    intervalMs: 2000, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    fetchState: () => {
      calls += 1;
      if (calls === 1) throw new Error("bridge hiccup");
      if (calls === 2) return Promise.reject(new Error("again"));
      return { generated_at: "2026-09-29T10:00:05Z" };
    },
    onState: () => events.push("state"), onError: () => events.push("error"),
  });
  poller.start();
  await flush();
  timers.fire();
  await flush();
  timers.fire();
  await flush();
  assert.deepEqual(events, ["error", "error", "state"]);
  poller.stop();
});

// -- the view: text only, calm empty states -----------------------------------------------
test("hostile backend strings are inserted as text, never as markup", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  const hostile = state({
    currency: HOSTILE,
    open_plays: [play({ asset: HOSTILE, pair: HOSTILE, direction_text: HOSTILE, why: HOSTILE,
      steps: [{ title: HOSTILE, text: HOSTILE, state: "now" }] }), play({ play_id: 2, asset: "BTC" })],
    history: [{ play_id: 9, asset: HOSTILE, closed_at: HOSTILE, net: HOSTILE, outcome: "WIN", sentence: HOSTILE }],
    agents: [{ id: "scout", name: HOSTILE, color: "red;background:url(http://x)", enabled: true, last_activity_ts: HOSTILE }],
  });
  hostile.wallet.cost_sentence = HOSTILE;
  view.render(hostile);
  const nodes = Object.values(s).flatMap((slot) => all(slot));
  assert.equal(nodes.filter((n) => n.tagName === "img").length, 0);
  assert.ok(nodes.every((n) => !Object.keys(n.attributes).some((k) => k.startsWith("on"))));
  assert.ok(s.play.textContent.includes(HOSTILE));
  assert.ok(s.history.textContent.includes(HOSTILE));
  assert.ok(s.office.textContent.includes(HOSTILE));
  assert.ok(s.wallet.textContent.includes(HOSTILE));
  const swatch = all(s.office).find((n) => n.tagName === "i");
  assert.equal(swatch.getAttribute("style"), "background:#4B5563", "an unexpected colour is not passed to CSS");
  // the newer play replaces the hostile one on screen; the old one is listed below it as text
  view.render(state({ open_plays: [play({ asset: HOSTILE }), play({ play_id: 2, asset: "BTC" })] }));
  assert.ok(all(s.play).some((n) => n.tagName === "ul" && n.textContent.includes(HOSTILE)));
});

test("empty wallet and no play show calm English text", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  view.render(undefined);
  assert.equal(s.wallet.textContent, PG.TEXT.loading);
  view.render({ enabled: true, available: false, reason: "No plays yet: the radar has not started the game.",
    params: { start_balance: "1000.00" }, wallet: null, open_plays: [], history: [], agents: [], generated_at: "2026-09-29T10:30:00Z" });
  assert.match(s.wallet.textContent, /No plays yet: the radar has not started the game\./);
  assert.match(s.wallet.textContent, /starts with €1,000\.00/);
  assert.match(s.play.textContent, /^Looking for a play/);
  assert.equal(s.history.textContent, PG.TEXT.noPlays);
  view.render(state({ open_plays: [], history: [], history_total: 0 }));
  assert.match(s.play.textContent, /^Looking for a play/);
  assert.equal(s.history.textContent, PG.TEXT.noPlays);
  assert.equal(s.playStatus.textContent, "");
});

test("a switched-off game says so and opens nothing", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  view.render(state({ enabled: false, open_plays: [] }));
  assert.equal(s.game.textContent, "Game switched off");
  assert.match(s.play.textContent, /switched off/);
});

test("the wallet and the play show the backend's numbers with the currency", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  view.render(state());
  assert.match(s.wallet.textContent, /€987\.40/);
  assert.match(s.wallet.textContent, /-€12\.60 \(-1\.26%\)/);
  assert.match(s.wallet.textContent, /dashed line = starting balance/);
  assert.ok(all(s.wallet).some((n) => n.getAttribute("class") === "pg-start-line" && n.getAttribute("stroke-dasharray")));
  assert.match(s.play.textContent, /Went in at€142\.18/, "a LONG goes in at the ask");
  assert.match(s.play.textContent, /Now€142\.61/, "a LONG would sell at the bid");
  assert.match(s.play.textContent, /Stake€100\.00/);
  assert.match(s.play.textContent, /\+€0\.30 so far, before costs/);
  const fill = all(s.play).find((n) => n.getAttribute("class") === "pg-fill");
  assert.equal(fill.getAttribute("style"), "width:50%");
  assert.equal(s.playStatus.textContent, "in progress");
  assert.match(s.history.textContent, /\+€0\.35/);
});

test("a failed refresh keeps the last reading and says so; a first failure shows the calm error", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  view.renderError();
  assert.equal(s.wallet.textContent, PG.TEXT.unreadable);
  view.render(state());
  view.renderError();
  assert.match(s.wallet.textContent, /€987\.40/);
  assert.match(s.refresh.textContent, /^Could not refresh; showing the reading from \d\d:\d\d:\d\d\.$/);
  view.render(state({ generated_at: "2026-09-29T10:30:02Z" }));
  assert.equal(s.refresh.textContent, "");
});

test("the live region announces only a play opening or closing", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  view.render(state({ open_plays: [], history: [], history_total: 0 }));
  assert.equal(s.live.textContent, "");
  view.render(state({ open_plays: [], history: [], history_total: 0 }));
  assert.equal(s.live.textContent, "");
  view.render(state());
  assert.equal(s.live.textContent, "A play closed. It won. New play: SOL, Bets it goes up.");
});

test("unchanged panels are not rebuilt between polls", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  view.render(state());
  const before = s.history.firstChild;
  view.render(state({ generated_at: "2026-09-29T10:30:02Z" }));
  assert.equal(s.history.firstChild, before);
});

test("with no closed play the chart shows only the dashed start line", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  const w = state().wallet;
  view.render(state({ open_plays: [], history: [], history_total: 0,
    wallet: { ...w, balance: "1000.00", change: "0.00", change_pct: "0.00", series: [w.series[0]], last_results: [] } }));
  const classes = all(s.wallet).map((n) => n.getAttribute("class"));
  assert.ok(classes.includes("pg-start-line"));
  assert.ok(!classes.includes("pg-balance-line"));
  view.render(state());
  assert.ok(all(s.wallet).some((n) => n.getAttribute("class") === "pg-balance-line"));
});

test("the time bar on screen keeps moving after an unchanged poll", () => {
  const s = slots();
  let clock = NOW;
  const view = PG.createView(s, { doc: fakeDoc, now: () => clock });
  view.render(state());
  const shown = () => all(s.play).find((n) => n.getAttribute("class") === "pg-fill");
  const fill = shown();
  view.render(state({ generated_at: "2026-09-29T10:30:02Z" }));
  assert.equal(shown(), fill, "the play panel was not rebuilt");
  clock = Date.parse("2026-09-29T10:45:00Z");
  view.tick();
  assert.equal(shown().getAttribute("style"), "width:75%", "the fill on screen follows the clock");
  assert.match(s.play.textContent, /Closes in 15 min/);
});

test("an open play while the game is switched off is shown as paused, not counting down", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  view.render(state({ enabled: false, reason: "disabled", open_plays: [play(), play({ play_id: 2, asset: "ETH" })] }));
  assert.equal(s.playStatus.textContent, "paused");
  assert.match(s.play.textContent, /closes only after it is switched back on/);
  assert.doesNotMatch(s.play.textContent, /Closes in/);
  view.render(state({ generated_at: "2026-09-29T10:30:02Z" }));
  assert.equal(s.playStatus.textContent, "in progress", "switching back on restores the countdown");
  assert.match(s.play.textContent, /Closes in 30 min/);
});

// -- the wallet valuation, fee provenance and FX label --------------------
const BASIS = "Open positions are valued as if closed now at the price you could actually sell at "
  + "(or buy back at, for a bet on a fall), after the assumed commission on both the buy and the sell.";
const FRESH = "A position is valued only on a valid price of its own pair, recorded since it opened and at most 10 minutes old.";
const FEE = "Assumed commission of 0.26% on the buy and again on the sell (ASSUMED, account tier unverified).";
const FX = "Hypothetical simulation: this pair is priced in USD; its price moves are scaled onto the EUR stake "
  + "with no exchange rate (FX excluded). This is not EUR inventory.";
const TOO_OLD = "The last price of this pair is too old to value it now.";

// wallet.valuation as ui/paper_reader.py builds it; the identity holds on these strings:
// total 987.50 = free cash 887.40 + liquidation 100.10 = realized balance 987.40 + open net 0.10.
function valuation(overrides) {
  return {
    currency: "EUR", as_of: "2026-09-29T10:30:00Z", valuation_basis: "conservative_liquidation",
    valuation_basis_text: BASIS, identity: "total_equity = free_cash + liquidation_value = realized_balance + open_net_pnl",
    freshness: { max_age_seconds: 600, text: FRESH }, mark_status: "all_marked", stale: false, unmarked: [], open_count: 1,
    free_cash: "887.40", open_cost_basis: "100.00", liquidation_value: "100.10", realized_balance: "987.40",
    realized_pnl: "-12.60", open_net_pnl: "0.10", total_equity: "987.50",
    ...overrides,
  };
}

const MARKED = { status: "marked", stale: false, reason_text: null, bid: "142.61", ask: "142.70",
  ts: "2026-09-29T10:29:30Z", age_seconds: 30 };
const STALE_MARK = { status: "stale_quote", stale: true, reason_text: TOO_OLD, bid: null, ask: null,
  ts: "2026-09-29T10:05:00Z", age_seconds: 1500 };

function valuedPlay(overrides) {
  return play({ fee_bps: "26", fee_source: "stored_play_fee_bps", account_tier_verified: false, fee_text: FEE,
    fx_excluded: false, fx_text: null, cost_basis: "100.00", mark: MARKED, liquidation_value: "100.10",
    open_net_pnl: "0.10", ...overrides });
}

function valued(overrides) {
  const base = state();
  return state({
    wallet: { ...base.wallet, valuation: valuation() },
    open_plays: [valuedPlay()],
    history: [{ ...base.history[0], fee_bps: "26", fee_text: FEE, fx_excluded: false, fx_text: null }],
    ...overrides,
  });
}

// A USD-priced play whose last price is too old: the dependent totals are null.
function staleValued() {
  const base = valued();
  return valued({
    wallet: { ...base.wallet, valuation: valuation({ mark_status: "unavailable", stale: true,
      unmarked: [{ play_id: 1, pair: "SOL/USD", status: "stale_quote" }],
      liquidation_value: null, open_net_pnl: null, total_equity: null }) },
    open_plays: [valuedPlay({ pair: "SOL/USD", quote: "USD", fx_excluded: true, fx_text: FX, mark: STALE_MARK,
      liquidation_value: null, open_net_pnl: null })],
    history: [{ ...base.history[0], fx_excluded: true, fx_text: FX }],
  });
}

// The "Value now" group: label -> text of the amount cell.
function valueRows(slot) {
  const group = all(slot).find((n) => n.getAttribute("aria-labelledby") === "pg-value-h");
  if (!group) return null;
  const rows = {};
  all(group).filter((n) => n.tagName === "dt").forEach((dt) => {
    const dd = dt.parentNode.childNodes.find((c) => c.tagName === "dd");
    rows[dt.textContent] = dd.textContent;
  });
  return { group, rows };
}

test("the wallet shows every valuation amount with its label, basis, freshness and as-of", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  view.render(valued());
  const { group, rows } = valueRows(s.wallet);
  assert.deepEqual(rows, {
    "Total value now": "€987.50",
    "Free cash (not in a play)": "€887.40",
    "Put into open plays": "€100.00",
    "Open plays if closed now": "€100.10",
    "Result of closed plays": "-€12.60",
    "Open plays after all costs": "+€0.10",
  });
  assert.deepEqual(PG.VALUE_ROWS.map((r) => r.key), ["total_equity", "free_cash", "open_cost_basis",
    "liquidation_value", "realized_pnl", "open_net_pnl"]);
  assert.equal(group.getAttribute("role"), "group");
  assert.match(group.textContent, /Value now, if every open play closed/);
  assert.match(group.textContent, /as of \d\d:\d\d:\d\d/);
  assert.ok(group.textContent.includes(BASIS));
  assert.ok(group.textContent.includes(FRESH));
  assert.doesNotMatch(group.textContent, /unknown|too old/);
  assert.match(s.wallet.textContent, /Balance after closed plays\. Started with/);
});

test("the current play and each history row show the stored fee as ASSUMED and the play's value now", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  view.render(valued());
  assert.match(s.play.textContent, /If closed now€100\.10/);
  assert.match(s.play.textContent, /After all costs\+€0\.10/);
  assert.ok(s.play.textContent.includes(FEE));
  assert.ok(s.history.textContent.includes(FEE));
  assert.doesNotMatch(s.play.textContent + s.history.textContent, /FX excluded/, "a EUR-priced play has no FX label");
});

test("a stale mark leaves the dependent totals as a dash with the reason, never 0", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  view.render(staleValued());
  const { group, rows } = valueRows(s.wallet);
  assert.equal(rows["Total value now"], "—unknown: price too old");
  assert.equal(rows["Open plays if closed now"], "—unknown: price too old");
  assert.equal(rows["Open plays after all costs"], "—unknown: price too old");
  assert.equal(rows["Free cash (not in a play)"], "€887.40", "known cash stays available");
  assert.equal(rows["Put into open plays"], "€100.00");
  assert.equal(rows["Result of closed plays"], "-€12.60");
  assert.doesNotMatch(Object.values(rows).join("|"), /€0\.00/);
  assert.match(group.getAttribute("class"), /\bis-stale\b/);
  assert.match(group.textContent, /price missing or too old/);
  assert.ok(group.textContent.includes("SOL/USD " + TOO_OLD), "the unpriced play is named with the backend's reason");
  // the play: no value now, the reason, the fee and the FX-excluded label
  assert.match(s.play.textContent, /If closed now—/);
  assert.match(s.play.textContent, /After all costs—/);
  assert.ok(s.play.textContent.includes(TOO_OLD));
  assert.ok(s.play.textContent.includes(FX));
  assert.ok(s.play.textContent.includes(FEE));
  assert.ok(s.history.textContent.includes(FX));
});

test("a value that is not a decimal string is a dash, and a real zero is shown as zero", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  const base = valued();
  view.render(valued({ wallet: { ...base.wallet, valuation: valuation({ total_equity: 12.5, open_net_pnl: "0.00",
    unmarked: [{ play_id: 77, pair: HOSTILE, status: "no_such_status" }] }) } }));
  const { group, rows } = valueRows(s.wallet);
  assert.equal(rows["Total value now"], "—unknown: no usable price");
  assert.equal(rows["Open plays after all costs"], "€0.00");
  assert.ok(group.textContent.includes(HOSTILE), "a hostile pair is text");
  assert.equal(all(s.wallet).filter((n) => n.tagName === "img").length, 0);
});

test("with nothing open the valuation equals the balance and names no missing price", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  const base = valued();
  view.render(valued({ open_plays: [], wallet: { ...base.wallet, valuation: valuation({ mark_status: "no_open_positions",
    open_count: 0, free_cash: "987.40", open_cost_basis: "0.00", liquidation_value: "0.00", open_net_pnl: "0.00",
    total_equity: "987.40" }) } }));
  const { group, rows } = valueRows(s.wallet);
  assert.equal(rows["Total value now"], "€987.40");
  assert.equal(rows["Open plays if closed now"], "€0.00");
  assert.doesNotMatch(group.textContent, /unknown|price missing/);
  assert.match(s.play.textContent, /^Looking for a play/);
});

test("a legacy payload without the valuation block renders as before", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  view.render(state());
  assert.equal(valueRows(s.wallet), null);
  assert.doesNotMatch(s.wallet.textContent, /Balance after closed plays|Value now/);
  assert.match(s.wallet.textContent, /€987\.40/);
  assert.doesNotMatch(s.play.textContent, /If closed now|After all costs|ASSUMED/);
  assert.doesNotMatch(s.history.textContent, /ASSUMED|FX excluded/);
  assert.match(s.play.textContent, /\+€0\.30 so far, before costs/);
});

test("a new reading updates the as-of in place without rebuilding the wallet; a new reason rebuilds it", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  view.render(valued());
  const before = s.wallet.firstChild;
  const at = () => all(s.wallet).find((n) => n.getAttribute("class") === "pg-value-at mono").textContent;
  const first = at();
  const base = valued();
  view.render(valued({ generated_at: "2026-09-29T10:31:00Z",
    wallet: { ...base.wallet, valuation: valuation({ as_of: "2026-09-29T10:31:00Z" }) },
    open_plays: [valuedPlay({ mark: { ...MARKED, age_seconds: 90 } })] }));
  assert.equal(s.wallet.firstChild, before, "the wallet (and anything focused in it) is kept");
  assert.notEqual(at(), first, "the as-of on screen follows the reading");
  assert.match(at(), /^as of \d\d:\d\d:\d\d$/);
  assert.equal(s.live.textContent, "", "a valuation update is not announced");
  // the play's price turns stale: the wallet is rebuilt and names the reason
  view.render({ ...staleValued(), generated_at: "2026-09-29T10:32:00Z" });
  assert.notEqual(s.wallet.firstChild, before);
  assert.ok(s.wallet.textContent.includes(TOO_OLD));
  assert.equal(s.live.textContent, "", "a stale price is not a play opening or closing");
});

test("a failed poll keeps the last valuation with the stale notice; a later reading clears it", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW });
  view.render(valued());
  view.renderError();
  assert.equal(valueRows(s.wallet).rows["Total value now"], "€987.50");
  assert.match(s.refresh.textContent, /^Could not refresh; showing the reading from \d\d:\d\d:\d\d\.$/);
  view.render({ ...staleValued(), generated_at: "2026-09-29T10:30:05Z" });
  assert.equal(s.refresh.textContent, "", "a refresh after a failed one clears the notice");
  assert.equal(valueRows(s.wallet).rows["Total value now"], "—unknown: price too old");
});

test("the signature ignores only the reading's as-of and price age", () => {
  const a = valued();
  const b = valued();
  b.wallet.valuation.as_of = "2026-09-29T11:00:00Z";
  b.open_plays[0].mark = { ...MARKED, age_seconds: 400 };
  assert.equal(PG.signature([a.wallet, a.open_plays]), PG.signature([b.wallet, b.open_plays]));
  b.wallet.valuation.total_equity = "987.51";
  assert.notEqual(PG.signature([a.wallet, a.open_plays]), PG.signature([b.wallet, b.open_plays]));
});

// -- EX-1 exit rule: stop, target, time left and close reason -----------------------------
const LEVELS = {
  exit_policy: "ex1_initial_paper_v1", stop: "139.18", target: "148.18", max_hold_minutes: 1440,
  due_at: "2026-09-30T10:00:00Z", exit_due_at: "2026-09-30T10:00:00Z",
};
const REASONS = [
  { exit_reason: "stop", exit_reason_text: "Hit the stop", net: "-2.41", sentence: "SOL lost €2.41. It hit the stop." },
  { exit_reason: "target", exit_reason_text: "Hit the target", net: "4.10", sentence: "ETH won €4.10. It hit the target." },
  { exit_reason: "time", exit_reason_text: "Closed at the 24-hour limit", net: "0.12", sentence: "ADA won €0.12. It reached the 24-hour limit." },
];

test("durations and the countdown text of a play with levels and of a legacy play", () => {
  assert.equal(PG.durationText(0), "0 min");
  assert.equal(PG.durationText(45), "45 min");
  assert.equal(PG.durationText(60), "1 h");
  assert.equal(PG.durationText(1392), "23 h 12 min");
  for (const bad of [null, -1, 1.5, NaN, "60"]) assert.equal(PG.durationText(bad), null);
  const ex1 = play(LEVELS);
  assert.equal(PG.hasLevels(ex1), true);
  assert.equal(PG.dueOf(ex1), LEVELS.exit_due_at);
  assert.equal(PG.minutesLeft(PG.dueOf(ex1), NOW), 1410);
  assert.match(PG.closingText(ex1, NOW), /^At most 23 h 30 min left \(until \d\d:\d\d\)\.$/);
  assert.equal(PG.closingText(play({ ...LEVELS, due_at: null, exit_due_at: null }), NOW),
    "Closes at the stop, the target or the time limit.");
  assert.equal(PG.closingText(play({ due_at: "2026-09-29T12:00:00Z" }), NOW).replace(/\d\d:\d\d/, "HH:MM"),
    "Closes in 1 h 30 min (at HH:MM).", "a legacy play keeps its own hold");
  assert.equal(PG.hasLevels(play()), false);
  assert.equal(PG.hasLevels(play({ stop: "139.18", target: null })), false, "one level alone is not a plan");
  assert.equal(PG.hasLevels(play({ stop: "abc", target: "148.18" })), false);
});

test("the play panel shows stop, target and time left only when the payload has them", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW, officeView: null });
  view.render(state({ open_plays: [play(LEVELS)] }));
  const text = s.play.textContent;
  assert.match(text, /Stop \(loss limit\)€139\.18/);
  assert.match(text, /Target \(profit goal\)€148\.18/);
  assert.match(text, /At most 23 h 30 min left/);
  assert.match(text, /Stake€100\.00/);
  const bar = all(s.play).find((n) => n.getAttribute("role") === "progressbar");
  assert.equal(bar.getAttribute("aria-valuenow"), "2", "30 min of a 24 h limit");
  assert.equal(bar.getAttribute("aria-label"), "Time used of the play's time limit");

  for (const missing of [play(), play({ stop: null, target: null, exit_due_at: null }), play({ stop: "139.18" })]) {
    const t = slots();
    PG.createView(t, { doc: fakeDoc, now: () => NOW, officeView: null }).render(state({ open_plays: [missing] }));
    assert.doesNotMatch(t.play.textContent, /Stop|Target|loss limit|profit goal/, "absent, never shown as 0");
    assert.doesNotMatch(t.play.textContent, /€0\.00/);
    assert.match(t.play.textContent, /Closes in 30 min/, "a legacy play counts down to its own close");
  }
});

test("the history names each recorded close reason and a legacy close has none", () => {
  const s = slots();
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW, officeView: null });
  const legacy = { play_id: 1, asset: "DOT", closed_at: "2026-09-29T08:00:00Z", net: "0.20", outcome: "WIN",
    sentence: "DOT won €0.20.", exit_reason: null, exit_reason_text: null };
  const items = REASONS.map((r, i) => ({ play_id: 10 + i, asset: "SOL", closed_at: "2026-09-29T09:00:00Z",
    outcome: "WIN", ...r })).concat([legacy]);
  view.render(state({ history: items, history_total: items.length }));
  const chips = all(s.history).filter((n) => n.getAttribute("class") === "pg-reason");
  assert.deepEqual(chips.map((n) => n.textContent), ["Hit the stop", "Hit the target", "Closed at the 24-hour limit"]);
  const rows = all(s.history).filter((n) => n.tagName === "li");
  assert.equal(rows.length, 4);
  assert.match(rows[3].textContent, /DOT won €0\.20\./);
  assert.ok(!all(rows[3]).some((n) => n.getAttribute("class") === "pg-reason"), "a legacy close shows no reason");
  view.render(state({ history: [{ ...items[0], exit_reason_text: HOSTILE }], history_total: 1 }));
  assert.ok(s.history.textContent.includes(HOSTILE), "the reason is inserted as text");
  assert.ok(all(s.history).every((n) => n.tagName !== "img"));
});

test("the wall board shows the recorded stop and target and counts down to the 24 h limit", () => {
  const b = OV.boardModel(state({ open_plays: [play({ ...LEVELS, price_line: [
    { ts: "2026-09-29T10:10:00Z", mid: "142.00" }, { ts: "2026-09-29T10:20:00Z", mid: "142.66" },
  ] })] }), NOW, PG);
  assert.equal(b.stop, "€139.18");
  assert.equal(b.target, "€148.18");
  assert.equal(b.ringText, "23h");
  assert.equal(b.ringNote, "time left");
  assert.equal(b.dueAt, LEVELS.exit_due_at);
  assert.ok(Math.abs(b.remaining - 1410 / 1440) < 1e-9);
  assert.ok(b.line.targetY < b.line.entryY && b.line.entryY < b.line.stopY, "target above, stop below for a rise");
  assert.equal(b.line.stopY, 15, "the stop is the lowest price in the box");
  assert.equal(b.line.targetY, 3, "the target is the highest");
  const short = OV.boardModel(state({ open_plays: [play({ ...LEVELS, direction: "SHORT", stop: "145.10", target: "136.10",
    price_line: [{ mid: "142.00" }] })] }), NOW, PG);
  assert.ok(short.line.stopY < short.line.targetY, "a fall has its stop above");
  const legacy = OV.boardModel(state({ open_plays: [play({ price_line: [{ mid: "142.00" }] })] }), NOW, PG);
  assert.equal(legacy.stop, null);
  assert.equal(legacy.target, null);
  assert.equal(legacy.line.stopY, null);
  assert.equal(legacy.line.targetY, null);
  assert.equal(legacy.ringText, "30m");
  assert.equal(legacy.ringNote, "to close");
  assert.equal(OV.ringLeft(59), "59m");
  assert.equal(OV.ringLeft(60), "1h");
  assert.equal(OV.ringLeft(null), "—");
});

test("the empty board names the last close and its recorded reason", () => {
  for (const r of REASONS) {
    const b = OV.boardModel(state({ open_plays: [], history: [{ play_id: 3, asset: "SOL", ...r }], history_total: 1 }), NOW, PG);
    assert.equal(b.title, "Looking for a play");
    assert.equal(b.sub, "Last play: SOL · " + PG.formatMoney(r.net, "EUR", { signed: true }));
    assert.equal(b.note, r.exit_reason_text, "the reason gets a line of its own");
  }
  const legacy = OV.boardModel(state({ open_plays: [], history: [{ asset: "DOT", net: "0.20", exit_reason_text: null }] }), NOW, PG);
  assert.equal(legacy.sub, "Last play: DOT · +€0.20");
  assert.equal(legacy.note, null, "a legacy close has no reason line");
  const none = OV.boardModel(state({ open_plays: [], history: [] }), NOW, PG);
  assert.equal(none.sub, OV.TEXT.lookingSub);
  assert.equal(none.note, null);
  const off = OV.boardModel(state({ open_plays: [], enabled: false, history: [{ asset: "SOL", ...REASONS[0] }] }), NOW, PG);
  assert.equal(off.sub, OV.TEXT.switchedOff);
  assert.equal(off.note, null);
});

test("the office writes the last close's reason under the empty board", () => {
  const browser = fakeBrowser();
  const { host, view } = officeIn(browser, { t: NOW });
  view.update(state({ open_plays: [], history: [{ play_id: 3, asset: "SOL", ...REASONS[2] }], history_total: 1 }));
  browser.runFrame();
  let text = svgText(host);
  assert.ok(text.includes("Last play: SOL · +€0.12"));
  assert.ok(text.includes("Closed at the 24-hour limit"));
  view.update(state({ open_plays: [], history: [{ asset: "DOT", net: "0.20", exit_reason_text: null }] }));
  browser.runFrame();
  text = svgText(host);
  assert.ok(text.includes("Last play: DOT · +€0.20"));
  assert.ok(!text.includes("Closed at the 24-hour limit"), "a legacy close leaves the reason line empty");
  view.destroy();
});

test("a level value too wide for the board is squeezed to its slot, never cut or rounded", () => {
  const room = OV.LEVELS.right - OV.LEVELS.x - OV.LEVELS.valueDx;
  assert.equal(OV.levelFit("€24.236661"), null, "a usual price fits as it is");
  assert.equal(OV.levelFit(null), null);
  assert.equal(OV.levelFit("€57,933.53764285714"), room);
  const browser = fakeBrowser();
  const { host, view } = officeIn(browser, { t: NOW });
  const long = { ...LEVELS, stop: "57933.53764285714", target: "59092.1234567891", price_line: [{ mid: "58224.6576" }] };
  view.update(state({ open_plays: [play(long)] }));
  browser.runFrame();
  const squeezed = all(host).filter((n) => n.getAttribute("lengthAdjust") === "spacingAndGlyphs" &&
    /57,933|59,092/.test(n.textContent));
  assert.equal(squeezed.length, 2);
  assert.ok(squeezed.every((n) => n.getAttribute("textLength") === String(room)));
  assert.ok(svgText(host).includes("€57,933.53764285714"), "every digit is kept");
  view.update(state({ open_plays: [play({ ...LEVELS, price_line: [{ mid: "142.00" }] })] }));
  browser.runFrame();
  assert.ok(!all(host).some((n) => n.getAttribute("lengthAdjust") === "spacingAndGlyphs" && /139|148/.test(n.textContent)),
    "a value that fits again loses the squeeze");
  view.destroy();
});

test("the office draws the stop and target on the board only for a play that has them", () => {
  const browser = fakeBrowser();
  const { host, view } = officeIn(browser, { t: NOW });
  const levelNodes = () => all(host).filter((n) => n.tagName === "line" && n.getAttribute("stroke-dasharray") &&
    ["1 3", "6 3"].includes(n.getAttribute("stroke-dasharray")));
  view.update(state({ open_plays: [play({ ...LEVELS, price_line: [{ mid: "142.00" }, { mid: "142.66" }] })] }));
  browser.runFrame();
  let text = svgText(host);
  assert.ok(text.includes("€139.18") && text.includes("€148.18"), "the recorded levels are on the board");
  assert.ok(text.includes("time left"));
  assert.ok(text.includes("23h"));
  assert.deepEqual(levelNodes().map((n) => n.getAttribute("opacity")), ["1", "1"]);
  view.update(state({ open_plays: [play({ price_line: [{ mid: "142.00" }] })] }));
  browser.runFrame();
  text = svgText(host);
  assert.ok(!text.includes("€139.18") && !text.includes("€148.18"), "a legacy play shows no levels");
  assert.ok(!/stop\s*€|target\s*€/.test(text));
  assert.deepEqual(levelNodes().map((n) => n.getAttribute("opacity")), ["0", "0"]);
  assert.ok(text.includes("to close"));
  view.destroy();
});

// -- no external resources -----------------------------------------------------------------
test("the page loads no external network resources", () => {
  const html = readFileSync(path.join(WEB, "index.html"), "utf8");
  const css = readFileSync(path.join(WEB, "style.css"), "utf8");
  const js = readFileSync(path.join(WEB, "paper_game.js"), "utf8");
  assert.doesNotMatch(html, /<(link|script|img)[^>]+(href|src)=["']?(https?:)?\/\//i);
  assert.doesNotMatch(css, /@import|url\(\s*["']?(https?:)?\/\//i);
  assert.doesNotMatch(js, /https?:\/\/(?!www\.w3\.org\/2000\/svg)/);
  assert.doesNotMatch(js, /\.innerHTML\s*=|insertAdjacentHTML|document\.write/);
  assert.match(html, /id="tab-game"/);
  assert.match(html, /data-tab="game"/);
});

// -- the office's wall board (paper_office.js) -------------------------------------------------
test("the wall board shows the open play from get_paper_state() strings", () => {
  const b = OV.boardModel(state({ open_plays: [play({ price_line: [
    { ts: "2026-09-29T10:10:00Z", mid: "142.00" }, { ts: "2026-09-29T10:20:00Z", mid: "142.66" },
  ] })] }), NOW, PG);
  assert.equal(b.kind, "play");
  assert.equal(b.symbol, "SOL");
  assert.equal(b.pair, "SOL/EUR");
  assert.equal(b.arrow, "▲");
  assert.equal(b.entry, PG.formatPrice("142.18", "EUR"), "a LONG enters at the ask");
  assert.equal(b.now, PG.formatPrice("142.61", "EUR"), "and would leave at the bid");
  assert.equal(b.gain, "+€0.30");
  assert.equal(b.gainNote, "so far, before costs");
  assert.equal(b.tone, "up");
  assert.equal(b.ringKind, "running");
  assert.equal(b.ringText, "30m");
  assert.ok(Math.abs(b.remaining - 0.5) < 1e-9, "half the hour is left");
  assert.equal(b.line.points.length, 2);
  assert.ok(b.line.entryY !== null, "the entry mid is drawn as the reference line");
});

test("the wall board is green or red only when the play is actually up or down", () => {
  const tone = (gross) => OV.boardModel(state({ open_plays: [play({ gross_now: gross })] }), NOW, PG).tone;
  assert.equal(tone("0.30"), "up");
  assert.equal(tone("-0.12"), "down");
  assert.equal(tone("0.00"), "flat");
  assert.equal(tone("-0.00"), "flat");
  assert.equal(tone(null), "flat");
  const none = OV.boardModel(state({ open_plays: [play({ gross_now: null })] }), NOW, PG);
  assert.equal(none.gain, "—");
  assert.equal(none.gainNote, "no recent price");
});

test("a SHORT on the wall board enters at the bid and would leave at the ask", () => {
  const b = OV.boardModel(state({ open_plays: [play({ direction: "SHORT", direction_text: "Bets it goes down" })] }), NOW, PG);
  assert.equal(b.arrow, "▼");
  assert.equal(b.entry, PG.formatPrice("142.10", "EUR"));
  assert.equal(b.now, PG.formatPrice("142.70", "EUR"));
});

test("the wall board says it is looking for a play when none is open", () => {
  assert.equal(OV.boardModel(state({ open_plays: [] }), NOW, PG).title, "Looking for a play");
  assert.equal(OV.boardModel(state({ open_plays: [] }), NOW, PG).kind, "empty");
  const off = OV.boardModel(state({ open_plays: [], enabled: false, reason: "disabled" }), NOW, PG);
  assert.equal(off.sub, "The game is switched off.");
  assert.equal(OV.boardModel(undefined, NOW, PG).kind, "loading");
  assert.equal(OV.boardModel({ available: false, reason: "missing" }, NOW, PG).kind, "empty", "no tables yet: no play");
  assert.equal(OV.boardModel(null, NOW, PG).kind, "unavailable");
});

test("the countdown ring stops when the play waits for a price or the game is paused", () => {
  const pending = OV.boardModel(state({ open_plays: [play({ status: "PENDING_EXIT", due_at: "2026-09-29T10:20:00Z" })] }), NOW, PG);
  assert.equal(pending.ringKind, "pending");
  assert.equal(pending.ringText, "wait");
  const paused = OV.boardModel(state({ enabled: false, reason: "disabled" }), NOW, PG);
  assert.equal(paused.ringKind, "paused");
  assert.equal(paused.ringText, "paused");
  const more = OV.boardModel(state({ open_plays: [play(), play({ play_id: 2, asset: "ETH" })] }), NOW, PG);
  assert.equal(more.more, "+1 more open");
});

test("the price line ignores bad points and needs at least one real mid", () => {
  assert.equal(OV.lineGeometry(play({ price_line: [] }), 150, 26), null);
  assert.equal(OV.lineGeometry(play({ price_line: [{ mid: "abc" }, { mid: null }, null] }), 150, 26), null);
  const g = OV.lineGeometry(play({ price_line: [{ mid: "142.00" }, { mid: "x" }, { mid: "143.00" }] }), 150, 26);
  assert.equal(g.points.length, 2);
  assert.ok(g.points.every((p) => p.y >= 0 && p.y <= 26));
});

// -- the office view in a fake browser ------------------------------------------------------
function fakeBrowser({ hidden = false, reduced = false } = {}) {
  const frames = new Map();
  let frameId = 0;
  const listeners = {};
  const observers = [];
  const doc = {
    ...fakeDoc, hidden,
    addEventListener: (name, fn) => { (listeners[name] ||= new Set()).add(fn); },
    removeEventListener: (name, fn) => { if (listeners[name]) listeners[name].delete(fn); },
  };
  const win = {
    requestAnimationFrame: (fn) => { frameId += 1; frames.set(frameId, fn); return frameId; },
    cancelAnimationFrame: (id) => { frames.delete(id); },
    matchMedia: () => ({ matches: reduced, addEventListener() {}, removeEventListener() {} }),
    IntersectionObserver: class {
      constructor(fn) { this.fn = fn; this.on = true; observers.push(this); }
      observe() {}
      disconnect() { this.on = false; }
    },
  };
  return {
    doc, win, frames, listeners, observers,
    runFrame() { const [[id, fn]] = frames.entries(); frames.delete(id); fn(); },
    fire(name) { (listeners[name] || []).forEach((fn) => fn()); },
  };
}

function officeIn(browser, clock) {
  const host = new FakeElement("div");
  const view = OV.createOfficeView(host, {
    doc: browser.doc, win: browser.win, game: PG, engine: ENGINE, now: () => clock.t,
    setTimeout: () => 1, clearTimeout: () => {},
  });
  return { host, view };
}

const svgText = (host) => all(host).filter((n) => n.tagName === "text" || n.tagName === "tspan")
  .map((n) => n.textContent).join(" ");
const liveOf = (host) => all(host).find((n) => n.getAttribute("aria-live") === "polite");

test("the office puts backend strings on the board and in bubbles as text only", () => {
  const browser = fakeBrowser();
  const clock = { t: NOW };
  const { host, view } = officeIn(browser, clock);
  view.update(state({ activity: [] }));
  view.update(state({
    open_plays: [play({ asset: HOSTILE, pair: HOSTILE, direction_text: HOSTILE })],
    activity: [{ id: "alert:1", ts: "2026-09-29T10:30:00Z", agent: "scout", kind: "alert", asset: "SOL", text: HOSTILE }],
  }));
  let said = "";
  for (let i = 0; i < 120 && !said; i++) {
    clock.t += 500;
    view.frame();
    said = liveOf(host).textContent;
  }
  assert.equal(said, "Scout: " + HOSTILE, "the current bubble is announced as plain text");
  assert.ok(svgText(host).includes(HOSTILE), "the board shows the asset as text");
  assert.ok(all(host).every((n) => n.tagName !== "img"), "no markup was parsed");
  assert.equal(liveOf(host).getAttribute("tabindex"), "0", "the bubble text is keyboard reachable");
  const bubble = all(host).find((n) => String(n.className).startsWith("pg-bubble"));
  assert.equal(bubble.getAttribute("aria-hidden"), "true", "the in-scene bubble is not read twice");
  assert.equal(bubble.className, "pg-bubble on");
  view.destroy();
});

test("the Treasurer's bubble at the wallet screen sits beside him, not over the balance", () => {
  const browser = fakeBrowser();
  const clock = { t: NOW };
  const { host, view } = officeIn(browser, clock);
  view.update(state({ activity: [] }));
  view.update(state({
    activity: [{ id: "play:open:1", ts: "2026-09-29T10:30:00Z", agent: "treasurer", kind: "play_open", asset: "SOL", text: "I put money on SOL." }],
  }));
  let said = "";
  for (let i = 0; i < 120 && !said; i++) {
    clock.t += 500;
    view.frame();
    said = liveOf(host).textContent;
  }
  assert.equal(said, "Treasurer: I put money on SOL.");
  const bubble = all(host).find((n) => String(n.className).startsWith("pg-bubble"));
  assert.equal(bubble.className, "pg-bubble side on");
  view.destroy();
});

test("the office shows Looking for a play when no play is open", () => {
  const browser = fakeBrowser();
  const { host, view } = officeIn(browser, { t: NOW });
  view.update(state({ open_plays: [] }));
  browser.runFrame();
  assert.equal(view.board().kind, "empty");
  assert.ok(svgText(host).includes("Looking for a play"));
  view.destroy();
});

test("the office animates only while it is on screen and the page is visible", () => {
  const browser = fakeBrowser();
  const { view } = officeIn(browser, { t: NOW });
  assert.equal(browser.frames.size, 1, "one requestAnimationFrame loop");
  browser.runFrame();
  assert.equal(browser.frames.size, 1, "the loop keeps itself going");
  browser.doc.hidden = true;
  browser.fire("visibilitychange");
  assert.equal(browser.frames.size, 0, "a hidden page stops the loop");
  browser.doc.hidden = false;
  browser.fire("visibilitychange");
  assert.equal(browser.frames.size, 1, "showing the page resumes it");
  browser.fire("visibilitychange");
  assert.equal(browser.frames.size, 1, "and never starts a second loop");
  browser.observers[0].fn([{ isIntersecting: false }]);
  assert.equal(browser.frames.size, 0, "off screen (another tab shown) stops the loop");
  browser.observers[0].fn([{ isIntersecting: true }]);
  assert.equal(browser.frames.size, 1);
  view.destroy();
  assert.equal(browser.frames.size, 0, "destroy cancels the loop");
  assert.equal(browser.listeners.visibilitychange.size, 0, "and removes its listener");
  assert.equal(browser.observers[0].on, false, "and its observer");
});

test("a hidden page never starts the loop", () => {
  const browser = fakeBrowser({ hidden: true });
  const { view } = officeIn(browser, { t: NOW });
  assert.equal(browser.frames.size, 0);
  view.destroy();
});

test("mounting, destroying and mounting again leaves one live office", () => {
  const browser = fakeBrowser();
  const host = new FakeElement("div");
  const opts = { doc: browser.doc, win: browser.win, game: PG, engine: ENGINE, now: () => NOW,
    setTimeout: () => 1, clearTimeout: () => {} };
  const first = OV.createOfficeView(host, opts);
  first.destroy();
  assert.equal(host.childNodes.length, 0, "destroy removes the stage and the caption");
  const second = OV.createOfficeView(host, opts);
  assert.equal(host.childNodes.length, 2);
  assert.equal(browser.frames.size, 1);
  assert.equal(browser.listeners.visibilitychange.size, 1);
  second.destroy();
});

test("with reduced motion the agents do not move while idle", () => {
  const browser = fakeBrowser({ reduced: true });
  const clock = { t: NOW };
  const { host, view } = officeIn(browser, clock);
  view.update(state());
  const snapshot = () => all(host).map((n) => (n.getAttribute("transform") || "") + (n.getAttribute("opacity") || "")).join("|");
  view.frame();
  const before = snapshot();
  clock.t += 1700;
  view.frame();
  assert.equal(snapshot(), before, "no idle sway or floating Z while reduced");
  view.destroy();
});

test("a name tag moves off a character standing just below its owner", () => {
  const body = (x, y) => ({ x0: x - 12, x1: x + 12, y0: y - 56, y1: y });
  const clear = { id: "a", x: 100, y: 100, w: 68, body: body(100, 100) };
  assert.deepEqual(OV.tagOffset(clear, [clear, { id: "b", x: 300, y: 100, w: 68, body: body(300, 100) }]), [0, 0]);
  // Two agents at the table: the lower one's head is where the upper one's tag goes.
  const upper = { id: "analyst", x: 100, y: 100, w: 68, body: body(100, 100) };
  const lower = { id: "strategist", x: 112, y: 150, w: 68, body: body(112, 150) };
  const [dx, dy] = OV.tagOffset(upper, [upper, lower]);
  assert.ok(dx !== 0 || dy !== 0, "the tag moved");
  const x0 = upper.x + dx - 34;
  const y0 = upper.y + dy + 8;
  const b = lower.body;
  assert.ok(!(x0 < b.x1 && b.x0 < x0 + upper.w && y0 < b.y1 && b.y0 < y0 + 16), "and now covers nobody");
  assert.equal(OV.tagOffset(lower, [upper, lower])[1], 0, "the lower tag keeps its place");
});

// -- office geometry regressions --------------------------------------------------------------
const apart = (a, b, gap) => !(a.x0 < b.x1 + gap && b.x0 < a.x1 + gap && a.y0 < b.y1 + gap && b.y0 < a.y1 + gap);
const tagAt = (id, node, name) => {
  const n = ENGINE.LAYOUT.nodes[node];
  const [x, y] = OV.project(n.x, n.y, 0);
  return { id, x, y, w: Math.round(24 + name.length * 5.5), body: OV.bodyBox(x, y, false) };
};
function assertTagsClear(list, offsets) {
  const rects = list.map((t) => OV.tagRect(t, offsets[t.id]));
  list.forEach((t, i) => {
    list.forEach((o, j) => {
      if (i === j) return;
      assert.ok(apart(rects[i], rects[j], OV.TAG_GAP), `${t.id}'s tag keeps ${OV.TAG_GAP}px from ${o.id}'s`);
      assert.ok(apart(rects[i], o.body, 0), `${t.id}'s tag does not cover ${o.id}`);
    });
    const r = rects[i];
    assert.ok(r.x0 >= 0 && r.x1 <= OV.VIEW.width && r.y0 >= 0 && r.y1 <= OV.VIEW.height, `${t.id}'s tag stays in the picture`);
  });
}

test("the Treasurer at the wallet spot does not cover the screen's pretend-money line", () => {
  assert.equal(OV.TEXT.pretend, "pretend money");
  const label = OV.walletLabelBox();
  const spot = ENGINE.LAYOUT.nodes.wallet;
  const [x, y] = OV.project(spot.x, spot.y, 0);
  const body = OV.bodyBox(x, y, false);
  assert.ok(apart(label, body, 0), `label ${JSON.stringify(label)} vs Treasurer ${JSON.stringify(body)}`);
  // The label is still on the wallet screen: inside the screen's projected corners.
  const W = OV.WALLET;
  const corners = [[0, W.from, W.zBottom], [0, W.to, W.zBottom], [0, W.from, W.zTop], [0, W.to, W.zTop]].map((c) => OV.project(...c));
  const xs = corners.map((c) => c[0]);
  const ys = corners.map((c) => c[1]);
  assert.ok(label.x0 >= Math.min(...xs) && label.x1 <= Math.max(...xs), "the label is within the screen's width");
  assert.ok(label.y0 >= Math.min(...ys) && label.y1 <= Math.max(...ys), "and within its height");
  // He still stands by the screen, looking at the wall, reachable from his seat.
  assert.equal(spot.face, "west");
  assert.ok(x - Math.max(...xs) < 40 && y - Math.max(...ys) < 80, "the spot is next to the screen");
  assert.ok(ENGINE.LAYOUT.edges.some((e) => e.includes("wallet") && e.includes("seat_treasurer")));
});

test("two name tags that would touch are pulled apart", () => {
  const body = (x, y) => OV.bodyBox(x, y, false);
  // Side by side: the right tag's usual place starts 30px right of the left one's.
  const left = { id: "scout", x: 100, y: 200, w: 68, body: body(100, 200) };
  const right = { id: "analyst", x: 130, y: 196, w: 68, body: body(130, 196) };
  assert.ok(!apart(OV.tagRect(left, [0, 0]), OV.tagRect(right, [0, 0]), OV.TAG_GAP), "they would overlap");
  const offsets = OV.placeTagTargets([left, right]);
  assertTagsClear([left, right], offsets);
  assert.deepEqual(offsets.scout, [0, 0], "the lower tag keeps its usual place");
  // Just touching (1px apart) also counts.
  const near = { id: "boss", x: 100 + 68 + 1, y: 200, w: 68, body: body(400, 100) };
  const pair = OV.placeTagTargets([left, near]);
  assertTagsClear([left, near], pair);
});

test("four agents at the meeting table get separate tags that cover nobody", () => {
  const list = [
    tagAt("scout", "table_w", "Scout"),
    tagAt("analyst", "table_n", "Analyst"),
    tagAt("strategist", "table_e", "Strategist"),
    tagAt("boss", "table_s", "Boss"),
  ];
  const offsets = OV.placeTagTargets(list);
  assertTagsClear(list, offsets);
  // Every pair of the four, and the same when only two or three are there.
  for (let mask = 3; mask < 16; mask++) {
    const some = list.filter((_t, i) => mask & (1 << i));
    if (some.length < 2) continue;
    assertTagsClear(some, OV.placeTagTargets(some));
  }
  // With the Treasurer at the wallet and the Strategist asleep-width label too.
  const crowd = list.concat([tagAt("treasurer", "wallet", "Treasurer")]);
  assertTagsClear(crowd, OV.placeTagTargets(crowd));
});

test("name tags slide towards their place, and snap under reduced motion", () => {
  const step = OV.easeTag([0, 0], [-50, 0], false);
  assert.ok(step[0] < 0 && step[0] > -50, "one frame moves part of the way");
  let cur = [0, 0];
  for (let i = 0; i < 60; i++) cur = OV.easeTag(cur, [-50, 19], false);
  assert.deepEqual(cur, [-50, 19], "and it settles exactly on the target");
  assert.deepEqual(OV.easeTag([0, 0], [-50, 19], true), [-50, 19], "reduced motion snaps");
});

test("the board arrow follows the symbol's width and never leaves the board", () => {
  const boardW = (OV.BOARD.to - OV.BOARD.from) * OV.VIEW.unit;
  const inside = (l) => l.arrowX + OV.SYMBOL.arrowWidth <= boardW - 12;
  const clear = (l) => l.arrowX >= OV.SYMBOL.x + l.width;
  // A measured width wins over any estimate.
  const measured = OV.symbolLayout("SOL", 40);
  assert.equal(measured.arrowX, OV.SYMBOL.x + 40 + OV.SYMBOL.gap);
  assert.equal(measured.squeeze, false);
  // No measurement: an estimate, clamped for a long symbol, which is then squeezed.
  for (const symbol of ["SOL", "FARTCOIN", "SUPERLONGCOIN", "A".repeat(40)]) {
    for (const width of [undefined, 0, NaN, 999]) {
      const l = OV.symbolLayout(symbol, width);
      assert.ok(inside(l), `${symbol}/${width}: arrow at ${l.arrowX} stays on the board`);
      assert.ok(clear(l), `${symbol}/${width}: arrow clear of the symbol`);
    }
  }
  assert.equal(OV.symbolLayout("SUPERLONGCOIN").squeeze, true, "a 13-letter symbol is squeezed");
  assert.equal(OV.symbolLayout("SUPERLONGCOIN").width, OV.SYMBOL.maxWidth);
  const src = readFileSync(path.join(WEB, "paper_office.js"), "utf8");
  assert.doesNotMatch(src, /length \* 16/, "no fixed per-letter arrow offset");
});

test("the office places the board arrow from the browser's measurement of the symbol", () => {
  const browser = fakeBrowser();
  const { host, view } = officeIn(browser, { t: NOW });
  const texts = all(host).filter((n) => n.tagName === "text");
  const symbol = texts.find((n) => n.getAttribute("font-size") === String(OV.SYMBOL.size));
  const arrow = texts.find((n) => n.getAttribute("font-size") === "18");
  const bu0 = OV.BOARD.from * OV.VIEW.unit;
  const at = (n) => Number(n.getAttribute("x")) - bu0;
  symbol.getComputedTextLength = () => 52.5;
  view.update(state({ open_plays: [play({ asset: "SOL" })] }));
  view.frame();
  assert.equal(symbol.textContent, "SOL");
  assert.ok(Math.abs(at(arrow) - (OV.SYMBOL.x + 52.5 + OV.SYMBOL.gap)) < 0.11, `arrow at ${at(arrow)}`);
  // A 12-letter symbol measured wider than the board allows is squeezed to fit.
  symbol.getComputedTextLength = () => 12 * 16;
  view.update(state({ open_plays: [play({ asset: "LONGSYMBOL12" })] }));
  view.frame();
  assert.equal(symbol.getAttribute("textLength"), String(OV.SYMBOL.maxWidth));
  assert.equal(symbol.getAttribute("lengthAdjust"), "spacingAndGlyphs");
  assert.ok(at(arrow) + OV.SYMBOL.arrowWidth <= (OV.BOARD.to - OV.BOARD.from) * OV.VIEW.unit - 12);
  // Unmeasurable (hidden tab): the clamped estimate, still inside the board.
  symbol.getComputedTextLength = () => 0;
  view.update(state({ open_plays: [play({ asset: "ANOTHERLONGONE" })] }));
  view.frame();
  assert.ok(at(arrow) >= OV.SYMBOL.x + OV.SYMBOL.maxWidth);
  assert.ok(at(arrow) + OV.SYMBOL.arrowWidth <= (OV.BOARD.to - OV.BOARD.from) * OV.VIEW.unit - 12);
  view.destroy();
});

test("the Game view mounts the office and forwards readings and disconnects", () => {
  const s = slots();
  const calls = [];
  const officeView = {
    createOfficeView: (host) => ({
      update: (st) => calls.push(["update", st && st.generated_at, host.getAttribute("class")]),
      noteDisconnect: () => calls.push(["disconnect"]),
    }),
  };
  const view = PG.createView(s, { doc: fakeDoc, now: () => NOW, officeView });
  assert.equal(s.office.childNodes.length, 2, "a stage and the cast legend under it");
  view.render(state());
  assert.deepEqual(calls[0], ["update", "2026-09-29T10:30:00Z", "pg-office-stage"]);
  assert.doesNotMatch(s.office.textContent, /will appear here/, "no placeholder once the office is there");
  assert.match(s.office.textContent, /Scout/, "the cast legend is still shown");
  view.renderError();
  assert.deepEqual(calls[1], ["disconnect"]);
});

test("the office script loads no external resources and never parses markup", () => {
  const js = readFileSync(path.join(WEB, "paper_office.js"), "utf8");
  assert.doesNotMatch(js, /https?:\/\/(?!www\.w3\.org\/2000\/svg)/);
  assert.doesNotMatch(js, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function/);
  const html = readFileSync(path.join(WEB, "index.html"), "utf8");
  assert.ok(html.indexOf('src="paper_office.js"') < html.indexOf('src="app.js"'), "loaded before app.js");
  assert.ok(html.indexOf('src="paper_office_engine.js"') < html.indexOf('src="paper_office.js"'));
});
