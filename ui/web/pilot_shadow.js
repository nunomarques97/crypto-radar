/* Pilot shadow - the Game tab's panel for the second pretend account
 * (DESIGN.md "AI Game", subsection "Pilot shadow panel").
 *
 * UMD like paper_game.js, whose helpers it reuses (decimal-string money
 * formatting, clock text, the poller and its stale-response guard). The pure
 * parts and the view run identically under Node
 * (tests/ui_tests/js/test_pilot_shadow.mjs) and in the browser.
 *
 * Honesty rule: every number shown is a string Api.get_pilot_state() produced
 * (ui/pilot_reader.py); this file formats those strings but never computes an
 * amount, a limit or a quantity. The only arithmetic is the time left until a
 * recorded due time. Every backend string reaches the page via
 * createTextNode/setAttribute - never innerHTML - so a pair such as
 * "<img onerror=...>" is shown as text. The panel is read-only: the kill
 * switch and the lock reviews are operated from scripts/pilot_control.py.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory(require("./paper_game.js"));
  } else {
    root.RadarPilotShadow = factory(root.RadarPaperGame);
  }
})(typeof self !== "undefined" ? self : this, function (PG) {
  "use strict";

  var TEXT = {
    loading: "Reading the pilot shadow data…",
    unreadable: "The pilot shadow data could not be read.",
    waiting: "The pilot shadow has not started yet.",
    switchedOff: "Pilot switched off",
    noPosition: "No position open.",
    noPositionSub: "The pilot holds at most one position at a time and only buys.",
    noSizing: "No alert has reached the sizing yet.",
    noSizingSub: "An alert reaches it once the coin is priced in euros and nothing blocks entries.",
    noLocks: "No lock has tripped so far.",
    noDecisions: "No alert has been evaluated yet.",
    noRefusals: "Every alert evaluated so far led to an entry.",
    notRecorded: "not recorded yet",
    roundingNote: "Amounts are rounded to the cent for display; the pilot uses exact values.",
    valueUnknown: "Unknown until the open position has a usable price:",
  };

  // Short words for the typed reason the open position has no valuation price
  // (the backend's full sentence is shown below the amounts).
  var MARK_WORDS = {
    missing_quote: "no price yet",
    stale_quote: "price too old",
    invalid_quote: "price not usable",
  };

  // One plain-English line per NO_TRADE reason (radar_v08/domain/risk.py
  // NoTradeReason). The Node suite checks that every reason has one.
  var NO_TRADE_TEXT = {
    unsupported_direction: { label: "Bet on a fall", text: "The alert bet on a fall; the pilot only buys, betting the price goes up." },
    envelope_changed: { label: "Settings changed", text: "The settings differ from the limits recorded when the account started, so nothing new opens." },
    kill_switch_engaged: { label: "Kill switch on", text: "The kill switch was on, so no new entry could open." },
    daily_loss_lock: { label: "Daily loss lock", text: "The day's loss limit was reached; entries wait for a written review." },
    drawdown_lock: { label: "Drawdown lock", text: "The account fell too far below its best value; entries wait for a written review." },
    position_already_open: { label: "Position open", text: "A position was already open, and the pilot holds only one at a time." },
    quote_currency_mismatch: { label: "Not priced in euros", text: "The coin was priced in another currency than euros, and the pilot does not convert." },
    invalid_quote: { label: "Bad price", text: "The buy or sell price was missing or did not make sense." },
    no_valid_atr: { label: "No usual swing", text: "There was no reliable measure of the coin's usual price swing to place the stop." },
    missing_pair_rules: { label: "Exchange rules missing", text: "The exchange's rules for this coin (smallest order, size step, price step) were missing." },
    equity_unavailable: { label: "Account value unknown", text: "The account value or its free cash could not be worked out." },
    invalid_levels: { label: "Bad stop or target", text: "The stop or the target price did not make sense." },
    stop_invalid_after_tick: { label: "Stop too close", text: "After rounding to the exchange's price step, the stop was no longer below the buy price." },
    no_loss_budget: { label: "No loss budget left", text: "There was no room left in the loss budget for another entry." },
    no_notional_room: { label: "No size room left", text: "The open positions already used the whole size limit." },
    insufficient_cash: { label: "Not enough cash", text: "Too little free cash was left after keeping the reserve." },
    below_order_minimum: { label: "Below smallest order", text: "The amount the limits allow was smaller than the exchange's smallest order." },
    below_cost_minimum: { label: "Below smallest value", text: "The amount the limits allow was worth less than the exchange's smallest order value." },
    sizing_inconsistent: { label: "Size would not fit", text: "After rounding, no size fitted every limit within the allowed steps down." },
  };

  var LOCK_TEXT = {
    daily_loss: { label: "Daily loss lock", reference: "the day start" },
    drawdown: { label: "Drawdown lock", reference: "the best value" },
  };

  var CANDIDATE_TEXT = {
    per_entry_loss: "Loss per entry",
    aggregate_loss: "All open planned losses",
    notional: "Size of open positions",
    cash: "Free cash above the reserve",
  };

  // -- small formatting helpers ----------------------------------------------------------
  function str(value) {
    return typeof value === "string" && value.trim() ? value : null;
  }

  function isObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }

  function orDash(text) {
    return text === null || text === undefined ? "—" : text;
  }

  function eur(value, currency) {
    return PG.formatMoney(value, currency || "EUR");
  }

  function pct(value) {
    return PG.formatPercent(value);
  }

  // A recorded quantity with all its recorded digits, English grouping.
  function qty(value) {
    var d = PG.parseDecimal(value);
    if (!d) return null;
    return (d.sign < 0 ? "-" : "") + d.int.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (d.frac ? "." + d.frac : "");
  }

  function asInt(value) {
    return typeof value === "number" && isFinite(value) && Math.floor(value) === value ? value : null;
  }

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // "29 Sep 10:01" in local time; null when the recorded time cannot be placed.
  function whenText(value) {
    if (typeof value !== "string") return null;
    var ms = Date.parse(value);
    if (!isFinite(ms)) return null;
    var t = new Date(ms);
    return t.getDate() + " " + MONTHS[t.getMonth()] + " " + PG.clockText(value);
  }

  // The recorded detail of a refusal ("0.19<1", "USD"), or null when it is empty or
  // only a record number, which says nothing to a reader.
  function detailText(value) {
    var text = str(value);
    return text && !/^\d+$/.test(text.trim()) ? text : null;
  }

  function reasonInfo(code) {
    return Object.prototype.hasOwnProperty.call(NO_TRADE_TEXT, code) ? NO_TRADE_TEXT[code] : null;
  }

  // "At most 23 h 12 min left (until 10:00)." from the recorded due time.
  function timeLeftText(dueAt, nowMs) {
    var left = PG.minutesLeft(dueAt, nowMs);
    if (left === null) return "The time limit was not recorded.";
    if (left <= 0) return "The 24-hour limit has passed: it closes at the first valid price.";
    return "At most " + PG.durationText(left) + " left (until " + PG.clockText(dueAt) + ").";
  }

  // -- which state to show ------------------------------------------------------------------
  function selectView(state) {
    if (state === undefined) return { kind: "loading", message: TEXT.loading, disabled: false };
    if (!isObject(state)) return { kind: "unavailable", message: TEXT.unreadable, disabled: false };
    var disabled = state.enabled === false;
    if (!state.available || !isObject(state.account)) {
      return { kind: "waiting", message: str(state.reason_text) || TEXT.waiting, disabled: disabled };
    }
    return { kind: "ready", message: str(state.reason_text), disabled: disabled };
  }

  // The lock and kill switch facts an announcement compares; null without data.
  function lockState(state) {
    if (!isObject(state) || !state.available || !isObject(state.account)) return null;
    var active = isObject(state.locks) && Array.isArray(state.locks.active) ? state.locks.active : [];
    var locks = {};
    active.forEach(function (lock) {
      if (isObject(lock) && asInt(lock.lock_id) !== null) locks[lock.lock_id] = str(lock.kind);
    });
    return { kill: !!(isObject(state.kill_switch) && state.kill_switch.engaged === true), locks: locks };
  }

  function lockLabel(kind) {
    return LOCK_TEXT[kind] ? LOCK_TEXT[kind].label.toLowerCase() : "loss lock";
  }

  // One short sentence for the live region when the kill switch or a lock changed
  // between two readings with data; null otherwise (and on the first reading).
  function announcement(prev, next) {
    if (!prev || !next) return null;
    var parts = [];
    if (!prev.kill && next.kill) parts.push("the kill switch is on: no new entry until it is released");
    if (prev.kill && !next.kill) parts.push("the kill switch was released");
    Object.keys(next.locks).forEach(function (id) {
      if (!Object.prototype.hasOwnProperty.call(prev.locks, id)) {
        parts.push("the " + lockLabel(next.locks[id]) + " is on: new entries wait for a written review");
      }
    });
    Object.keys(prev.locks).forEach(function (id) {
      if (!Object.prototype.hasOwnProperty.call(next.locks, id)) {
        parts.push("the " + lockLabel(prev.locks[id]) + " was cleared by a review");
      }
    });
    return parts.length ? "Pilot shadow: " + parts.join("; ") + "." : null;
  }

  // -- DOM building (text only) -----------------------------------------------------------
  function el(doc, tag, attrs, children) {
    var node = doc.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) node.setAttribute(k, String(attrs[k]));
      });
    }
    append(doc, node, children);
    return node;
  }

  function append(doc, node, children) {
    if (children === null || children === undefined) return;
    (Array.isArray(children) ? children : [children]).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      node.appendChild(typeof c === "string" ? doc.createTextNode(c) : c);
    });
  }

  function pillNode(doc, cls, label) {
    return el(doc, "span", { "class": "pill " + cls }, [el(doc, "span", { "class": "dot", "aria-hidden": "true" }), label]);
  }

  function mono(doc, text, extra) {
    return el(doc, "span", { "class": "mono" + (extra ? " " + extra : "") }, orDash(text));
  }

  function emptyNode(doc, title, sub) {
    return el(doc, "div", { "class": "pg-empty" }, [
      el(doc, "div", { "class": "pg-empty-title" }, title),
      sub ? el(doc, "div", { "class": "pg-empty-sub" }, sub) : null,
    ]);
  }

  function block(doc, id, title, children, extra) {
    return el(doc, "section", { "class": "ps-block" + (extra ? " " + extra : ""), "aria-labelledby": id }, [
      el(doc, "h4", { "class": "ps-h", id: id }, title),
    ].concat(children));
  }

  function facts(doc, rows) {
    return el(doc, "dl", { "class": "ps-facts" }, rows.map(function (row) {
      return el(doc, "div", null, [el(doc, "dt", null, row[0]), el(doc, "dd", null, row[1])]);
    }));
  }

  function table(doc, caption, head, rows, extra) {
    return el(doc, "table", { "class": "ps-table" + (extra ? " " + extra : "") }, [
      el(doc, "caption", { "class": "ps-caption" }, caption),
      el(doc, "thead", null, el(doc, "tr", null, head.map(function (h, i) {
        return el(doc, "th", { scope: "col", "class": i ? "num" : null }, h);
      }))),
      el(doc, "tbody", null, rows),
    ]);
  }

  // -- the parts of the panel ---------------------------------------------------------------
  function stateNodes(doc, state, view) {
    var pills = [];
    if (view.disabled) pills.push(pillNode(doc, "idle", TEXT.switchedOff));
    if (view.kind === "ready") {
      if (state.kill_switch && state.kill_switch.engaged === true) pills.push(pillNode(doc, "err", "Kill switch on"));
      var active = state.locks && Array.isArray(state.locks.active) ? state.locks.active : [];
      active.forEach(function (lock) {
        pills.push(pillNode(doc, "err", LOCK_TEXT[lock.kind] ? LOCK_TEXT[lock.kind].label : "Loss lock"));
      });
      if (!pills.length) pills.push(pillNode(doc, "idle", "No lock, kill switch off"));
    }
    var note = view.kind === "ready" && view.message ? el(doc, "p", { "class": "ps-note" }, view.message) : null;
    // What kind of account this is (PAPER, not SHADOW_LIVE, not real); absent from a legacy payload.
    var mode = isObject(state) && str(state.account_text)
      ? el(doc, "p", { "class": "ps-mode" }, [str(state.account_mode) ? pillNode(doc, "info", state.account_mode) : null,
        el(doc, "span", null, state.account_text)])
      : null;
    return [mode, el(doc, "div", { "class": "ps-pills" }, pills), note];
  }

  function signedEur(value, currency) {
    return PG.formatMoney(value, currency || "EUR", { signed: true });
  }

  // An amount of the reporting valuation: the backend's string, or a dash with
  // the short reason when the backend left it null (never 0).
  function valueCell(doc, value, currency, signed, why) {
    var shown = signed ? signedEur(value, currency) : eur(value, currency);
    if (shown === null) return el(doc, "span", null, [mono(doc, "—"), el(doc, "span", { "class": "ps-missing" }, why)]);
    return mono(doc, shown, signed ? "tone-" + PG.toneOf(value) : null);
  }

  // The Account block from the reporting valuation (ui/pilot_reader.py
  // "Reporting valuation"): the open position is priced only on a fresh, usable
  // price of its own pair. The runtime equity the limits and locks use is kept,
  // labelled as such.
  function valuationAccountNodes(doc, state, v) {
    var a = isObject(state.account) ? state.account : {};
    var cur = str(v.currency) || state.currency;
    var positions = Array.isArray(v.positions) ? v.positions.filter(isObject) : [];
    var unmarked = positions.filter(function (p) { return isObject(p.mark) && p.mark.stale === true; });
    var kinds = unmarked.map(function (p) { return p.mark.status; }).filter(function (k, i, list) { return list.indexOf(k) === i; });
    var why = "unknown: " + (kinds.length === 1 && MARK_WORDS[kinds[0]] ? MARK_WORDS[kinds[0]] : "no usable price");
    var freshness = isObject(v.freshness) ? str(v.freshness.text) : null;
    var asOf = parseAt(v.as_of);
    return block(doc, "ps-account-h", "Account", [
      el(doc, "div", { "class": "ps-big mono" }, orDash(eur(v.total_equity, cur))),
      el(doc, "div", { "class": "ps-sub" }, [
        "Total value now, if the position closed · ",
        el(doc, "span", { "class": "mono" }, asOf === null ? "—" : "as of " + PG.clockText(v.as_of, true)),
        v.stale === true ? pillNode(doc, "warn", "price missing or too old") : null,
      ]),
      unmarked.length ? el(doc, "div", { "class": "ps-unknown" }, [
        TEXT.valueUnknown,
        el(doc, "ul", null, unmarked.map(function (p) {
          return el(doc, "li", null, [mono(doc, str(p.pair)), " ", orDash(str(p.mark.reason_text) || MARK_WORDS[p.mark.status] || null)]);
        })),
      ]) : null,
      facts(doc, [
        ["Assigned at the start", mono(doc, eur(v.assigned, cur))],
        ["Free cash (not in a position)", valueCell(doc, v.free_cash, cur, false, TEXT.notRecorded)],
        ["Put into the open position", valueCell(doc, v.open_cost_basis, cur, false, TEXT.notRecorded)],
        ["Open position if sold now", valueCell(doc, v.liquidation_value, cur, false, why)],
        ["Result of closed positions", valueCell(doc, v.realized_pnl, cur, true, TEXT.notRecorded)],
        ["Open position after all costs", valueCell(doc, v.open_net_pnl, cur, true, why)],
        ["Value the limits and locks use", mono(doc, eur(a.equity, cur))],
        ["Day start (UTC)", a.day_start === null || a.day_start === undefined ? el(doc, "span", { "class": "pg-dim" }, TEXT.notRecorded) : mono(doc, eur(a.day_start, cur))],
        ["Best value so far", a.high_water === null || a.high_water === undefined ? el(doc, "span", { "class": "pg-dim" }, TEXT.notRecorded) : mono(doc, eur(a.high_water, cur))],
        ["Below the best value", a.drawdown_pct === null || a.drawdown_pct === undefined ? el(doc, "span", { "class": "pg-dim" }, TEXT.notRecorded) : mono(doc, pct(a.drawdown_pct))],
      ]),
      el(doc, "p", { "class": "ps-foot" }, [str(v.valuation_basis_text), freshness ? " " + freshness : null,
        " The limits and locks value an open position at its latest recorded price, or at its entry price when none is recorded."]),
    ]);
  }

  function parseAt(value) {
    if (typeof value !== "string") return null;
    var ms = Date.parse(value);
    return isFinite(ms) ? ms : null;
  }

  function accountNodes(doc, state) {
    if (isObject(state.valuation)) return valuationAccountNodes(doc, state, state.valuation);
    // A payload without the reporting valuation (older reader) renders as before.
    var a = state.account;
    var cur = state.currency;
    var change = eur(a.change, cur) === null ? null : PG.formatMoney(a.change, cur || "EUR", { signed: true });
    return block(doc, "ps-account-h", "Account", [
      el(doc, "div", { "class": "ps-big mono" }, orDash(eur(a.equity, cur))),
      el(doc, "div", { "class": "ps-sub" }, [
        "Equity now · ",
        el(doc, "span", { "class": "mono tone-" + PG.toneOf(a.change) }, orDash(change)),
        " since the start",
      ]),
      facts(doc, [
        ["Assigned at the start", mono(doc, eur(a.assigned, cur))],
        ["Closed results", mono(doc, eur(a.realized, cur) && PG.formatMoney(a.realized, cur || "EUR", { signed: true }))],
        ["Cash not in a position", mono(doc, eur(a.cash, cur))],
        ["Day start (UTC)", a.day_start === null ? el(doc, "span", { "class": "pg-dim" }, TEXT.notRecorded) : mono(doc, eur(a.day_start, cur))],
        ["Best value so far", a.high_water === null ? el(doc, "span", { "class": "pg-dim" }, TEXT.notRecorded) : mono(doc, eur(a.high_water, cur))],
        ["Below the best value", a.drawdown_pct === null ? el(doc, "span", { "class": "pg-dim" }, TEXT.notRecorded) : mono(doc, pct(a.drawdown_pct))],
      ]),
    ]);
  }

  function limitRule(limit) {
    var p = pct(limit.pct);
    switch (limit.id) {
      case "per_entry_loss":
        return "at most " + orDash(p) + " of equity" + (str(limit.cap) ? ", capped at " + eur(limit.cap) : "");
      case "aggregate_loss": return "at most " + orDash(p) + " of equity";
      case "gross_notional": return "at most " + orDash(p) + " of equity";
      case "cash_buffer": return "at least " + orDash(p) + " of equity stays free";
      case "daily_loss": return "locks " + orDash(p) + " below the day start (UTC)";
      case "drawdown": return "locks " + orDash(p) + " below the best value";
      case "positions": return "at most " + orDash(asInt(limit.max)) + " at a time";
      case "leverage": return asInt(limit.max) === 0 ? "none: only its own cash" : "recorded as " + orDash(asInt(limit.max));
      default: return "—";
    }
  }

  var LIMIT_LABELS = {
    per_entry_loss: "Planned loss per entry",
    aggregate_loss: "All open planned losses",
    gross_notional: "Size of open positions",
    cash_buffer: "Cash reserve",
    daily_loss: "Daily loss lock",
    drawdown: "Drawdown lock",
    positions: "Positions at once",
    leverage: "Borrowed money",
  };

  function limitsNodes(doc, state) {
    var cur = state.currency;
    var rows = (Array.isArray(state.limits) ? state.limits : []).filter(function (l) {
      return isObject(l) && LIMIT_LABELS[l.id];
    }).map(function (l) {
      var counted = l.id === "positions" || l.id === "leverage";
      return el(doc, "tr", null, [
        el(doc, "th", { scope: "row" }, [LIMIT_LABELS[l.id], el(doc, "span", { "class": "ps-rule" }, limitRule(l))]),
        el(doc, "td", { "class": "num" }, counted ? mono(doc, "—", "pg-dim") : mono(doc, eur(l.amount, cur))),
        el(doc, "td", { "class": "num" }, l.id === "positions" ? mono(doc, asInt(l.open) === null ? null : String(l.open))
          : l.id === "leverage" ? mono(doc, "—", "pg-dim") : mono(doc, eur(l.used, cur))),
        el(doc, "td", { "class": "num" }, counted ? mono(doc, "—", "pg-dim")
          : mono(doc, eur(l.left, cur), PG.signOf(l.left) < 0 ? "tone-down" : null)),
      ]);
    });
    return block(doc, "ps-limits-h", "Limits", [
      table(doc, "Each limit at the current equity: its amount, what the open position uses and what is left.",
        ["Limit", "Amount", "In use", "Left"], rows),
      el(doc, "p", { "class": "ps-foot" }, TEXT.roundingNote),
    ], "ps-limits");
  }

  function positionNodes(doc, state, refs, nowMs) {
    var p = state.open_position;
    if (!isObject(p)) {
      refs.timeLeft = null;
      return block(doc, "ps-position-h", "Open position", [emptyNode(doc, TEXT.noPosition, TEXT.noPositionSub)]);
    }
    var quote = str(p.quote);
    var timeLeft = el(doc, "p", { "class": "ps-time" }, timeLeftText(p.due_at, nowMs));
    refs.timeLeft = { node: timeLeft, due: p.due_at };
    return block(doc, "ps-position-h", "Open position", [
      el(doc, "div", { "class": "ps-coin" }, [
        el(doc, "span", { "class": "ps-sym" }, orDash(str(p.asset))),
        el(doc, "span", { "class": "ps-pair mono" }, orDash(str(p.pair))),
        pillNode(doc, "info", "Bought, betting it goes up"),
      ]),
      facts(doc, [
        ["Quantity", mono(doc, qty(p.quantity))],
        ["Bought at", mono(doc, PG.formatPrice(p.entry, quote))],
        ["Stop (loss limit)", mono(doc, PG.formatPrice(p.stop, quote))],
        ["Target (profit goal)", mono(doc, PG.formatPrice(p.target, quote))],
        ["Size", mono(doc, eur(p.notional, state.currency))],
        ["Planned worst loss", mono(doc, eur(p.planned_loss, state.currency))],
      ]),
      timeLeft,
      // The fee stored on the position (ASSUMED, account tier unverified); absent from a legacy payload.
      str(p.fee_text) ? el(doc, "p", { "class": "ps-muted ps-fee" }, p.fee_text) : null,
    ]);
  }

  function checkWord(ok) {
    return ok === true ? "passes" : ok === false ? "fails" : "not checked";
  }

  function sizingNodes(doc, state) {
    var s = state.last_sizing;
    if (!isObject(s)) return block(doc, "ps-sizing-h", "Last sizing", [emptyNode(doc, TEXT.noSizing, TEXT.noSizingSub)], "ps-sizing");
    var cur = state.currency;
    var quote = str(s.quote);
    var info = reasonInfo(s.reason);
    var outcome = s.outcome === "OPENED" ? "opened" : "no trade: " + (info ? info.text : orDash(str(s.reason)));
    var rows = (Array.isArray(s.candidates) ? s.candidates : []).filter(isObject).map(function (c) {
      return el(doc, "tr", { "class": c.binding === true ? "ps-binding" : null }, [
        el(doc, "th", { scope: "row" }, [
          orDash(CANDIDATE_TEXT[c.id]),
          c.binding === true ? el(doc, "span", { "class": "ps-chip" }, "sets the size") : null,
        ]),
        el(doc, "td", { "class": "num" }, mono(doc, eur(c.budget, cur))),
        el(doc, "td", { "class": "num" }, mono(doc, qty(c.quantity))),
      ]);
    });
    var m = isObject(s.minimums) ? s.minimums : {};
    var lotDecimals = asInt(s.lot_decimals);
    var passes = asInt(s.passes);
    return block(doc, "ps-sizing-h", "Last sizing", [
      el(doc, "p", { "class": "ps-line" }, [
        el(doc, "b", null, orDash(str(s.asset))), " ", mono(doc, str(s.pair), "pg-dim"),
        " at " + orDash(whenText(s.decided_at)) + ": " + outcome,
      ]),
      el(doc, "p", { "class": "ps-line ps-muted" }, [
        "Buy price ", mono(doc, PG.formatPrice(s.ask, quote)),
        " · stop ", mono(doc, PG.formatPrice(s.stop, quote)),
        " · worst loss per unit ", mono(doc, qty(s.loss_per_unit)),
        " · equity ", mono(doc, eur(s.equity, cur)),
      ]),
      table(doc, "The four quantities each limit allows; the smallest sets the size.",
        ["Limit", "Budget", "Quantity it allows"], rows),
      facts(doc, [
        ["Rounded down to the lot" + (lotDecimals === null ? "" : " (" + lotDecimals + " decimals)"), mono(doc, qty(s.lot_quantity))],
        ["Steps down to fit", mono(doc, passes === null ? null : String(passes))],
        ["Final quantity", mono(doc, qty(s.quantity))],
        ["Smallest order " + orDash(qty(m.ordermin)), el(doc, "span", { "class": "ps-check ps-" + checkWord(m.order_ok).replace(" ", "-") },
          [mono(doc, qty(m.checked_quantity)), " " + checkWord(m.order_ok)])],
        ["Smallest value " + orDash(PG.formatPrice(m.costmin, quote)), el(doc, "span", { "class": "ps-check ps-" + checkWord(m.cost_ok).replace(" ", "-") },
          [mono(doc, eur(m.cost, quote)), " " + checkWord(m.cost_ok)])],
        ["Planned worst loss", mono(doc, eur(s.planned_loss, cur))],
        ["Size and fees", el(doc, "span", null, [mono(doc, eur(s.notional, cur)), " + ", mono(doc, eur(s.entry_fee, cur)), " + ",
          mono(doc, eur(s.exit_fee, cur))])],
      ]),
    ], "ps-sizing");
  }

  function lockItem(doc, lock, cur) {
    var info = LOCK_TEXT[lock.kind] || { label: "Loss lock", reference: "its reference" };
    var review = isObject(lock.review) ? lock.review : null;
    return el(doc, "li", null, [
      el(doc, "div", { "class": "ps-lock-head" }, [
        el(doc, "b", null, info.label),
        lock.active === true ? pillNode(doc, "err", "locked") : pillNode(doc, "idle", "cleared"),
      ]),
      el(doc, "div", null, [
        "Tripped " + orDash(whenText(lock.tripped_at)) + ": equity ", mono(doc, eur(lock.equity, cur)),
        " against " + info.reference + " ", mono(doc, eur(lock.reference, cur)),
        " (limit ", mono(doc, pct(lock.limit_pct)), ").",
      ]),
      review ? el(doc, "div", { "class": "ps-muted" }, [
        "Cleared " + orDash(whenText(review.reviewed_at)) + " by ", el(doc, "b", null, orDash(str(review.reviewer))),
        ": " + orDash(str(review.cause)) + ". Reference reset to ", mono(doc, eur(review.rebase_equity, cur)), ".",
      ]) : el(doc, "div", { "class": "ps-muted" }, "Stays on until a written review clears it (scripts/pilot_control.py review-lock)."),
    ]);
  }

  function safetyNodes(doc, state) {
    var cur = state.currency;
    var k = isObject(state.kill_switch) ? state.kill_switch : {};
    var killLine;
    if (k.engaged === true) {
      killLine = el(doc, "p", { "class": "ps-line" }, [pillNode(doc, "err", "on"), " since " + orDash(whenText(k.recorded_at)) + ": ",
        orDash(str(k.reason)), el(doc, "span", { "class": "ps-muted" }, " (" + orDash(str(k.actor)) + ")"),
        el(doc, "span", { "class": "ps-muted ps-block-line" }, "No new entry opens; an open position still closes by its stop, target or time limit.")]);
    } else if (str(k.recorded_at)) {
      killLine = el(doc, "p", { "class": "ps-line" }, [pillNode(doc, "idle", "off"), " released " + orDash(whenText(k.recorded_at)) + ": ",
        orDash(str(k.reason))]);
    } else {
      killLine = el(doc, "p", { "class": "ps-line" }, [pillNode(doc, "idle", "off"), " never used"]);
    }
    var recent = state.locks && Array.isArray(state.locks.recent) ? state.locks.recent.filter(isObject) : [];
    return block(doc, "ps-safety-h", "Locks and kill switch", [
      el(doc, "h5", { "class": "ps-h5" }, "Kill switch"),
      killLine,
      el(doc, "h5", { "class": "ps-h5" }, "Loss locks"),
      recent.length ? el(doc, "ul", { "class": "ps-locks" }, recent.map(function (lock) { return lockItem(doc, lock, cur); }))
        : el(doc, "p", { "class": "ps-muted" }, TEXT.noLocks),
    ]);
  }

  function refusalNodes(doc, state) {
    var nt = isObject(state.no_trade) ? state.no_trade : {};
    var total = asInt(nt.total);
    var decisions = asInt(state.decisions_total);
    var counts = (Array.isArray(nt.counts) ? nt.counts : []).filter(function (c) {
      return isObject(c) && asInt(c.count) !== null && c.count > 0;
    }).slice().sort(function (a, b) { return b.count - a.count; });
    var summary;
    if (decisions === 0) summary = el(doc, "p", { "class": "ps-muted" }, TEXT.noDecisions);
    else if (total === 0) summary = el(doc, "p", { "class": "ps-muted" }, TEXT.noRefusals);
    else summary = el(doc, "p", { "class": "ps-line" }, [mono(doc, total === null ? null : String(total)), " of ",
      mono(doc, decisions === null ? null : String(decisions)), " alerts evaluated did not trade:"]);
    var nodes = [summary];
    if (counts.length) {
      nodes.push(table(doc, "Why the pilot did not trade, by reason.", ["Reason", "Times"], counts.map(function (c) {
        var info = reasonInfo(c.reason);
        return el(doc, "tr", null, [
          el(doc, "th", { scope: "row" }, [info ? info.label : orDash(str(c.reason)),
            el(doc, "span", { "class": "ps-rule" }, info ? info.text : "")]),
          el(doc, "td", { "class": "num" }, mono(doc, String(c.count))),
        ]);
      }), "ps-reasons"));
    }
    var recent = Array.isArray(state.recent) ? state.recent.filter(isObject) : [];
    if (recent.length) {
      nodes.push(table(doc, "Most recent decisions, newest first.", ["Time", "Coin", "Result"], recent.map(function (d) {
        var info = reasonInfo(d.reason);
        var result = d.outcome === "OPENED"
          ? el(doc, "span", { "class": "tone-up" }, ["Opened ", mono(doc, qty(d.quantity))])
          : el(doc, "span", null, [info ? info.label : orDash(str(d.reason)),
            detailText(d.detail) ? el(doc, "span", { "class": "mono ps-detail" }, detailText(d.detail)) : null]);
        return el(doc, "tr", null, [
          el(doc, "td", null, mono(doc, whenText(d.decided_at), "pg-dim")),
          el(doc, "th", { scope: "row" }, [orDash(str(d.asset)), " ", mono(doc, str(d.pair), "pg-dim")]),
          el(doc, "td", { "class": "num" }, result),
        ]);
      }), "ps-recent"));
    }
    return block(doc, "ps-refusals-h", "Why it did not trade", nodes, "ps-refusals");
  }

  function bodyNodes(doc, state, view, refs, nowMs) {
    if (view.kind !== "ready") {
      refs.timeLeft = null;
      return [emptyNode(doc, view.message, view.kind === "waiting"
        ? "It starts with the radar; nothing here is estimated before it records its account." : null)];
    }
    // Two independent columns (the facts about the account on the left, the tables on
    // the right), read in document order; one column in a narrow window.
    return [el(doc, "div", { "class": "ps-grid" }, [
      el(doc, "div", { "class": "ps-col" }, [
        accountNodes(doc, state),
        positionNodes(doc, state, refs, nowMs),
        safetyNodes(doc, state),
      ]),
      el(doc, "div", { "class": "ps-col" }, [
        limitsNodes(doc, state),
        sizingNodes(doc, state),
        refusalNodes(doc, state),
      ]),
    ])];
  }

  function signature(value) {
    try { return JSON.stringify(value); } catch (e) { return String(Math.random()); }
  }

  // -- the panel ---------------------------------------------------------------------------------
  // slots: {status, body, refresh, live}
  function createView(slots, opts) {
    var doc = (opts && opts.doc) || (typeof document !== "undefined" ? document : null);
    var now = (opts && opts.now) || function () { return Date.now(); };
    var sigs = {};
    var refs = { timeLeft: null };
    var hasGood = false;
    var shownAt = null;
    var lastAppliedAt = null;
    var lastLocks = null;

    function put(name, build, sig) {
      var slot = slots[name];
      if (!slot) return;
      if (sig !== undefined && sigs[name] === sig) return;
      sigs[name] = sig;
      while (slot.firstChild) slot.removeChild(slot.firstChild);
      append(doc, slot, build());
    }

    function tick() {
      if (refs.timeLeft && refs.timeLeft.node) refs.timeLeft.node.textContent = timeLeftText(refs.timeLeft.due, now());
    }

    function show(state, view) {
      put("status", function () { return stateNodes(doc, state, view); }, signature(["s", view, state && state.kill_switch, state && state.locks,
        state && state.account_mode, state && state.account_text]));
      put("body", function () { return bodyNodes(doc, state, view, refs, now()); }, signature(["b", view, view.kind === "ready" ? state : null]));
      tick();
    }

    // Applies one reading. A reading older than the one on screen is dropped
    // (returns false), so an out-of-order response never replaces a newer one.
    function render(state) {
      var at = isObject(state) && typeof state.generated_at === "string" ? Date.parse(state.generated_at) : NaN;
      if (isFinite(at) && shownAt !== null && at < shownAt) return false;
      if (isFinite(at)) shownAt = at;
      var view = selectView(state);
      show(state, view);
      var locks = lockState(state);
      if (locks) {
        var said = announcement(lastLocks, locks);
        if (said && slots.live) slots.live.textContent = said;
        lastLocks = locks;
      }
      if (slots.refresh) slots.refresh.textContent = "";
      if (view.kind !== "loading") {
        hasGood = view.kind !== "unavailable";
        lastAppliedAt = isObject(state) ? state.generated_at : null;
      }
      return true;
    }

    return {
      render: render,
      // A failed poll: keep the last good reading on screen with a visible note;
      // with nothing good yet, show the unreadable state.
      renderError: function () {
        if (!hasGood) {
          show(null, selectView(null));
          return;
        }
        if (slots.refresh) {
          slots.refresh.textContent = "Could not refresh the pilot shadow; showing the reading from " +
            PG.clockText(lastAppliedAt, true) + ".";
        }
      },
      tick: tick,
    };
  }

  return {
    TEXT: TEXT,
    NO_TRADE_TEXT: NO_TRADE_TEXT,
    LOCK_TEXT: LOCK_TEXT,
    LIMIT_LABELS: LIMIT_LABELS,
    selectView: selectView,
    lockState: lockState,
    announcement: announcement,
    timeLeftText: timeLeftText,
    limitRule: limitRule,
    whenText: whenText,
    createView: createView,
    createPoller: PG.createPoller,
  };
});
