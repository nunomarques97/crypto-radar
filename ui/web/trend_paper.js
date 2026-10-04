/* Trend paper (research) - the Game tab's read-only panel for the trend paper
 * books (DESIGN.md "AI Game", subsection "Trend paper panel").
 *
 * UMD like pilot_shadow.js, reusing paper_game.js (decimal-string parsing, clock
 * text, the poller and its stale-response guard). The pure parts, the view and
 * the catch-up control run identically under Node
 * (tests/ui_tests/js/test_trend_paper.mjs) and in the browser.
 *
 * Honesty rule: every figure shown is a string Api.get_trend_paper_state()
 * produced (ui/trend_reader.py); this file formats those strings but never
 * computes an amount, a return or an exposure. A value the backend left null
 * reads "—", never 0. Gains and losses take --ok/--err only from the sign of
 * the backend's string. Every backend string reaches the page through
 * createTextNode/setAttribute - never innerHTML.
 *
 * The only control is "Catch up now": it calls Api.trend_paper_catch_up() (the
 * existing paper catch-up, public market data only), is inert while a request is
 * pending, announces the result once in a polite live region without moving
 * focus, and is never called while TEST MODE is on. Nothing here trades.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory(require("./paper_game.js"));
  } else {
    root.RadarTrendPaper = factory(root.RadarPaperGame);
  }
})(typeof self !== "undefined" ? self : this, function (PG) {
  "use strict";

  var TEXT = {
    honesty: "Paper only — research, not qualified, no real orders",
    loading: "Reading the trend paper ledger…",
    unreadable: "The trend paper ledger could not be read.",
    empty: "No paper day has been booked yet.",
    refused: "The trend paper ledger was refused; it is never rewritten.",
    refusedSub: "No figure is shown from a refused ledger.",
    refusedPill: "Ledger refused",
    exposureNote: "Exposure is the share of the book held after the last daily fill; the target is what the " +
      "strategy asked for from the previous day's close. Each strategy is compared with buy-and-hold of the " +
      "same coins in the same currency and at the same fee.",
    catchingUp: "Catching up from public market data…",
    catchUpFailed: "Catch-up: the request failed before an answer came back. The panel shows what the ledger holds.",
    catchUpUnknown: "Catch-up: the answer was not understood. The panel shows what the ledger holds.",
    catchUpBlocked: "Catch-up is off in TEST MODE.",
  };

  // One short label per catch-up outcome (ui/trend_reader.py CatchUpOutcome) and
  // the sentence used when the backend sent no detail. The Node suite checks that
  // every outcome has one.
  var CATCH_UP_TEXT = {
    BOOKED: { label: "Caught up", fallback: "Missed paper days were booked.", ok: true },
    UP_TO_DATE: { label: "Up to date", fallback: "Every due paper day is already booked.", ok: true },
    BEFORE_START: { label: "Before the start", fallback: "There is nothing to book before the first paper day.", ok: true },
    BUSY: { label: "Busy", fallback: "The radar's own catch-up holds the ledger; nothing was written.", ok: false },
    MARKET_FAILED: { label: "Market data failed", fallback: "Public market data could not be read.", ok: false },
    WAITING_FOR_DATA: {
      label: "Waiting for data",
      fallback: "The public candles do not cover every due paper day yet; nothing was written.",
      ok: false,
    },
    LEDGER_REFUSED: { label: "Ledger refused", fallback: "The ledger was refused; it is never rewritten.", ok: false },
    IN_PROGRESS: { label: "Already running", fallback: "A catch-up is already running.", ok: false },
  };

  var STATES = { ok: true, empty: true, refused: true, unavailable: true };

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

  function asInt(value) {
    return typeof value === "number" && isFinite(value) && Math.floor(value) === value ? value : null;
  }

  // A backend amount with English grouping and every recorded digit, without the
  // currency (the table heading names it): "7000.00" -> "7,000.00".
  function amount(value) {
    var d = PG.parseDecimal(value);
    if (!d) return null;
    return (d.sign < 0 ? "-" : "") + d.int.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (d.frac ? "." + d.frac : "");
  }

  function signedPercent(value) {
    return PG.formatPercent(value, { signed: true });
  }

  // A difference of two returns in percentage points: "-0.81" -> "-0.81 pp".
  function points(value) {
    var text = PG.formatPercent(value, { signed: true });
    return text === null ? null : text.slice(0, -1) + " pp";
  }

  // A recorded UTC day ("2026-10-17"), shown as recorded; null when it is not one.
  function day(value) {
    return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null;
  }

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // "17 Oct 10:00" in local time; null when the recorded time cannot be placed.
  function whenText(value) {
    if (typeof value !== "string") return null;
    var ms = Date.parse(value);
    if (!isFinite(ms)) return null;
    var t = new Date(ms);
    return t.getDate() + " " + MONTHS[t.getMonth()] + " " + PG.clockText(value);
  }

  // -- which state to show ------------------------------------------------------------------
  function selectView(state) {
    if (state === undefined) return { kind: "loading", message: TEXT.loading };
    if (!isObject(state) || !STATES[state.state]) return { kind: "unavailable", message: TEXT.unreadable };
    if (state.state === "ok" && !Array.isArray(state.quotes)) return { kind: "unavailable", message: TEXT.unreadable };
    var fallback = { ok: null, empty: TEXT.empty, refused: TEXT.refused, unavailable: TEXT.unreadable }[state.state];
    return { kind: state.state, message: str(state.reason) || fallback };
  }

  // The sentence for one catch-up answer and whether it reads as a success.
  function catchUpMessage(result) {
    if (!isObject(result) || !Object.prototype.hasOwnProperty.call(CATCH_UP_TEXT, result.status)) {
      return { text: TEXT.catchUpUnknown, ok: false };
    }
    var info = CATCH_UP_TEXT[result.status];
    return { text: "Catch-up: " + info.label + ". " + (str(result.detail) || info.fallback), ok: info.ok };
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

  // -- the parts of the panel ---------------------------------------------------------------
  // Paper days with no record because a public candle is missing for good: one calm line,
  // only when the backend counts at least one. Each day with the backend's own reason.
  function skippedNode(doc, state) {
    var count = asInt(state.days_skipped);
    if (count === null || count < 1) return null;
    var list = (Array.isArray(state.skipped_days) ? state.skipped_days : []).filter(isObject);
    var items = [];
    list.forEach(function (s, i) {
      items.push(i ? "; " : ": ", mono(doc, day(s.day)), " — ", orDash(str(s.reason)));
    });
    return el(doc, "p", { "class": "tp-summary tp-dim tp-skipped" }, [
      mono(doc, String(count)), count === 1 ? " paper day skipped" : " paper days skipped",
      " (a public candle is missing for good; no record, the books carry over unchanged)",
    ].concat(items, ["."]));
  }

  function summaryNodes(doc, state, view) {
    if (view.kind === "empty") return [skippedNode(doc, state)].filter(Boolean);
    if (view.kind !== "ok") return [];
    var days = asInt(state.days_booked);
    var recorded = whenText(state.last_record_ts);
    return [el(doc, "p", { "class": "tp-summary" }, [
      mono(doc, days === null ? null : String(days)), days === 1 ? " paper day booked, " : " paper days booked, ",
      "from ", mono(doc, day(state.first_day)), " to ", mono(doc, day(state.last_day)), ".",
      recorded ? el(doc, "span", { "class": "tp-dim" }, [" Last record written ", mono(doc, recorded), "."]) : null,
    ]), skippedNode(doc, state)].filter(Boolean);
  }

  // Exposure after the last fill and its target, one line per asset of the book.
  function exposureCell(doc, assets) {
    var list = (Array.isArray(assets) ? assets : []).filter(isObject);
    if (!list.length) return mono(doc, null);
    return el(doc, "span", { "class": "tp-exposure" }, list.map(function (a) {
      return el(doc, "span", { "class": "tp-asset" }, [
        el(doc, "span", { "class": "tp-sym" }, orDash(str(a.asset))), " ",
        mono(doc, PG.formatPercent(a.exposure_pct)),
        el(doc, "span", { "class": "tp-target" }, ["target ", mono(doc, PG.formatPercent(a.target_pct))]),
      ]);
    }));
  }

  function versusCell(doc, row) {
    var comparator = isObject(row.comparator) ? row.comparator : {};
    return el(doc, "span", null, [
      mono(doc, points(row.vs_buy_hold_pp), "tone-" + PG.toneOf(row.vs_buy_hold_pp)),
      el(doc, "span", { "class": "tp-sub" }, ["buy-and-hold ", mono(doc, signedPercent(comparator.return_pct))]),
    ]);
  }

  // The equity after the last booked day, and that day under it.
  function equityCell(doc, row) {
    var last = day(row.last_date);
    return el(doc, "span", null, [
      mono(doc, amount(row.equity)),
      el(doc, "span", { "class": "tp-sub" }, last ? ["as of ", mono(doc, last)] : "last day —"),
    ]);
  }

  // One row per fee scenario; the fee is the row's header.
  function bookRow(doc, row) {
    var trades = asInt(row.trades);
    return el(doc, "tr", null, [
      el(doc, "th", { scope: "row", "class": "tp-fee" }, mono(doc, PG.formatPercent(row.fee_pct))),
      el(doc, "td", { "class": "num" }, equityCell(doc, row)),
      el(doc, "td", { "class": "num" }, mono(doc, signedPercent(row.return_pct), "tone-" + PG.toneOf(row.return_pct))),
      el(doc, "td", { "class": "num" }, versusCell(doc, row)),
      el(doc, "td", { "class": "num" }, mono(doc, PG.formatPercent(row.max_drawdown_pct))),
      el(doc, "td", { "class": "num" }, mono(doc, trades === null ? null : String(trades))),
      el(doc, "td", { "class": "num" }, mono(doc, amount(row.fees))),
      el(doc, "td", { "class": "num" }, exposureCell(doc, row.assets)),
    ]);
  }

  // Plain words for the buy-and-hold comparator a strategy is measured against.
  var COMPARATOR_TEXT = {
    BH_5050: "buy-and-hold of BTC and ETH, half each",
    BH_BTC: "buy-and-hold of BTC",
  };

  // What a strategy holds (from its recorded assets) and what it is compared with.
  function strategyNote(row) {
    var coins = (Array.isArray(row.assets) ? row.assets : []).filter(isObject)
      .map(function (a) { return str(a.asset); }).filter(Boolean);
    var comparator = isObject(row.comparator) ? str(row.comparator.rule) : null;
    var words = comparator && Object.prototype.hasOwnProperty.call(COMPARATOR_TEXT, comparator)
      ? COMPARATOR_TEXT[comparator] : comparator;
    var parts = [];
    if (coins.length) parts.push("Holds " + coins.join(" and "));
    if (words) parts.push("compared with " + words);
    return parts.length ? parts.join("; ") + "." : null;
  }

  // Consecutive rows of the same strategy form one row group (one row per fee scenario).
  function groups(rows) {
    var out = [];
    rows.forEach(function (row) {
      var label = str(row.label) || "—";
      var last = out[out.length - 1];
      if (last && last.label === label) last.rows.push(row);
      else out.push({ label: label, rows: [row] });
    });
    return out;
  }

  var COLUMNS = 8;

  function quoteBlock(doc, state, group, index) {
    var currency = str(group.currency) || str(group.quote) || "—";
    var rows = (Array.isArray(group.rows) ? group.rows : []).filter(isObject);
    var start = day(state.paper_start);
    var id = "tp-q" + index + "-h";
    var head = ["Fee per leg", "Equity (" + currency + ")", "Return since " + orDash(start), "vs buy-and-hold",
      "Max drawdown", "Trades", "Fees (" + currency + ")", "Exposure (target)"];
    return el(doc, "section", { "class": "ps-block tp-book", "aria-labelledby": id }, [
      el(doc, "h4", { "class": "ps-h", id: id }, currency + " books"),
      el(doc, "table", { "class": "ps-table tp-table" }, [
        el(doc, "caption", { "class": "ps-caption" }, [
          "Each book started with ", mono(doc, amount(state.capital)), " " + currency + " on ", mono(doc, start),
          ". Amounts are in " + currency + ".",
        ]),
        el(doc, "thead", null, el(doc, "tr", null, head.map(function (h, i) {
          return el(doc, "th", { scope: "col", "class": i ? "num" : null }, h);
        }))),
      ].concat(groups(rows).map(function (g) {
        var note = strategyNote(g.rows[0]);
        return el(doc, "tbody", null, [
          el(doc, "tr", { "class": "tp-group" }, el(doc, "th", { scope: "rowgroup", colspan: COLUMNS }, [
            el(doc, "span", { "class": "tp-name" }, g.label),
            note ? el(doc, "span", { "class": "tp-desc" }, note) : null,
          ])),
        ].concat(g.rows.map(function (row) { return bookRow(doc, row); })));
      }))),
    ]);
  }

  function bodyNodes(doc, state, view) {
    if (view.kind === "ok") {
      var quotes = state.quotes.filter(isObject);
      if (!quotes.length) return [emptyNode(doc, TEXT.unreadable, null)];
      return quotes.map(function (q, i) { return quoteBlock(doc, state, q, i); })
        .concat([el(doc, "p", { "class": "ps-foot" }, TEXT.exposureNote)]);
    }
    if (view.kind === "empty") {
      var capital = amount(state.capital);
      return [el(doc, "div", { "class": "pg-empty" }, [
        el(doc, "div", { "class": "pg-empty-title" }, view.message),
        el(doc, "div", { "class": "pg-empty-sub" }, ["Each book starts with ", capital ? mono(doc, capital) : "the same capital",
          " in its currency (EUR or USDT). Nothing is shown before a day is booked."]),
      ])];
    }
    if (view.kind === "refused") {
      var error = isObject(state.error) ? state.error : {};
      var code = str(error.code);
      var detail = str(error.detail);
      return [
        el(doc, "div", { "class": "ps-pills" }, [pillNode(doc, "err", TEXT.refusedPill)]),
        el(doc, "div", { "class": "pg-empty" }, [
          el(doc, "div", { "class": "pg-empty-title" }, view.message),
          code ? el(doc, "div", { "class": "pg-empty-sub" }, [mono(doc, code), detail ? ": " + detail : null]) : null,
          el(doc, "div", { "class": "pg-empty-sub" }, TEXT.refusedSub),
        ]),
      ];
    }
    return [emptyNode(doc, view.message, null)];
  }

  function signature(value) {
    try { return JSON.stringify(value); } catch (e) { return String(Math.random()); }
  }

  // Everything that is drawn, without the time of the reading (a poll that changes
  // nothing leaves the DOM alone).
  function drawn(state) {
    if (!isObject(state)) return state;
    var copy = {};
    Object.keys(state).forEach(function (k) { if (k !== "generated_at") copy[k] = state[k]; });
    return copy;
  }

  // -- the panel ---------------------------------------------------------------------------------
  // slots: {summary, body, refresh}
  function createView(slots, opts) {
    var doc = (opts && opts.doc) || (typeof document !== "undefined" ? document : null);
    var sigs = {};
    var hasGood = false;
    var shownAt = null;
    var lastAppliedAt = null;

    function put(name, build, sig) {
      var slot = slots[name];
      if (!slot) return;
      if (sigs[name] === sig) return;
      sigs[name] = sig;
      while (slot.firstChild) slot.removeChild(slot.firstChild);
      append(doc, slot, build());
    }

    function show(state, view) {
      var sig = signature([view, drawn(state)]);
      put("summary", function () { return summaryNodes(doc, state, view); }, sig);
      put("body", function () { return bodyNodes(doc, state, view); }, sig);
    }

    // Applies one reading. A reading older than one already applied is dropped
    // (returns false), so an out-of-order response never replaces a newer one.
    function render(state) {
      var at = isObject(state) && typeof state.generated_at === "string" ? Date.parse(state.generated_at) : NaN;
      if (isFinite(at) && shownAt !== null && at < shownAt) return false;
      if (isFinite(at)) shownAt = at;
      var view = selectView(state);
      show(state, view);
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
          slots.refresh.textContent = "Could not refresh the trend paper; showing the reading from " +
            PG.clockText(lastAppliedAt, true) + ".";
        }
      },
    };
  }

  // -- the single control: "Catch up now" ----------------------------------------------------
  // opts: {button, result, live, call, allowed, onDone}. The button stays focusable
  // and is marked aria-disabled while a request is pending (a disabled attribute
  // would drop keyboard focus); activating it then does nothing. call() is never
  // made while allowed() is false (TEST MODE).
  function createCatchUp(opts) {
    var button = opts.button;
    var pending = false;
    var token = 0;

    function setPending(on) {
      pending = on;
      if (button) {
        button.setAttribute("aria-disabled", on ? "true" : "false");
        button.setAttribute("aria-busy", on ? "true" : "false");
      }
    }

    function say(message, ok) {
      if (opts.result) {
        opts.result.textContent = message;
        opts.result.setAttribute("class", "tp-result" + (ok ? "" : " tp-warn"));
      }
    }

    function finish(mine, message) {
      if (mine !== token) return;
      setPending(false);
      say(message.text, message.ok);
      if (opts.live) opts.live.textContent = message.text; // announced once, focus untouched
      if (opts.onDone) {
        try { opts.onDone(); } catch (e) { /* a refresh failure is shown by the poller */ }
      }
    }

    function run() {
      if (pending) return null;
      if (opts.allowed && !opts.allowed()) {
        say(TEXT.catchUpBlocked, false);
        return null;
      }
      token += 1;
      var mine = token;
      setPending(true);
      say(TEXT.catchingUp, true);
      var request;
      try {
        request = Promise.resolve(opts.call());
      } catch (err) {
        request = Promise.reject(err);
      }
      return request.then(function (result) {
        finish(mine, catchUpMessage(result));
      }, function () {
        finish(mine, { text: TEXT.catchUpFailed, ok: false });
      });
    }

    setPending(false);
    if (button && typeof button.addEventListener === "function") {
      button.addEventListener("click", function () { run(); });
    }
    return {
      run: run,
      isPending: function () { return pending; },
    };
  }

  return {
    TEXT: TEXT,
    CATCH_UP_TEXT: CATCH_UP_TEXT,
    amount: amount,
    points: points,
    signedPercent: signedPercent,
    whenText: whenText,
    selectView: selectView,
    catchUpMessage: catchUpMessage,
    createView: createView,
    createCatchUp: createCatchUp,
    createPoller: PG.createPoller,
  };
});
