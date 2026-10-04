// Regression tests for the Pilot shadow panel (ui/web/pilot_shadow.js): state
// transitions, stale and out-of-order readings, one live announcement per lock or
// kill switch change, text-only insertion and English copy for every NO_TRADE
// reason. Same hand-rolled DOM stub as test_paper_game.mjs.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const PS = require("../../../ui/web/pilot_shadow.js");
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
    this.attributes = {}; this.parentNode = null;
  }
  appendChild(child) { child.parentNode = this; this.childNodes.push(child); return child; }
  removeChild(child) { this.childNodes = this.childNodes.filter((c) => c !== child); child.parentNode = null; return child; }
  get firstChild() { return this.childNodes[0] || null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  focus() { focusCalls += 1; }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(value) { this.childNodes = [new FakeText(value)]; }
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

function slots() {
  return { status: new FakeElement("div"), body: new FakeElement("div"), refresh: new FakeElement("div"), live: new FakeElement("div") };
}

function view(s, now = NOW) {
  return PS.createView(s, { doc: fakeDoc, now: () => now });
}

const HOSTILE = "<img src=x onerror=alert(1)>";
const NOW = Date.parse("2026-09-29T10:01:00Z");

// A reading as ui/pilot_reader.py builds it (tests/ui_tests/test_pilot_reader.py
// checks the same numbers against the real store).
function reading(overrides) {
  return {
    enabled: true, available: true, reason: null, reason_text: null, pretend_money: true, direction: "LONG", currency: "EUR",
    account: {
      assigned: "240.00", equity: "239.88", change: "-0.12", realized: "0.00", cash: "220.95", day_start: "240.00",
      day_change: "-0.12", high_water: "240.00", drawdown_pct: "0.05", recorded_at: "2026-09-29T10:00:00.000000+00:00",
      envelope_policy_id: "ex1_envelope_v1", envelope_sha256: "x",
    },
    limits: [
      { id: "per_entry_loss", pct: "0.25", amount: "0.60", used: "0.57", left: null, cap: null },
      { id: "aggregate_loss", pct: "0.50", amount: "1.20", used: "0.57", left: "0.63" },
      { id: "gross_notional", pct: "10", amount: "23.99", used: "19.00", left: "4.99" },
      { id: "cash_buffer", pct: "10", amount: "23.99", used: "19.05", left: "196.96" },
      { id: "daily_loss", pct: "1", amount: "2.40", used: "0.12", left: "2.28" },
      { id: "drawdown", pct: "3", amount: "7.20", used: "0.12", left: "7.08" },
      { id: "positions", max: 1, open: 1 },
      { id: "leverage", max: 0 },
    ],
    kill_switch: { engaged: false, reason: null, actor: null, recorded_at: null },
    locks: { active: [], recent: [], total: 0 },
    open_position: {
      position_id: 1, event_id: "e1", asset: "BTC", pair: "BTC/EUR", quote: "EUR", direction: "LONG", quantity: "0.19",
      entry: "100", entry_bid: "99.9", stop: "98", target: "104", stress_exit: "97.510", notional: "19.00",
      cost_basis: "19.05", planned_loss: "0.57", fee_bps: "26", exit_policy: "ex1_initial_paper_v1",
      opened_at: "2026-09-29T10:00:00.000000+00:00", due_at: "2026-09-30T10:00:00.000000+00:00",
    },
    open_count: 1,
    last_sizing: {
      decision_id: 1, event_id: "e1", asset: "BTC", pair: "BTC/EUR", quote: "EUR", outcome: "OPENED", reason: null, detail: "",
      decided_at: "2026-09-29T10:00:00.000000+00:00", equity: "240.00", ask: "100", stop: "98", stress_exit: "97.510",
      loss_per_unit: "3.00352600", binding: "per_entry_loss",
      candidates: [
        { id: "per_entry_loss", quantity: "0.19976520", budget: "0.60", binding: true },
        { id: "aggregate_loss", quantity: "0.39953041", budget: "1.20", binding: false },
        { id: "notional", quantity: "0.24000000", budget: "24.00", binding: false },
        { id: "cash", quantity: "2.15439856", budget: "216.00", binding: false },
      ],
      lot_decimals: 2, lot_quantity: "0.19", passes: 0, quantity: "0.19", notional: "19.00", entry_fee: "0.05", exit_fee: "0.05",
      planned_loss: "0.57",
      minimums: { checked_quantity: "0.19", ordermin: "0.01", order_ok: true, cost: "19.00", costmin: "0.50", cost_ok: true },
    },
    no_trade: { total: 2, counts: [
      { reason: "unsupported_direction", count: 1 }, { reason: "kill_switch_engaged", count: 0 },
      { reason: "position_already_open", count: 1 },
    ] },
    decisions_total: 3, opened_total: 1,
    recent: [
      { decision_id: 2, event_id: "e2", asset: "BTC", pair: "BTC/USD", quote: "USD", outcome: "NO_TRADE",
        reason: "position_already_open", detail: "1", decided_at: "2026-09-29T10:00:00.000000+00:00", quantity: null },
      { decision_id: 1, event_id: "e1", asset: "BTC", pair: "BTC/EUR", quote: "EUR", outcome: "OPENED", reason: null,
        detail: "", decided_at: "2026-09-29T10:00:00.000000+00:00", quantity: "0.19" },
    ],
    generated_at: "2026-09-29T10:01:00.000000+00:00",
    ...overrides,
  };
}

function unavailable(reason, text, at) {
  return {
    enabled: true, available: false, reason, reason_text: text, pretend_money: true, direction: "LONG", currency: null,
    account: null, limits: [], kill_switch: null, locks: { active: [], recent: [], total: 0 }, open_position: null,
    open_count: 0, last_sizing: null, no_trade: { total: 0, counts: [] }, decisions_total: 0, opened_total: 0, recent: [],
    generated_at: at,
  };
}

const DAILY = {
  lock_id: 1, kind: "daily_loss", active: true, equity: "237.44", reference: "240.00", limit_pct: "1", utc_day: "2026-09-29",
  evaluated_on: "settle", tripped_at: "2026-09-29T23:00:05.000000+00:00", review: null,
};

function at(minute) {
  return `2026-09-29T10:${String(minute).padStart(2, "0")}:00.000000+00:00`;
}

// -- copy -----------------------------------------------------------------------------
test("every NO_TRADE reason of the Risk Engine has a plain English label and line", () => {
  const source = readFileSync(path.join(ROOT, "radar_v08", "domain", "risk.py"), "utf8");
  const block = source.split("class NoTradeReason(Enum):")[1].split("\nclass ")[0];
  const values = [...block.matchAll(/^\s+[A-Z_]+ = "([a-z_]+)"$/gm)].map((m) => m[1]);
  assert.equal(values.length, 19);
  assert.deepEqual(Object.keys(PS.NO_TRADE_TEXT).sort(), values.slice().sort());
  for (const code of values) {
    const info = PS.NO_TRADE_TEXT[code];
    assert.match(info.label, /^[A-Z][A-Za-z ,'-]+$/, code);
    assert.match(info.text, /^[A-Z][^\n]+\.$/, code);
    assert.ok(!/[ãçéêõáíóú]/i.test(info.label + info.text), code);
  }
});

test("each limit states its rule in words from the recorded percentage", () => {
  const rules = reading().limits.map((l) => PS.limitRule(l));
  assert.deepEqual(rules, [
    "at most 0.25% of equity", "at most 0.50% of equity", "at most 10% of equity", "at least 10% of equity stays free",
    "locks 1% below the day start (UTC)", "locks 3% below the best value", "at most 1 at a time", "none: only its own cash",
  ]);
  assert.equal(PS.limitRule({ id: "per_entry_loss", pct: "0.25", cap: "0.40" }), "at most 0.25% of equity, capped at €0.40");
});

// -- which state is shown -----------------------------------------------------------------
test("selectView: loading, unreadable, waiting, switched off and ready", () => {
  assert.equal(PS.selectView(undefined).kind, "loading");
  assert.equal(PS.selectView(null).kind, "unavailable");
  assert.equal(PS.selectView("nonsense").kind, "unavailable");
  const waiting = PS.selectView(unavailable("no_tables", "The pilot shadow has not started yet.", at(0)));
  assert.deepEqual([waiting.kind, waiting.message], ["waiting", "The pilot shadow has not started yet."]);
  const off = PS.selectView(reading({ enabled: false, reason: "disabled", reason_text: "Switched off." }));
  assert.deepEqual([off.kind, off.disabled, off.message], ["ready", true, "Switched off."]);
  assert.equal(PS.selectView(reading()).kind, "ready");
});

test("empty -> filled -> unavailable -> filled renders each state from scratch", () => {
  const s = slots();
  const v = view(s);
  v.render(undefined);
  assert.match(s.body.textContent, /Reading the pilot shadow data/);
  v.render(unavailable("no_database", "No radar data yet.", at(0)));
  assert.match(s.body.textContent, /No radar data yet\./);
  assert.equal(s.status.textContent, "");
  v.render(reading({ generated_at: at(1) }));
  const text = s.body.textContent;
  for (const part of ["€239.88", "-€0.12", "€240.00", "0.05%", "€0.60", "€196.96", "€7.08", "BTC/EUR", "0.19", "€98",
    "€104", "0.19976520", "2.15439856", "sets the size", "passes", "Why it did not trade", "Bet on a fall",
    "A position was already open"]) {
    assert.ok(text.includes(part), part);
  }
  assert.match(s.status.textContent, /No lock, kill switch off/);
  v.render(unavailable("unreadable", "The pilot shadow data could not be read.", at(2)));
  assert.match(s.body.textContent, /could not be read/);
  assert.ok(!s.body.textContent.includes("€239.88"));
  v.render(reading({ generated_at: at(3) }));
  assert.ok(s.body.textContent.includes("€239.88"));
});

test("values not recorded show as not recorded, never as zero", () => {
  const s = slots();
  const r = reading();
  r.account = { ...r.account, day_start: null, day_change: null, high_water: null, drawdown_pct: null };
  r.limits = r.limits.map((l) => (l.id === "daily_loss" ? { ...l, amount: null, used: null, left: null } : l));
  r.open_position = null;
  r.last_sizing = null;
  view(s).render(r);
  const text = s.body.textContent;
  assert.equal(text.split("not recorded yet").length - 1, 3);
  assert.match(text, /No position open\./);
  assert.match(text, /No alert has reached the sizing yet\./);
  const daily = all(s.body).find((n) => n.tagName === "tr" && n.textContent.startsWith("Daily loss lock"));
  assert.ok(!daily.textContent.includes("€0.00"));
  assert.equal(daily.textContent.split("—").length - 1, 3);
});

// -- stale and out-of-order readings ---------------------------------------------------
test("a failed poll keeps the last good reading with a visible stale note", () => {
  const s = slots();
  const v = view(s);
  v.render(reading({ generated_at: "2026-09-29T10:01:00Z" }));
  const before = s.body.textContent;
  v.renderError();
  assert.equal(s.body.textContent, before);
  assert.match(s.refresh.textContent, /^Could not refresh the pilot shadow; showing the reading from \d\d:\d\d:\d\d\.$/);
  v.render(reading({ generated_at: "2026-09-29T10:01:05Z" }));
  assert.equal(s.refresh.textContent, "");
});

test("a failed first poll shows the unreadable state; a later success replaces it", () => {
  const s = slots();
  const v = view(s);
  v.render(undefined);
  v.renderError();
  assert.match(s.body.textContent, /could not be read/);
  assert.equal(s.refresh.textContent, "");
  v.renderError();  // retry fails again: still unreadable, no crash
  assert.match(s.body.textContent, /could not be read/);
  v.render(reading());
  assert.ok(s.body.textContent.includes("€239.88"));
});

test("an older reading arriving after a newer one is ignored", () => {
  const s = slots();
  const v = view(s);
  assert.equal(v.render(reading({ generated_at: at(5), account: { ...reading().account, equity: "241.00" } })), true);
  assert.equal(v.render(reading({ generated_at: at(4) })), false);
  assert.ok(s.body.textContent.includes("€241.00"));
  assert.ok(!s.body.textContent.includes("€239.88"));
});

function fakeTimers() {
  const pending = new Map();
  let id = 0;
  return {
    setTimeout: (fn, ms) => { id += 1; pending.set(id, { fn, ms }); return id; },
    clearTimeout: (t) => { pending.delete(t); },
    pending,
    fire() { const entries = [...pending.values()]; pending.clear(); entries.forEach((t) => t.fn()); },
  };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

test("the poller drops a response that settles after a newer one and survives stop/start/stop/start", async () => {
  const timers = fakeTimers();
  const resolvers = [];
  const s = slots();
  const v = view(s);
  const poller = PS.createPoller({
    intervalMs: 5000, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    fetchState: () => new Promise((resolve) => resolvers.push(resolve)),
    onState: (state) => v.render(state), onError: () => v.renderError(),
  });
  poller.start();
  poller.stop();
  poller.start();
  assert.equal(resolvers.length, 2);
  resolvers[1](reading({ generated_at: at(2), account: { ...reading().account, equity: "250.00" } }));
  await flush();
  resolvers[0](reading({ generated_at: at(1) }));
  await flush();
  assert.ok(s.body.textContent.includes("€250.00"));
  assert.equal(timers.pending.size, 1, "one timer, never two");
  poller.stop();
  assert.equal(timers.pending.size, 0);
  poller.start();
  poller.stop();
  poller.start();
  assert.equal(timers.pending.size, 0, "a request is in flight, no timer yet");
  resolvers[resolvers.length - 1](reading({ generated_at: at(3) }));
  await flush();
  assert.equal(timers.pending.size, 1);
  poller.stop();
});

// -- live announcements ---------------------------------------------------------------------
test("lock and kill switch changes are announced once, without moving focus", () => {
  focusCalls = 0;
  const s = slots();
  const v = view(s);
  v.render(reading({ generated_at: at(1) }));
  assert.equal(s.live.textContent, "", "the first reading is not announced");
  const locked = reading({
    generated_at: at(2), locks: { active: [DAILY], recent: [DAILY], total: 1 },
    kill_switch: { engaged: true, reason: "gap", actor: "operator", recorded_at: at(2) },
  });
  v.render(locked);
  assert.equal(s.live.textContent,
    "Pilot shadow: the kill switch is on: no new entry until it is released; the daily loss lock is on: new entries wait for a written review.");
  s.live.textContent = "";
  v.render({ ...locked, generated_at: at(3) });
  v.render({ ...locked, generated_at: at(4) });
  assert.equal(s.live.textContent, "", "an unchanged state is not announced again");
  v.renderError();
  assert.equal(s.live.textContent, "", "a failed poll announces nothing");
  const reviewed = { ...DAILY, active: false, review: { review_id: 1, reviewer: "operator", cause: "gap", rebase_equity: "237.44",
    reviewed_at: at(5) } };
  v.render(reading({ generated_at: at(5), locks: { active: [], recent: [reviewed], total: 1 } }));
  assert.equal(s.live.textContent,
    "Pilot shadow: the kill switch was released; the daily loss lock was cleared by a review.");
  assert.match(s.status.textContent, /No lock, kill switch off/);
  assert.equal(focusCalls, 0);
});

test("an unavailable reading between two with data neither announces nor forgets the lock state", () => {
  const s = slots();
  const v = view(s);
  const locked = reading({ generated_at: at(1), locks: { active: [DAILY], recent: [DAILY], total: 1 } });
  v.render(locked);
  v.render(unavailable("unreadable", "The pilot shadow data could not be read.", at(2)));
  assert.equal(s.live.textContent, "");
  v.render({ ...locked, generated_at: at(3) });
  assert.equal(s.live.textContent, "", "the same lock after a gap is not new");
});

test("locked view shows pills, the lock facts and how it is cleared", () => {
  const s = slots();
  view(s).render(reading({
    locks: { active: [DAILY], recent: [DAILY], total: 1 },
    kill_switch: { engaged: true, reason: "gap on BTC", actor: "operator", recorded_at: at(2) },
  }));
  assert.match(s.status.textContent, /Kill switch on/);
  assert.match(s.status.textContent, /Daily loss lock/);
  const text = s.body.textContent;
  assert.match(text, /equity €237\.44 against the day start €240\.00 \(limit 1%\)/);
  assert.match(text, /Stays on until a written review clears it/);
  assert.match(text, /gap on BTC/);
  assert.match(text, /No new entry opens; an open position still closes/);
});

test("a refusal's recorded detail is shown unless it is only a record number; a negative room is marked", () => {
  const s = slots();
  const r = reading();
  r.recent = [
    { ...r.recent[0], event_id: "a", detail: "1" },
    { ...r.recent[0], event_id: "b", reason: "below_order_minimum", detail: "0.19<1" },
  ];
  r.limits = r.limits.map((l) => (l.id === "gross_notional" ? { ...l, left: "-0.01" } : l));
  view(s).render(r);
  const details = all(s.body).filter((n) => (n.getAttribute("class") || "").includes("ps-detail")).map((n) => n.textContent);
  assert.deepEqual(details, ["0.19<1"]);
  const notional = all(s.body).find((n) => n.tagName === "tr" && n.textContent.startsWith("Size of open positions"));
  const left = all(notional).filter((n) => n.tagName === "span" && n.textContent === "-€0.01");
  assert.equal(left.length, 1);
  assert.match(left[0].getAttribute("class"), /tone-down/);
});

// -- the reporting valuation, fee provenance and account kind ----------------
const ACCOUNT_TEXT = "A separate pretend EUR account (PAPER). It is not SHADOW_LIVE and not a real exchange account: "
  + "no real balance is read or shown.";
const BASIS = "Open positions are valued as if closed now at the price you could actually sell at "
  + "(or buy back at, for a bet on a fall), after the assumed commission on both the buy and the sell.";
const FRESH = "A position is valued only on a valid price of its own pair, recorded since it opened and at most 10 minutes old.";
const FEE = "Assumed commission of 0.26% on the buy and again on the sell (ASSUMED, account tier unverified).";
const TOO_OLD = "The last price of this pair is too old to value it now.";
const MARKED = { status: "marked", stale: false, reason_text: null, bid: "99.9", ask: "100.1", ts: at(0), age_seconds: 60 };
const STALE_MARK = { status: "stale_quote", stale: true, reason_text: TOO_OLD, bid: null, ask: null, ts: at(0), age_seconds: 900 };

// ui/pilot_reader.py's valuation for reading(): 0.19 at bid 99.9 is 18.981, less the 0.05
// exit fee = 18.931; total 239.88 = free cash 220.95 + 18.93 = realized balance 240.00 - 0.12.
function pilotValuation(overrides) {
  return {
    account_mode: "PAPER", account_text: ACCOUNT_TEXT, currency: "EUR", as_of: at(1),
    valuation_basis: "conservative_liquidation", valuation_basis_text: BASIS,
    identity: "total_equity = free_cash + liquidation_value = realized_balance + open_net_pnl",
    freshness: { max_age_seconds: 600, text: FRESH }, mark_status: "all_marked", stale: false, unmarked: [], open_count: 1,
    assigned: "240.00", free_cash: "220.95", open_cost_basis: "19.05", liquidation_value: "18.93", realized_balance: "240.00",
    realized_pnl: "0.00", open_net_pnl: "-0.12", total_equity: "239.88",
    positions: [{ position_id: 1, pair: "BTC/EUR", quantity: "0.19", cost_basis: "19.05", mark: MARKED, exit_fee: "0.05",
      liquidation_value: "18.93", open_net_pnl: "-0.12", fee_bps: "26", fee_text: FEE }],
    exact: {},
    ...overrides,
  };
}

function valuedReading(overrides) {
  const base = reading();
  return reading({
    account_mode: "PAPER", account_text: ACCOUNT_TEXT, valuation: pilotValuation(),
    open_position: { ...base.open_position, fee_source: "stored_position_fee_bps", account_tier_verified: false, fee_text: FEE },
    ...overrides,
  });
}

function staleReading() {
  const v = pilotValuation();
  return valuedReading({ valuation: pilotValuation({ mark_status: "unavailable", stale: true,
    unmarked: [{ position_id: 1, pair: "BTC/EUR", status: "stale_quote" }],
    liquidation_value: null, open_net_pnl: null, total_equity: null,
    positions: [{ ...v.positions[0], mark: STALE_MARK, exit_fee: null, liquidation_value: null, open_net_pnl: null }] }) });
}

// The Account block's facts: label -> text of the amount cell.
function accountFacts(slot) {
  const section = all(slot).find((n) => n.tagName === "section" && n.getAttribute("aria-labelledby") === "ps-account-h");
  const rows = {};
  all(section).filter((n) => n.tagName === "dt").forEach((dt) => {
    rows[dt.textContent] = dt.parentNode.childNodes.find((c) => c.tagName === "dd").textContent;
  });
  return { section, rows };
}

test("the pilot says it is a separate pretend EUR PAPER account, not SHADOW_LIVE or real", () => {
  const s = slots();
  view(s).render(valuedReading());
  assert.ok(s.status.textContent.includes("PAPER"));
  assert.ok(s.status.textContent.includes(ACCOUNT_TEXT));
  assert.match(s.status.textContent, /not SHADOW_LIVE and not a real exchange account/);
});

test("the Account block shows the reporting valuation, its basis and the runtime value, labelled", () => {
  const s = slots();
  view(s).render(valuedReading());
  const { section, rows } = accountFacts(s.body);
  assert.deepEqual(rows, {
    "Assigned at the start": "€240.00",
    "Free cash (not in a position)": "€220.95",
    "Put into the open position": "€19.05",
    "Open position if sold now": "€18.93",
    "Result of closed positions": "€0.00",
    "Open position after all costs": "-€0.12",
    "Value the limits and locks use": "€239.88",
    "Day start (UTC)": "€240.00",
    "Best value so far": "€240.00",
    "Below the best value": "0.05%",
  });
  assert.equal(all(section).find((n) => n.getAttribute("class") === "ps-big mono").textContent, "€239.88");
  assert.match(section.textContent, /Total value now, if the position closed · as of \d\d:\d\d:\d\d/);
  assert.ok(section.textContent.includes(BASIS));
  assert.ok(section.textContent.includes(FRESH));
  assert.doesNotMatch(section.textContent, /unknown|price missing/);
  assert.ok(s.body.textContent.includes(FEE), "the open position shows its stored fee as ASSUMED");
});

test("a stale pilot mark leaves the dependent totals as a dash with the reason, never 0", () => {
  const s = slots();
  view(s).render(staleReading());
  const { section, rows } = accountFacts(s.body);
  assert.equal(all(section).find((n) => n.getAttribute("class") === "ps-big mono").textContent, "—");
  assert.equal(rows["Open position if sold now"], "—unknown: price too old");
  assert.equal(rows["Open position after all costs"], "—unknown: price too old");
  assert.equal(rows["Free cash (not in a position)"], "€220.95", "known cash stays available");
  assert.equal(rows["Put into the open position"], "€19.05");
  assert.equal(rows["Result of closed positions"], "€0.00", "a recorded zero is still shown");
  assert.match(section.textContent, /price missing or too old/);
  assert.ok(section.textContent.includes("BTC/EUR " + TOO_OLD));
});

test("the pilot with nothing open values the account at its free cash", () => {
  const s = slots();
  view(s).render(valuedReading({ open_position: null, open_count: 0, valuation: pilotValuation({
    mark_status: "no_open_positions", open_count: 0, positions: [], free_cash: "240.00", open_cost_basis: "0.00",
    liquidation_value: "0.00", open_net_pnl: "0.00", total_equity: "240.00" }) }));
  const { section, rows } = accountFacts(s.body);
  assert.equal(all(section).find((n) => n.getAttribute("class") === "ps-big mono").textContent, "€240.00");
  assert.equal(rows["Open position if sold now"], "€0.00");
  assert.doesNotMatch(section.textContent, /unknown|price missing/);
  assert.match(s.body.textContent, /No position open\./);
});

test("a legacy pilot payload without the valuation renders the Account block as before", () => {
  const s = slots();
  view(s).render(reading());
  assert.doesNotMatch(s.status.textContent, /PAPER|SHADOW_LIVE/);
  const { section } = accountFacts(s.body);
  assert.doesNotMatch(section.textContent, /Free cash|if sold now|Value the limits/);
  assert.ok(section.textContent.includes("€239.88"));
  assert.doesNotMatch(s.body.textContent, /ASSUMED/);
});

test("valuation updates keep the stale-note, out-of-order and announcement behaviour", () => {
  focusCalls = 0;
  const s = slots();
  const v = view(s);
  v.render(valuedReading({ generated_at: at(1) }));
  v.renderError();
  assert.match(s.refresh.textContent, /^Could not refresh the pilot shadow/);
  assert.equal(accountFacts(s.body).rows["Open position if sold now"], "€18.93", "the last reading stays");
  assert.equal(v.render({ ...staleReading(), generated_at: at(0) }), false, "an older reading is ignored");
  assert.equal(accountFacts(s.body).rows["Open position if sold now"], "€18.93");
  v.render({ ...staleReading(), generated_at: at(2) });
  assert.equal(s.refresh.textContent, "");
  assert.equal(accountFacts(s.body).rows["Open position if sold now"], "—unknown: price too old");
  assert.equal(s.live.textContent, "", "a price turning stale is not a lock change");
  assert.equal(focusCalls, 0);
});

// -- text only ---------------------------------------------------------------------------
test("hostile backend strings are inserted as text only", () => {
  const s = slots();
  const r = reading();
  r.open_position = { ...r.open_position, pair: HOSTILE, asset: HOSTILE };
  r.last_sizing = { ...r.last_sizing, pair: HOSTILE, reason: HOSTILE, outcome: "NO_TRADE" };
  r.recent = [{ ...r.recent[0], pair: HOSTILE, detail: HOSTILE, reason: HOSTILE }];
  r.no_trade = { total: 1, counts: [{ reason: HOSTILE, count: 1 }] };
  r.kill_switch = { engaged: true, reason: HOSTILE, actor: HOSTILE, recorded_at: at(1) };
  r.locks = { active: [], recent: [{ ...DAILY, active: false, review: { review_id: 1, reviewer: HOSTILE, cause: HOSTILE,
    rebase_equity: "237.44", reviewed_at: at(1) } }], total: 1 };
  view(s).render(r);
  const nodes = all(s.body).concat(all(s.status));
  assert.ok(!nodes.some((n) => n.tagName === "img" || n.tagName === "script"));
  assert.ok(nodes.every((n) => Object.keys(n.attributes).every((k) => !k.startsWith("on"))));
  assert.ok(s.body.textContent.split(HOSTILE).length - 1 >= 8);
});

test("the stale note and the live region are text too", () => {
  const s = slots();
  const v = view(s);
  v.render(reading({ generated_at: HOSTILE }));
  v.renderError();
  assert.ok(s.refresh.childNodes.every((c) => c.nodeType === 3));
});

// -- structure for the keyboard and screen readers ---------------------------------------------
test("every block is a labelled section with a heading; tables have captions and header scopes", () => {
  const s = slots();
  view(s).render(reading());
  const nodes = all(s.body);
  const sections = nodes.filter((n) => n.tagName === "section");
  assert.deepEqual(sections.map((n) => n.getAttribute("aria-labelledby")),
    ["ps-account-h", "ps-position-h", "ps-safety-h", "ps-limits-h", "ps-sizing-h", "ps-refusals-h"]);
  const headings = nodes.filter((n) => n.tagName === "h4");
  assert.deepEqual(headings.map((n) => n.textContent),
    ["Account", "Open position", "Locks and kill switch", "Limits", "Last sizing", "Why it did not trade"]);
  sections.forEach((sec, i) => assert.equal(sec.getAttribute("aria-labelledby"), headings[i].getAttribute("id")));
  const tables = nodes.filter((n) => n.tagName === "table");
  assert.equal(tables.length, 4);
  for (const t of tables) {
    assert.equal(t.childNodes[0].tagName, "caption");
    const ths = all(t).filter((n) => n.tagName === "th");
    assert.ok(ths.every((th) => th.getAttribute("scope") === "col" || th.getAttribute("scope") === "row"));
  }
  assert.ok(nodes.every((n) => n.getAttribute("tabindex") === null), "nothing is made focusable");
});

test("the time left follows the recorded due time", () => {
  assert.equal(PS.timeLeftText("2026-09-30T10:00:00Z", Date.parse("2026-09-29T10:01:00Z")),
    `At most 23 h 59 min left (until ${PS.whenText("2026-09-30T10:00:00Z").split(" ").pop()}).`);
  assert.match(PS.timeLeftText("2026-09-29T10:00:00Z", Date.parse("2026-09-29T10:01:00Z")), /has passed/);
  assert.equal(PS.timeLeftText(null, NOW), "The time limit was not recorded.");
});
