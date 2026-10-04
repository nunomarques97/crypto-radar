/* AI Game (paper game) - the Game tab's wallet, current play, history and
 * glossary (DESIGN.md "AI Game (paper game) - scoped direction").
 *
 * UMD like agent_room.js: the pure helpers (money formatting of the bridge's
 * decimal strings, chart geometry, time-bar fraction, empty-state selection,
 * the stale-response guard and the poller) run identically under Node
 * (tests/ui_tests/js/test_paper_game.mjs) and in the browser.
 *
 * Honesty rule: every number shown is a string Api.get_paper_state() produced
 * (ui/paper_reader.py); this file formats those strings but never computes a
 * balance, a gain or a percentage. Every backend string reaches the page via
 * createTextNode/textContent - never innerHTML - so an asset name such as
 * "<img onerror=...>" is shown as text.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.RadarPaperGame = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";

  var TEXT = {
    loading: "Reading the game data…",
    unreadable: "The game data could not be read.",
    noPlays: "No plays yet.",
    looking: "Looking for a play",
    lookingSub: "A play opens when the radar raises a new alert that says which way the price should go.",
    switchedOff: "The game is switched off: the radar opens no new plays.",
    paused: "Paused: the game is switched off, so this play closes only after it is switched back on.",
    pretend: "Pretend money",
    officeSoon: "The animated office will appear here. Until then, this is who works in it:",
    valueTitle: "Value now, if every open play closed",
    valueUnknown: "Unknown until every open play has a usable price:",
  };

  // The wallet valuation (wallet.valuation, ui/paper_reader.py): each amount with
  // its plain-English label, in reading order.
  var VALUE_ROWS = [
    { key: "total_equity", label: "Total value now", lead: true },
    { key: "free_cash", label: "Free cash (not in a play)" },
    { key: "open_cost_basis", label: "Put into open plays" },
    { key: "liquidation_value", label: "Open plays if closed now" },
    { key: "realized_pnl", label: "Result of closed plays", signed: true },
    { key: "open_net_pnl", label: "Open plays after all costs", signed: true },
  ];

  // Short words for the typed reason an open play has no valuation price; the
  // backend's full sentence is shown next to the pair.
  var MARK_WORDS = {
    missing_quote: "no price yet",
    stale_quote: "price too old",
    invalid_quote: "price not usable",
  };

  // What each agent does, in plain words (DESIGN.md: one agent per real
  // pipeline role). The Strategist and the Boss only react to the router's
  // decision: the Claude API stays off for paper trading.
  var CAST_ROLES = {
    scout: "Scans every coin on each radar cycle.",
    analyst: "The local model (Qwen) that double-checks the numbers.",
    strategist: "Joins when the router says an alert deserves a closer look.",
    boss: "Joins only when the router says an alert deserves the big model.",
    treasurer: "Opens and closes the plays in the pretend wallet.",
  };

  // -- decimal strings from the bridge -------------------------------------------
  // Money crosses the bridge as decimal strings in cents ("1000.00", "-0.35")
  // and prices as recorded decimal strings. They are parsed as text, never as
  // floats, so the digits shown are exactly the digits the backend produced.
  var DECIMAL_RE = /^([+-]?)(\d+)(?:\.(\d+))?$/;

  function parseDecimal(value) {
    if (typeof value !== "string") return null;
    var m = DECIMAL_RE.exec(value.trim());
    if (!m) return null;
    var intDigits = m[2].replace(/^0+(?=\d)/, "");
    var frac = m[3] || "";
    var zero = /^0+$/.test(intDigits + frac);
    return { sign: zero ? 0 : (m[1] === "-" ? -1 : 1), int: intDigits, frac: frac };
  }

  function signOf(value) {
    var d = parseDecimal(value);
    return d ? d.sign : null;
  }

  function groupDigits(intDigits) {
    return intDigits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  var SYMBOLS = { EUR: "€", USD: "$", GBP: "£" };

  function currencyCode(code) {
    return typeof code === "string" && /^[A-Z0-9]{2,6}$/.test(code) ? code : null;
  }

  function withCurrency(sign, digits, currency) {
    var code = currencyCode(currency) || "EUR";
    var symbol = SYMBOLS[code];
    return symbol ? sign + symbol + digits : sign + digits + " " + code;
  }

  // "€1,000.00" / "+€0.35" / "-€0.58" (English format, like ui/paper_texts.py
  // format_money); zero never carries a sign. null when not a decimal string.
  function formatMoney(value, currency, opts) {
    var d = parseDecimal(value);
    if (!d) return null;
    var signed = !!(opts && opts.signed);
    var sign = d.sign < 0 ? "-" : (signed && d.sign > 0 ? "+" : "");
    var digits = groupDigits(d.int) + (d.frac ? "." + d.frac : "");
    return withCurrency(sign, digits, currency);
  }

  // A recorded price with all its recorded digits, in the pair's quote currency.
  function formatPrice(value, quote) {
    return formatMoney(value, quote, { signed: false });
  }

  function formatPercent(value, opts) {
    var d = parseDecimal(value);
    if (!d) return null;
    var signed = !!(opts && opts.signed);
    var sign = d.sign < 0 ? "-" : (signed && d.sign > 0 ? "+" : "");
    return sign + groupDigits(d.int) + (d.frac ? "." + d.frac : "") + "%";
  }

  function toneOf(value) {
    var s = signOf(value);
    return s > 0 ? "up" : s < 0 ? "down" : "flat";
  }

  // -- times -----------------------------------------------------------------------
  function parseTime(value) {
    if (typeof value !== "string") return null;
    var ms = Date.parse(value);
    return isFinite(ms) ? ms : null;
  }

  function pad2(n) {
    return (n < 10 ? "0" : "") + n;
  }

  // Local HH:MM of a recorded time, "—" when it cannot be placed in time.
  function clockText(value, withSeconds) {
    var ms = parseTime(value);
    if (ms === null) return "—";
    var t = new Date(ms);
    var text = pad2(t.getHours()) + ":" + pad2(t.getMinutes());
    return withSeconds ? text + ":" + pad2(t.getSeconds()) : text;
  }

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function dateText(value) {
    var ms = parseTime(value);
    if (ms === null) return null;
    var t = new Date(ms);
    return t.getDate() + " " + MONTHS[t.getMonth()] + " " + t.getFullYear();
  }

  // How far a play is through its hold, 0..1; null when its times are unusable.
  function timeFraction(openedAt, dueAt, nowMs) {
    var open = parseTime(openedAt);
    var due = parseTime(dueAt);
    if (open === null || due === null || due <= open || !isFinite(nowMs)) return null;
    var f = (nowMs - open) / (due - open);
    return f < 0 ? 0 : f > 1 ? 1 : f;
  }

  function minutesLeft(dueAt, nowMs) {
    var due = parseTime(dueAt);
    if (due === null || !isFinite(nowMs)) return null;
    return Math.max(0, Math.ceil((due - nowMs) / 60000));
  }

  // "45 min", "23 h 12 min", "24 h" for a whole number of minutes; null otherwise.
  function durationText(minutes) {
    if (typeof minutes !== "number" || !isFinite(minutes) || minutes < 0 || Math.floor(minutes) !== minutes) return null;
    if (minutes < 60) return minutes + " min";
    var rest = minutes % 60;
    return Math.floor(minutes / 60) + " h" + (rest ? " " + rest + " min" : "");
  }

  // True when the play carries its recorded stop and target (the EX-1 exit rule); a
  // legacy play (fixed hold, no levels) has neither and nothing is shown for them.
  function hasLevels(play) {
    return !!(play && parseDecimal(play.stop) && parseDecimal(play.target));
  }

  // The time limit a play counts down to: its recorded EX-1 due time, or the
  // planned close of a legacy play.
  function dueOf(play) {
    return play ? (str(play.exit_due_at) || play.due_at) : null;
  }

  function closingText(play, nowMs) {
    if (play && play.status === "PENDING_EXIT") {
      return "Closing time passed: it closes at the first valid price.";
    }
    var levels = hasLevels(play);
    var due = dueOf(play);
    var left = minutesLeft(due, nowMs);
    if (left === null) return levels ? "Closes at the stop, the target or the time limit." : "Closes at the planned time.";
    if (left === 0) return "Closing now.";
    if (levels) return "At most " + durationText(left) + " left (until " + clockText(due) + ").";
    return "Closes in " + durationText(left) + " (at " + clockText(due) + ").";
  }

  // -- the balance chart -------------------------------------------------------------
  // One point per recorded balance (the start, then the balance after each
  // closed play), evenly spaced; a dashed line marks the start balance.
  function round2(n) {
    return Math.round(n * 100) / 100;
  }

  function chartGeometry(series, startBalance, opts) {
    var width = (opts && opts.width) || 600;
    var height = (opts && opts.height) || 120;
    var pad = (opts && opts.pad !== undefined) ? opts.pad : 10;
    var values = [];
    (Array.isArray(series) ? series : []).forEach(function (p) {
      var d = p && parseDecimal(p.balance);
      if (d) values.push(Number(p.balance));
    });
    if (values.length === 0) return null;
    var start = parseDecimal(startBalance) ? Number(startBalance) : values[0];
    var min = Math.min.apply(null, values.concat([start]));
    var max = Math.max.apply(null, values.concat([start]));
    if (max === min) { max += 1; min -= 1; }
    function y(v) { return round2(pad + (max - v) / (max - min) * (height - 2 * pad)); }
    var n = values.length;
    var points = values.map(function (v, i) {
      return { x: n === 1 ? width : round2(i * width / (n - 1)), y: y(v) };
    });
    // A single recorded balance is the start still held now: a flat line.
    var drawn = n === 1 ? [{ x: 0, y: points[0].y }, points[0]] : points;
    var line = drawn.map(function (p, i) { return (i ? "L" : "M") + p.x + "," + p.y; }).join(" ");
    var last = points[points.length - 1];
    return {
      width: width,
      height: height,
      points: points,
      line: line,
      area: line + " L" + width + "," + height + " L0," + height + " Z",
      startY: y(start),
      last: last,
      trend: values[n - 1] > start ? "up" : values[n - 1] < start ? "down" : "flat",
    };
  }

  // -- which state to show -------------------------------------------------------------
  function str(value) {
    return typeof value === "string" && value.trim() ? value : null;
  }

  function selectView(state) {
    if (state === undefined) return { kind: "loading", message: TEXT.loading, disabled: false };
    if (!state || typeof state !== "object" || Array.isArray(state)) {
      return { kind: "unavailable", message: TEXT.unreadable, disabled: false };
    }
    var disabled = state.enabled === false;
    if (!state.available || !state.wallet || typeof state.wallet !== "object") {
      return { kind: "waiting", message: str(state.reason) || TEXT.noPlays, disabled: disabled };
    }
    return { kind: "ready", message: str(state.reason), disabled: disabled };
  }

  function openPlays(state) {
    return state && Array.isArray(state.open_plays) ? state.open_plays.filter(function (p) {
      return p && typeof p === "object";
    }) : [];
  }

  // The play shown large: the newest open one (the others are listed below it).
  function mainPlay(state) {
    var plays = openPlays(state);
    return plays.length ? plays[plays.length - 1] : null;
  }

  function radarPill(procState) {
    switch (procState) {
      case "RUNNING": return { cls: "ok", label: "Radar on" };
      case "STARTING": return { cls: "warn", label: "Radar starting" };
      case "STOPPING": return { cls: "warn", label: "Radar stopping" };
      case "ERROR": return { cls: "err", label: "Radar stopped on an error" };
      case "STOPPED": return { cls: "idle", label: "Radar off" };
      default: return { cls: "idle", label: "Radar state unknown" };
    }
  }

  // -- responses arrive in order or are dropped -------------------------------------
  // Each request gets a sequence number. A response (success or failure) is
  // applied only when it is newer than every response already settled, and a
  // success whose generated_at is older than the one on screen is dropped too.
  function createStaleGuard() {
    var issued = 0;
    var settled = 0;
    var shownAt = null;
    return {
      next: function () { issued += 1; return issued; },
      accept: function (seq, generatedAt) {
        if (seq <= settled) return false;
        var at = parseTime(generatedAt);
        if (at !== null && shownAt !== null && at < shownAt) return false;
        settled = seq;
        if (at !== null) shownAt = at;
        return true;
      },
      acceptFailure: function (seq) {
        if (seq <= settled) return false;
        settled = seq;
        return true;
      },
    };
  }

  // Polls fetchState() while started: one request at a time, the next one
  // scheduled after the previous settles. stop() drops any response still in
  // flight; start() after stop() begins a new generation, so repeated
  // start/stop/start never leaves two timers running.
  function createPoller(opts) {
    var setT = opts.setTimeout || setTimeout;
    var clearT = opts.clearTimeout || clearTimeout;
    var guard = opts.guard || createStaleGuard();
    var running = false;
    var generation = 0;
    var timer = null;

    function schedule(gen) {
      if (!running || gen !== generation) return;
      timer = setT(function () { timer = null; poll(gen); }, opts.intervalMs);
    }

    function poll(gen) {
      var seq = guard.next();
      var request;
      try {
        request = Promise.resolve(opts.fetchState());
      } catch (err) {
        request = Promise.reject(err);
      }
      return request.then(function (state) {
        if (running && gen === generation && guard.accept(seq, state && state.generated_at)) opts.onState(state);
      }, function (err) {
        if (running && gen === generation && guard.acceptFailure(seq)) opts.onError(err);
      }).then(function () { schedule(gen); });
    }

    return {
      start: function () {
        if (running) return null;
        running = true;
        generation += 1;
        return poll(generation);
      },
      stop: function () {
        running = false;
        generation += 1;
        if (timer !== null) { clearT(timer); timer = null; }
      },
      isRunning: function () { return running; },
      pendingTimer: function () { return timer !== null; },
    };
  }

  // -- DOM building (text only) --------------------------------------------------------
  function el(doc, tag, attrs, children) {
    var node = doc.createElement(tag);
    setAttrs(node, attrs);
    append(doc, node, children);
    return node;
  }

  function svg(doc, tag, attrs, children) {
    var node = doc.createElementNS(SVG_NS, tag);
    setAttrs(node, attrs);
    append(doc, node, children);
    return node;
  }

  function setAttrs(node, attrs) {
    if (!attrs) return;
    Object.keys(attrs).forEach(function (k) {
      if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) node.setAttribute(k, String(attrs[k]));
    });
  }

  function append(doc, node, children) {
    if (children === null || children === undefined) return;
    (Array.isArray(children) ? children : [children]).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      node.appendChild(typeof c === "string" ? doc.createTextNode(c) : c);
    });
  }

  function pillNode(doc, cls, label) {
    return el(doc, "span", { "class": "pill " + cls }, [el(doc, "span", { "class": "dot" }), label]);
  }

  function emptyNode(doc, title, sub) {
    return el(doc, "div", { "class": "pg-empty" }, [
      el(doc, "div", { "class": "pg-empty-title" }, title),
      sub ? el(doc, "div", { "class": "pg-empty-sub" }, sub) : null,
    ]);
  }

  function orDash(text) {
    return text === null || text === undefined ? "—" : text;
  }

  // -- panels --------------------------------------------------------------------------
  function walletNodes(doc, state, view, refs) {
    refs.asOf = null;
    if (view.kind !== "ready") {
      return [emptyNode(doc, view.message, view.kind === "waiting" && state && state.params
        ? startLine(state.params.start_balance, state.currency) : null)];
    }
    var w = state.wallet;
    var currency = state.currency;
    var change = formatMoney(w.change, currency, { signed: true });
    var pct = formatPercent(w.change_pct, { signed: true });
    var since = Array.isArray(w.series) && w.series[0] ? dateText(w.series[0].ts) : null;
    var nodes = [
      el(doc, "div", { "class": "pg-wallet" }, [
        el(doc, "div", null, [
          el(doc, "div", { "class": "pg-big mono" }, orDash(formatMoney(w.balance, currency))),
          el(doc, "div", { "class": "pg-sub" }, [
            // With a valuation beside it, say which total this is: closed plays only.
            isObject(w.valuation) ? "Balance after closed plays. " : null,
            "Started with ", el(doc, "span", { "class": "mono" }, orDash(formatMoney(w.start_balance, currency))),
            since ? " on " + since : "",
          ]),
        ]),
        el(doc, "div", { "class": "pg-delta mono tone-" + toneOf(w.change) },
          orDash(change) + (pct ? " (" + pct + ")" : "")),
      ]),
    ];
    nodes.push(valueNode(doc, state, w.valuation, refs));
    nodes.push(chartNode(doc, w, currency));
    nodes.push(tallyNode(doc, w));
    nodes.push(el(doc, "div", { "class": "pg-cash" }, [
      el(doc, "span", null, ["In open plays ", el(doc, "b", { "class": "mono" }, orDash(formatMoney(w.open_stakes, currency)))]),
      el(doc, "span", null, ["Free to bet ", el(doc, "b", { "class": "mono" }, orDash(formatMoney(w.available_cash, currency)))]),
      el(doc, "span", null, ["Commissions paid ", el(doc, "b", { "class": "mono" }, orDash(formatMoney(w.fees_total, currency)))]),
    ]));
    if (str(w.cost_sentence)) nodes.push(el(doc, "div", { "class": "pg-cost" }, w.cost_sentence));
    return nodes;
  }

  function isObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }

  // "as of 10:30:02" for a recorded time; null when it cannot be placed.
  function asOfText(value) {
    return parseTime(value) === null ? null : "as of " + clockText(value, true);
  }

  // The backend's sentence for why an open play has no price, found by its id.
  function markReason(state, playId) {
    var found = openPlays(state).filter(function (p) { return p.play_id === playId; })[0];
    return found && isObject(found.mark) ? str(found.mark.reason_text) : null;
  }

  // "Value now": the six amounts of wallet.valuation, each a backend string. An
  // amount the backend left null (an open play without a usable price) is a dash
  // with the reason, never 0. A legacy payload has no valuation: nothing is shown.
  function valueNode(doc, state, v, refs) {
    if (!isObject(v)) return null;
    var currency = str(v.currency) || state.currency;
    var unmarked = Array.isArray(v.unmarked) ? v.unmarked.filter(isObject) : [];
    var kinds = unmarked.map(function (u) { return u.status; }).filter(function (k, i, a) { return a.indexOf(k) === i; });
    var why = kinds.length === 1 && MARK_WORDS[kinds[0]] ? MARK_WORDS[kinds[0]] : "no usable price";
    var asOf = el(doc, "span", { "class": "pg-value-at mono" }, orDash(asOfText(v.as_of)));
    refs.asOf = asOf;
    var rows = VALUE_ROWS.map(function (row) {
      var shown = formatMoney(v[row.key], currency, { signed: !!row.signed });
      return el(doc, "div", { "class": row.lead ? "pg-value-lead" : null }, [
        el(doc, "dt", null, row.label),
        el(doc, "dd", null, shown === null
          ? [el(doc, "span", { "class": "mono" }, "—"), el(doc, "span", { "class": "pg-value-missing" }, "unknown: " + why)]
          : el(doc, "span", { "class": "mono" + (row.signed ? " tone-" + toneOf(v[row.key]) : "") }, shown)),
      ]);
    });
    var freshness = isObject(v.freshness) ? str(v.freshness.text) : null;
    return el(doc, "div", { "class": "pg-value" + (v.stale === true ? " is-stale" : ""), role: "group",
      "aria-labelledby": "pg-value-h" }, [
      el(doc, "div", { "class": "pg-value-head" }, [
        el(doc, "b", { id: "pg-value-h" }, TEXT.valueTitle),
        asOf,
        v.stale === true ? pillNode(doc, "warn", "price missing or too old") : null,
      ]),
      el(doc, "dl", { "class": "pg-value-grid" }, rows),
      unmarked.length ? el(doc, "div", { "class": "pg-value-note" }, [
        TEXT.valueUnknown,
        el(doc, "ul", null, unmarked.map(function (u) {
          return el(doc, "li", null, [el(doc, "b", { "class": "mono" }, orDash(str(u.pair))), " ",
            orDash(markReason(state, u.play_id) || MARK_WORDS[u.status] || null)]);
        })),
      ]) : null,
      el(doc, "p", { "class": "pg-value-basis" }, [str(v.valuation_basis_text), freshness ? " " + freshness : null]),
    ]);
  }

  // The stored fee (ASSUMED, account tier unverified) and, for a pair priced in
  // another currency, the FX-excluded label: backend sentences, one per line.
  function provenanceNode(doc, item, extra) {
    var lines = [str(item.fee_text), item.fx_excluded === true ? str(item.fx_text) : null].filter(Boolean);
    if (!lines.length) return null;
    return el(doc, "span", { "class": "pg-prov" + (extra ? " " + extra : "") }, lines.map(function (line) {
      return el(doc, "span", null, line);
    }));
  }

  function startLine(startBalance, currency) {
    var start = formatMoney(startBalance, currency);
    return start ? "The pretend wallet starts with " + start + "." : null;
  }

  function chartNode(doc, w, currency) {
    var g = chartGeometry(w.series, w.start_balance, { width: 600, height: 120 });
    if (!g) return null;
    var first = formatMoney(w.start_balance, currency);
    var now = formatMoney(w.balance, currency);
    var closes = g.points.length - 1;
    var label = "Balance after each closed play: started at " + orDash(first) + ", now " + orDash(now) +
      " after " + closes + (closes === 1 ? " closed play." : " closed plays.");
    var gradId = "pg-chart-fill";
    var stroke = g.trend === "down" ? "#F0616B" : g.trend === "up" ? "#34D399" : "#5B9DF6";
    return el(doc, "div", { "class": "pg-chart" }, [
      svg(doc, "svg", { viewBox: "0 0 600 120", preserveAspectRatio: "none", role: "img", "aria-label": label }, [
        svg(doc, "defs", null, [svg(doc, "linearGradient", { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 }, [
          svg(doc, "stop", { offset: 0, "stop-color": stroke, "stop-opacity": ".30" }),
          svg(doc, "stop", { offset: 1, "stop-color": stroke, "stop-opacity": "0" }),
        ])]),
        // With no closed play yet only the start line is drawn: nothing has moved.
        closes ? svg(doc, "path", { d: g.area, fill: "url(#" + gradId + ")" }) : null,
        closes ? svg(doc, "path", { "class": "pg-balance-line", d: g.line, fill: "none", stroke: stroke,
          "stroke-width": 2.5, "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke" }) : null,
        // Drawn last so the dashes stay visible where the balance sits on the start.
        svg(doc, "line", { "class": "pg-start-line", x1: 0, y1: g.startY, x2: 600, y2: g.startY,
          stroke: "#8A95A8", "stroke-dasharray": "5 5", "vector-effect": "non-scaling-stroke" }),
      ]),
      el(doc, "div", { "class": "pg-axis mono" }, [
        el(doc, "span", null, "start · " + orDash(first)),
        el(doc, "span", { "class": "pg-axis-mid" }, "dashed line = starting balance"),
        el(doc, "span", null, "now · " + orDash(now)),
      ]),
    ]);
  }

  var RESULT_WORDS = { WIN: "won", LOSS: "lost", FLAT: "flat" };

  function tallyNode(doc, w) {
    var results = Array.isArray(w.last_results) ? w.last_results.filter(function (r) { return RESULT_WORDS[r]; }) : [];
    var total = [w.wins, w.losses, w.flats].every(function (n) { return typeof n === "number"; })
      ? w.wins + w.losses + w.flats : null;
    var children = [];
    if (results.length) {
      children.push(el(doc, "div", {
        "class": "pg-tally", role: "img",
        "aria-label": "Last " + results.length + " plays, oldest first: " + results.map(function (r) { return RESULT_WORDS[r]; }).join(", "),
      }, results.map(function (r) { return el(doc, "i", { "class": "r-" + r.toLowerCase() }); })));
    }
    children.push(el(doc, "div", { "class": "pg-legend" }, total === null || total === 0 ? [
      el(doc, "span", null, "No closed plays yet."),
    ] : [
      el(doc, "span", null, ["Won ", el(doc, "b", { "class": "mono" }, String(w.wins))]),
      el(doc, "span", null, ["Lost ", el(doc, "b", { "class": "mono" }, String(w.losses))]),
      w.flats ? el(doc, "span", null, ["Flat ", el(doc, "b", { "class": "mono" }, String(w.flats))]) : null,
      el(doc, "span", { "class": "pg-dim" }, "of " + total + (total === 1 ? " closed play" : " closed plays") +
        (results.length && results.length < total ? "; squares show the last " + results.length : "")),
    ]));
    return el(doc, "div", null, children);
  }

  function botNode(doc) {
    return svg(doc, "svg", { "class": "pg-bot", viewBox: "0 0 96 112", "aria-hidden": "true" }, [
      svg(doc, "rect", { x: 44, y: 4, width: 8, height: 14, rx: 4, fill: "#5B9DF6" }),
      svg(doc, "rect", { x: 18, y: 16, width: 60, height: 40, rx: 12, fill: "#161B23", stroke: "#1E242D", "stroke-width": 2 }),
      svg(doc, "rect", { "class": "pg-eye", x: 32, y: 30, width: 10, height: 10, rx: 5, fill: "#E6E9EE" }),
      svg(doc, "rect", { "class": "pg-eye", x: 54, y: 30, width: 10, height: 10, rx: 5, fill: "#E6E9EE" }),
      svg(doc, "rect", { x: 22, y: 62, width: 52, height: 42, rx: 10, fill: "#161B23", stroke: "#1E242D", "stroke-width": 2 }),
      svg(doc, "circle", { "class": "pg-core", cx: 48, cy: 83, r: 7, fill: "#5B9DF6" }),
    ]);
  }

  function playNodes(doc, state, view, refs) {
    refs.fill = null; refs.bar = null; refs.left = null; refs.play = null;
    refs.disabled = view.disabled;
    if (view.kind !== "ready") return [emptyNode(doc, view.kind === "waiting" ? TEXT.looking : view.message,
      view.kind === "waiting" ? (view.disabled ? TEXT.switchedOff : TEXT.lookingSub) : null)];
    var play = mainPlay(state);
    if (!play) {
      return [emptyNode(doc, TEXT.looking, view.disabled ? TEXT.switchedOff : TEXT.lookingSub)];
    }
    refs.play = play;
    var currency = state.currency;
    var gross = formatMoney(play.gross_now, currency, { signed: true });
    var fill = el(doc, "div", { "class": "pg-fill" });
    var bar = el(doc, "div", { "class": "pg-track", role: "progressbar",
      "aria-label": hasLevels(play) ? "Time used of the play's time limit" : "Time until the play closes",
      "aria-valuemin": 0, "aria-valuemax": 100 }, fill);
    var left = el(doc, "span", null, "");
    refs.fill = fill; refs.bar = bar; refs.left = left;
    var nodes = [
      el(doc, "div", { "class": "pg-pick" }, [
        botNode(doc),
        el(doc, "div", null, [
          el(doc, "div", { "class": "pg-coin" }, [
            el(doc, "span", { "class": "pg-sym mono" }, orDash(str(play.asset))),
            str(play.pair) ? el(doc, "span", { "class": "pg-pair mono" }, play.pair) : null,
            str(play.direction_text) ? pillNode(doc, "info", play.direction_text) : null,
          ]),
          el(doc, "p", { "class": "pg-why" }, orDash(str(play.why))),
          el(doc, "div", { "class": "pg-facts" }, [
            el(doc, "div", null, ["Went in at", el(doc, "b", { "class": "mono" },
              orDash(formatPrice(play.direction === "SHORT" ? play.entry_bid : play.entry_ask, play.quote)))]),
            el(doc, "div", null, ["Now", el(doc, "b", { "class": "mono" }, orDash(formatPrice(
              play.now_bid && play.now_ask ? midText(play) : null, play.quote)))]),
            el(doc, "div", null, ["Stake", el(doc, "b", { "class": "mono" }, orDash(formatMoney(play.stake, currency)))]),
            // The recorded exit levels; a play without them (legacy) shows neither.
            hasLevels(play) ? el(doc, "div", { "class": "pg-level" }, ["Stop (loss limit)",
              el(doc, "b", { "class": "mono" }, formatPrice(play.stop, play.quote))]) : null,
            hasLevels(play) ? el(doc, "div", { "class": "pg-level" }, ["Target (profit goal)",
              el(doc, "b", { "class": "mono" }, formatPrice(play.target, play.quote))]) : null,
            // The play closed now at its valuation price (absent from a legacy payload).
            isObject(play.mark) ? el(doc, "div", null, ["If closed now", el(doc, "b", { "class": "mono" },
              orDash(formatMoney(play.liquidation_value, currency)))]) : null,
            isObject(play.mark) ? el(doc, "div", null, ["After all costs", el(doc, "b", {
              "class": "mono tone-" + (parseDecimal(play.open_net_pnl) ? toneOf(play.open_net_pnl) : "flat") },
              orDash(formatMoney(play.open_net_pnl, currency, { signed: true })))]) : null,
          ]),
          isObject(play.mark) && play.mark.stale === true
            ? el(doc, "p", { "class": "pg-mark-stale" }, orDash(str(play.mark.reason_text))) : null,
          el(doc, "div", { "class": "pg-timer" }, [
            bar,
            el(doc, "div", { "class": "pg-timer-row" }, [
              left,
              el(doc, "span", { "class": "mono tone-" + (gross ? toneOf(play.gross_now) : "flat") },
                gross ? gross + " so far, before costs" : "No recent price yet"),
            ]),
          ]),
          provenanceNode(doc, play),
        ]),
      ]),
    ];
    if (Array.isArray(play.steps) && play.steps.length) {
      nodes.push(el(doc, "ol", { "class": "pg-steps" }, play.steps.map(function (s) {
        return el(doc, "li", null, [
          el(doc, "b", null, [el(doc, "i", { "class": s && s.state === "now" ? "now" : "done", "aria-hidden": "true" }),
            orDash(str(s && s.title))]),
          orDash(str(s && s.text)),
        ]);
      })));
    }
    var others = openPlays(state).filter(function (p) { return p !== play; });
    if (others.length) {
      nodes.push(el(doc, "ul", { "class": "pg-others" }, others.map(function (p) {
        return el(doc, "li", null, [
          el(doc, "b", { "class": "mono" }, orDash(str(p.asset))), " ",
          orDash(str(p.direction_text)), " · ",
          el(doc, "span", { "class": "mono" }, orDash(formatMoney(p.stake, currency))), " · ",
          view.disabled ? "paused" : closingText(p, Date.now()),
        ]);
      })));
    }
    return nodes;
  }

  // The recorded mid of the latest quote is not in the payload; show the side
  // the play would close on (LONG sells at the bid, SHORT buys back at the ask).
  function midText(play) {
    return play.direction === "SHORT" ? play.now_ask : play.now_bid;
  }

  function updateTimer(refs, nowMs) {
    if (!refs.play || !refs.fill) return;
    var f = timeFraction(refs.play.opened_at, dueOf(refs.play), nowMs);
    var pct = f === null ? 0 : Math.round(f * 1000) / 10;
    refs.fill.setAttribute("style", "width:" + pct + "%");
    refs.bar.setAttribute("aria-valuenow", String(Math.round(pct)));
    refs.left.textContent = refs.disabled ? TEXT.paused : closingText(refs.play, nowMs);
  }

  function historyNodes(doc, state, view) {
    var items = view.kind === "ready" && Array.isArray(state.history) ? state.history.filter(function (h) {
      return h && typeof h === "object";
    }) : [];
    if (!items.length) return [emptyNode(doc, view.kind === "ready" || view.kind === "waiting" ? TEXT.noPlays : view.message)];
    var currency = state.currency;
    var nodes = [el(doc, "ul", { "class": "pg-feed" }, items.map(function (h) {
      return el(doc, "li", null, [
        el(doc, "span", { "class": "pg-t mono" }, clockText(h.closed_at)),
        el(doc, "span", { "class": "pg-s" }, [
          el(doc, "b", { "class": "mono" }, orDash(str(h.asset))),
          el(doc, "span", null, [
            // The recorded close reason (stop, target, time limit); none on a legacy close.
            str(h.exit_reason_text) ? el(doc, "span", { "class": "pg-reason" }, h.exit_reason_text) : null,
            orDash(str(h.sentence)),
            provenanceNode(doc, h, "pg-prov-row"),
          ]),
        ]),
        el(doc, "span", { "class": "pg-r mono tone-" + toneOf(h.net) }, orDash(formatMoney(h.net, currency, { signed: true }))),
      ]);
    }))];
    if (typeof state.history_total === "number" && state.history_total > items.length) {
      nodes.push(el(doc, "div", { "class": "pg-dim pg-more" },
        "Showing the latest " + items.length + " of " + state.history_total + " closed plays."));
    }
    return nodes;
  }

  function castNodes(doc, state, withOffice) {
    var agents = state && Array.isArray(state.agents) ? state.agents : [];
    return [
      withOffice ? null : el(doc, "div", { "class": "pg-office-soon" }, TEXT.officeSoon),
      el(doc, "div", { "class": "pg-cast" }, agents.filter(function (a) { return a && CAST_ROLES[a.id]; }).map(function (a) {
        var color = /^#[0-9A-Fa-f]{6}$/.test(a.color) ? a.color : "#4B5563";
        return el(doc, "div", { "class": a.enabled === false ? "off" : null }, [
          el(doc, "i", { style: "background:" + color, "aria-hidden": "true" }),
          el(doc, "b", null, orDash(str(a.name))),
          CAST_ROLES[a.id],
          el(doc, "span", { "class": "pg-seen mono" }, a.enabled === false ? "switched off"
            : a.last_activity_ts ? "last seen " + clockText(a.last_activity_ts) : "nothing recorded in 24 h"),
        ]);
      })),
    ];
  }

  // Fields that change on every reading without changing what a panel shows
  // (the as-of time is updated in place): left out so a panel is not rebuilt.
  var VOLATILE = { as_of: true, age_seconds: true };

  function signature(value) {
    try {
      return JSON.stringify(value, function (key, v) { return VOLATILE[key] === true ? undefined : v; });
    } catch (e) { return String(Math.random()); }
  }

  // The reason sentences the wallet's "Value now" shows, so it is rebuilt when one changes.
  function unmarkedReasons(state) {
    return openPlays(state).map(function (p) { return [p.play_id, isObject(p.mark) ? p.mark.reason_text : null]; });
  }

  // Short plain-text announcements for the live region; nothing on the first
  // reading, only when a play opened or closed since the previous one.
  function announcement(prev, next) {
    if (!prev || !next || !next.available) return null;
    var before = {};
    openPlays(prev).forEach(function (p) { before[p.play_id] = true; });
    var opened = openPlays(next).filter(function (p) { return !before[p.play_id]; });
    var closedCount = (typeof next.history_total === "number" && typeof prev.history_total === "number")
      ? next.history_total - prev.history_total : 0;
    var parts = [];
    if (closedCount > 0 && Array.isArray(next.history) && next.history[0] && str(next.history[0].sentence)) {
      parts.push((closedCount === 1 ? "A play closed. " : closedCount + " plays closed. Latest: ") + next.history[0].sentence);
    }
    opened.forEach(function (p) {
      parts.push("New play: " + orDash(str(p.asset)) + (str(p.direction_text) ? ", " + p.direction_text : "") + ".");
    });
    return parts.length ? parts.join(" ") : null;
  }

  // -- the tab --------------------------------------------------------------------------
  // slots: {radar, game, wallet, walletNote, playStatus, play, history, office, live, refresh}
  function createView(slots, opts) {
    var doc = (opts && opts.doc) || (typeof document !== "undefined" ? document : null);
    var now = (opts && opts.now) || function () { return Date.now(); };
    var sigs = {};
    var refs = {};
    var last;
    var lastAppliedAt = null;
    // The animated office (paper_office.js), when loaded: it owns a stage in the
    // office slot and the cast legend goes in a holder under it.
    var officeFactory = opts && opts.officeView !== undefined ? opts.officeView
      : (typeof window !== "undefined" && window.RadarPaperOfficeView ? window.RadarPaperOfficeView : null);
    var office = null;
    var castSlot = slots.office;
    if (officeFactory && slots.office && doc) {
      var stageHolder = el(doc, "div", { "class": "pg-office-stage" });
      castSlot = el(doc, "div", { "class": "pg-office-cast" });
      slots.office.appendChild(stageHolder);
      slots.office.appendChild(castSlot);
      office = officeFactory.createOfficeView(stageHolder, { doc: doc });
    }

    function put(name, nodes, sig) {
      var slot = name === "office" ? castSlot : slots[name];
      if (!slot) return;
      if (sig !== undefined && sigs[name] === sig) return;
      sigs[name] = sig;
      while (slot.firstChild) slot.removeChild(slot.firstChild);
      append(doc, slot, typeof nodes === "function" ? nodes() : nodes);
    }

    function render(state) {
      var view = selectView(state);
      var play = view.kind === "ready" ? mainPlay(state) : null;
      put("game", view.disabled ? [pillNode(doc, "idle", "Game switched off")] : [], "g" + view.disabled);
      put("playStatus", play ? [view.disabled ? pillNode(doc, "idle", "paused") : pillNode(doc,
        play.status === "PENDING_EXIT" ? "warn" : "info",
        play.status === "PENDING_EXIT" ? "waiting for a price" : "in progress")] : [],
        signature([play && play.status, view.disabled]));
      put("wallet", function () { return walletNodes(doc, state, view, refs); }, signature([view, state && state.wallet,
        state && state.currency, view.kind === "waiting" && state && state.params, unmarkedReasons(state)]));
      // Not part of the wallet's signature: the time of the reading on screen, updated in place.
      var valuation = view.kind === "ready" && isObject(state.wallet) ? state.wallet.valuation : null;
      if (refs.asOf && isObject(valuation)) refs.asOf.textContent = orDash(asOfText(valuation.as_of));
      // Built only when the slot changes: the timer refs must point at the nodes on screen.
      put("play", function () { return playNodes(doc, state, view, refs); }, signature([view, state && state.open_plays, state && state.currency]));
      put("history", historyNodes(doc, state, view), signature([view, state && state.history, state && state.history_total]));
      put("office", castNodes(doc, state, office !== null), signature(state && state.agents));
      if (office) office.update(state);
      updateTimer(refs, now());
      if (slots.live && last !== undefined) {
        var said = announcement(last, state);
        if (said) slots.live.textContent = said;
      }
      if (slots.refresh) slots.refresh.textContent = "";
      last = state;
      lastAppliedAt = state && state.generated_at;
    }

    return {
      render: render,
      renderError: function () {
        if (office) office.noteDisconnect();
        if (last === undefined || !last || typeof last !== "object") {
          render(null);
          return;
        }
        if (slots.refresh) {
          slots.refresh.textContent = "Could not refresh; showing the reading from " + clockText(lastAppliedAt, true) + ".";
        }
      },
      setRadarState: function (procState) {
        var p = radarPill(procState);
        put("radar", [pillNode(doc, p.cls, p.label)], p.cls + p.label);
      },
      tick: function () { updateTimer(refs, now()); },
    };
  }

  return {
    TEXT: TEXT,
    parseDecimal: parseDecimal,
    signOf: signOf,
    formatMoney: formatMoney,
    formatPrice: formatPrice,
    formatPercent: formatPercent,
    toneOf: toneOf,
    clockText: clockText,
    timeFraction: timeFraction,
    minutesLeft: minutesLeft,
    durationText: durationText,
    hasLevels: hasLevels,
    dueOf: dueOf,
    closingText: closingText,
    chartGeometry: chartGeometry,
    selectView: selectView,
    mainPlay: mainPlay,
    signature: signature,
    VALUE_ROWS: VALUE_ROWS,
    radarPill: radarPill,
    announcement: announcement,
    createStaleGuard: createStaleGuard,
    createPoller: createPoller,
    createView: createView,
  };
});
