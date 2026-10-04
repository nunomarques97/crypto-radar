/* AI Game (paper game) - the isometric office renderer (DESIGN.md "AI Game
 * (paper game) - scoped direction", mock docs/design/mocks/paper-game-office.html).
 *
 * It draws the office in inline SVG and plays the scene plan of the choreography
 * engine (paper_office_engine.js, window.RadarPaperOffice): it never decides who
 * moves. Every pose, walk and bubble comes from engine.frame(), which comes from
 * real activity entries of Api.get_paper_state(); the wall board and the wallet
 * screen show only get_paper_state() values. Backend strings go in via
 * textContent, never as markup.
 *
 * One requestAnimationFrame loop updates transforms and opacity only (the rare
 * depth reorder when an agent walks past furniture moves existing nodes). The loop
 * runs only while the office is on screen and the page is visible, and
 * prefers-reduced-motion stops the idle motion (the engine makes moves instant).
 *
 * UMD like paper_game.js: window.RadarPaperOfficeView in the browser, and the
 * pure boardModel() is tested under Node by tests/ui_tests/js/test_paper_game.mjs.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.RadarPaperOfficeView = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";

  // -- projection (the mock's isometric grid, sized to fit the whole floor) -------------
  var S = 42;                      // one floor unit, in viewBox pixels
  var C30 = Math.cos(Math.PI / 6);
  var OX = 376;
  var OY = 136;
  var WALL_H = 3.0;
  var VIEW_W = 970;
  var VIEW_H = 692;

  function P(x, y, z) { return [OX + (x - y) * C30 * S, OY + (x + y) * 0.5 * S - z * S]; }
  function L(x, y, z) { return [(x - y) * C30 * S, (x + y) * 0.5 * S - z * S]; }
  function pts(list) { return list.map(function (p) { return r1(p[0]) + "," + r1(p[1]); }).join(" "); }
  function r1(v) { return Math.round(v * 10) / 10; }

  // -- tokens (DESIGN.md) ---------------------------------------------------------------
  var COLORS = {
    floor: "#1A2029", backWall: "#202734", leftWall: "#171D26", grid: "#222A35",
    screen: "#0B0E13", text: "#E6E9EE", muted: "#8B95A5", dim: "#5A6577",
    up: "#34D399", down: "#F0616B", flat: "#8B95A5", info: "#5B9DF6", warn: "#F5B94D",
    skin: "#E8D5B7", hair: "#2A2F3A", trousers: "#2A3140", desk: "#3A4556", chair: "#262D3A",
    table: "#7A5A3A", sofa: "#3B4A8A", sofaBack: "#34427A",
  };
  var MONO = "JetBrains Mono, ui-monospace, Consolas, monospace";
  var SANS = "Inter, -apple-system, Segoe UI, system-ui, sans-serif";

  var CAST = {
    scout: { name: "Scout", color: "#5B9DF6" },
    analyst: { name: "Analyst", color: "#A78BFA" },
    strategist: { name: "Strategist", color: "#F5B94D" },
    boss: { name: "Boss", color: "#F0616B" },
    treasurer: { name: "Treasurer", color: "#34D399" },
  };

  // The trading board on the back wall and the wallet screen on the left wall
  // (floor units along the wall, z is height). Larger than the engine's marks so the
  // numbers stay readable; the engine only uses its own marks for walking targets.
  var BOARD = { from: 4.4, to: 12.2, zBottom: 0.22, zTop: 2.92 };
  var LINE_W = 150;
  var LINE_H = 18;
  var WALLET = { from: 5.9, to: 8.7, zBottom: 0.75, zTop: 1.95 };
  // The wallet screen's three lines: baseline below the top of the screen and size,
  // in wall pixels.
  var WALLET_TEXT = { title: { dy: 15, size: 9.5 }, balance: { dy: 33, size: 15 }, pretend: { dy: 45, size: 8.5 } };
  // The symbol on the board: left margin, font size, widest it may get (longer ones
  // are squeezed) and the gap before the direction arrow; the arrow's own width.
  var SYMBOL = { x: 14, size: 26, maxWidth: 140, gap: 7, arrowWidth: 12 };
  var TAG_GAP = 2;
  // The stop and target rows on the board, beside the in/now rows: left edge, where the
  // values start, baselines, font size and the right edge a value may reach (clear of
  // "+1 more open" and the ring), in board pixels from the board's left and top edges.
  var LEVELS = { x: 134, valueDx: 42, stopY: 68, targetY: 85, size: 10.5, right: 242 };
  // Monospace glyph width in em, a safe upper bound for the board's mono fonts.
  var MONO_EM = 0.6;

  var TEXT = {
    loading: "Reading the game data…",
    unavailable: "No game data",
    looking: "Looking for a play",
    lookingSub: "No play is open right now.",
    switchedOff: "The game is switched off.",
    beforeCosts: "so far, before costs",
    noPrice: "no recent price",
    pending: "waiting for a price",
    paused: "paused",
    toClose: "to close",
    timeLeft: "time left",
    lastPlay: "Last play: ",
    quiet: "Nobody is talking right now.",
    walletLabel: "Wallet",
    pretend: "pretend money",
  };

  // -- the board model (pure) -------------------------------------------------------------
  function str(v) { return typeof v === "string" && v.trim() ? v : null; }

  function openPlays(state) {
    return state && Array.isArray(state.open_plays) ? state.open_plays.filter(function (p) {
      return p && typeof p === "object";
    }) : [];
  }

  function price(v) {
    var n = typeof v === "string" ? Number(v) : NaN;
    return isFinite(n) && n > 0 ? n : null;
  }

  // Price-line geometry in a w x h box from the recorded mids since entry, with the
  // entry mid as a dashed reference and, when the play has them, its recorded stop
  // and target as level lines (the box then spans both, so the line shows where the
  // price sits between them). Numbers are used for the drawing only; the digits
  // shown are always the backend's strings.
  function lineGeometry(play, w, h) {
    var mids = (Array.isArray(play.price_line) ? play.price_line : []).map(function (p) {
      return p && typeof p.mid === "string" ? Number(p.mid) : NaN;
    }).filter(function (v) { return isFinite(v) && v > 0; });
    var entry = price(play.entry_mid);
    var stop = null;
    var target = null;
    if (price(play.stop) !== null && price(play.target) !== null) {
      stop = price(play.stop);
      target = price(play.target);
    }
    if (mids.length === 0) return null;
    var all = mids.concat([entry, stop, target].filter(function (v) { return v !== null; }));
    var lo = Math.min.apply(null, all);
    var hi = Math.max.apply(null, all);
    var span = hi - lo || Math.abs(hi) * 0.001 || 1;
    function y(v) { return r1(h - 3 - (v - lo) / span * (h - 6)); }
    var n = mids.length;
    return {
      points: mids.map(function (v, i) { return { x: n === 1 ? w : r1(i * w / (n - 1)), y: y(v) }; }),
      entryY: entry !== null ? y(entry) : null,
      stopY: stop !== null ? y(stop) : null,
      targetY: target !== null ? y(target) : null,
    };
  }

  // The countdown ring's text: whole hours from an hour up ("23h"), then minutes.
  function ringLeft(minutes) {
    if (minutes === null) return "—";
    return minutes >= 60 ? Math.floor(minutes / 60) + "h" : minutes + "m";
  }

  // The newest closed play, for the board while no play is open: its asset and result,
  // and its recorded close reason on a line of its own (none on a legacy close).
  function lastClose(state, game) {
    var items = state && Array.isArray(state.history) ? state.history : [];
    var h = items[0];
    if (!h || typeof h !== "object" || !str(h.asset)) return null;
    var net = game.formatMoney(h.net, str(state.currency) || "EUR", { signed: true });
    return { sub: TEXT.lastPlay + [h.asset, net].filter(Boolean).join(" · "), note: str(h.exit_reason_text) };
  }

  // The width a level value is squeezed to when its monospace width would pass
  // LEVELS.right; null when it fits as it is.
  function levelFit(text) {
    var room = LEVELS.right - LEVELS.x - LEVELS.valueDx;
    var natural = String(text || "").length * LEVELS.size * MONO_EM;
    return natural > room ? room : null;
  }

  // -- screen geometry (pure, for the tests) ---------------------------------------------
  // Rects are {x0, x1, y0, y1} in viewBox pixels; gap > 0 also rejects rects closer
  // than gap.
  function overlaps(a, b, gap) {
    var g = gap || 0;
    return a.x0 < b.x1 + g && b.x0 < a.x1 + g && a.y0 < b.y1 + g && b.y0 < a.y1 + g;
  }

  // The screen rect of a character whose feet are at screen point (px, py): head,
  // raised arm and shadow included.
  function bodyBox(px, py, seated) {
    return { x0: px - 14, x1: px + 14, y0: py - (seated ? 48 : 60), y1: py + 2 };
  }

  // A point of the left wall plane (u along the wall, v down from the floor line).
  function leftWall(u, v) { return [OX + C30 * u, OY - 0.5 * u + v]; }

  // The screen rect of the "pretend money" line of the wallet screen: its centred
  // text at a generous 0.6 em per character, from the cap height to below the baseline.
  function walletLabelBox() {
    var line = WALLET_TEXT.pretend;
    var u = -(WALLET.from + WALLET.to) / 2 * S;
    var v = -WALLET.zTop * S + line.dy;
    var half = TEXT.pretend.length * line.size * 0.6 / 2;
    var corners = [leftWall(u - half, v - line.size), leftWall(u + half, v - line.size),
      leftWall(u - half, v + line.size * 0.3), leftWall(u + half, v + line.size * 0.3)];
    var xs = corners.map(function (c) { return c[0]; });
    var ys = corners.map(function (c) { return c[1]; });
    return { x0: Math.min.apply(null, xs), x1: Math.max.apply(null, xs), y0: Math.min.apply(null, ys), y1: Math.max.apply(null, ys) };
  }

  // Where the board's symbol ends and its direction arrow goes, in board pixels from
  // the board's left edge. measured is the symbol's rendered width when the browser
  // can tell it; otherwise a monospace estimate. A symbol wider than SYMBOL.maxWidth
  // is squeezed to it, so the arrow always stays on the board, right of the symbol.
  function symbolLayout(symbol, measured) {
    var natural = typeof measured === "number" && isFinite(measured) && measured > 0
      ? measured : String(symbol || "").length * SYMBOL.size * 0.62;
    var width = Math.min(natural, SYMBOL.maxWidth);
    return { width: r1(width), squeeze: natural > SYMBOL.maxWidth, arrowX: r1(SYMBOL.x + width + SYMBOL.gap) };
  }

  // Name tags. A tag is {id, x, y, w, body}: its rect is x-34..x-34+w, y+8..y+24 at
  // offset [0, 0] (under the feet). tagOffset picks its place: the usual one, else
  // beside it, above the owner's head or a row lower, so that it covers no other
  // character, stays in the picture and keeps TAG_GAP from every tag in placed.
  function tagRect(tag, off) {
    var x0 = tag.x + off[0] - 34;
    var y0 = tag.y + off[1] + 8;
    return { x0: x0, x1: x0 + tag.w, y0: y0, y1: y0 + 16 };
  }

  function tagOffset(tag, list, placed) {
    placed = placed || [];
    var side = tag.w / 2 + 14;
    var tries = [[0, 0], [-side, 0], [side, 0], [0, -86], [0, 19], [-side, 19], [side, 19],
      [0, 38], [-side, 38], [side, 38], [0, -105], [-side, -19], [side, -19], [0, 57]];
    function coversBody(r) {
      return list.some(function (o) { return o !== tag && o.body && overlaps(r, o.body, 0); });
    }
    function touchesTag(r) {
      return placed.some(function (p) { return overlaps(r, p, TAG_GAP); });
    }
    function inView(r) { return r.x0 >= 0 && r.x1 <= VIEW_W && r.y0 >= 0 && r.y1 <= VIEW_H; }
    var i;
    for (i = 0; i < tries.length; i++) {
      var r = tagRect(tag, tries[i]);
      if (inView(r) && !coversBody(r) && !touchesTag(r)) return tries[i];
    }
    for (i = 0; i < tries.length; i++) {  // crowded: never cover a character first
      if (!coversBody(tagRect(tag, tries[i]))) return tries[i];
    }
    return [0, 0];
  }

  // Every tag's offset, by id. Tags lower on the screen are placed first and keep
  // their usual place when they can; the ones behind them move out of their way.
  function placeTagTargets(list) {
    var order = list.slice().sort(function (a, b) {
      return b.y - a.y || a.x - b.x || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
    });
    var placed = [];
    var out = {};
    order.forEach(function (tag) {
      var off = tagOffset(tag, list, placed);
      placed.push(tagRect(tag, off));
      out[tag.id] = off;
    });
    return out;
  }

  // One step of a tag's slide towards its target; reduced motion snaps.
  function easeTag(cur, target, reduced) {
    var k = reduced ? 1 : 0.2;
    var next = [cur[0] + (target[0] - cur[0]) * k, cur[1] + (target[1] - cur[1]) * k];
    if (Math.abs(next[0] - target[0]) < 0.5) next[0] = target[0];
    if (Math.abs(next[1] - target[1]) < 0.5) next[1] = target[1];
    return next;
  }

  // What the wall board shows for one get_paper_state() reading, at nowMs.

  function boardModel(state, nowMs, game) {
    var G = game;
    var view = G.selectView(state);
    if (view.kind === "loading") return { kind: "loading", title: TEXT.loading, sub: "" };
    if (view.kind === "unavailable") return { kind: "unavailable", title: TEXT.unavailable, sub: "" };
    var disabled = view.disabled;
    var plays = view.kind === "ready" ? openPlays(state) : [];
    var play = plays.length ? G.mainPlay(state) : null;
    if (!play) {
      var last = view.kind === "ready" && !disabled ? lastClose(state, G) : null;
      return {
        kind: "empty", title: TEXT.looking,
        sub: disabled ? TEXT.switchedOff : last ? last.sub : TEXT.lookingSub,
        note: last ? last.note : null,
      };
    }
    var currency = str(state.currency) || "EUR";
    var sign = G.signOf(play.gross_now);
    var tone = sign === 1 ? "up" : sign === -1 ? "down" : "flat";
    var gain = G.formatMoney(play.gross_now, currency, { signed: true });
    var short = play.direction === "SHORT";
    var levels = G.hasLevels(play);
    var due = G.dueOf(play);
    var fraction = G.timeFraction(play.opened_at, due, nowMs);
    var left = G.minutesLeft(due, nowMs);
    var pending = play.status === "PENDING_EXIT";
    var ring = disabled ? "paused" : pending ? "pending" : fraction === null ? "unknown" : "running";
    return {
      kind: "play",
      symbol: str(play.asset) || "—",
      pair: str(play.pair),
      direction: short ? "SHORT" : play.direction === "LONG" ? "LONG" : null,
      arrow: short ? "▼" : play.direction === "LONG" ? "▲" : "",
      directionText: str(play.direction_text),
      entry: G.formatPrice(short ? play.entry_bid : play.entry_ask, play.quote) || "—",
      now: G.formatPrice(short ? play.now_ask : play.now_bid, play.quote) || "—",
      gain: gain || "—",
      gainNote: gain ? TEXT.beforeCosts : TEXT.noPrice,
      tone: tone,
      remaining: fraction === null || ring !== "running" ? (ring === "pending" ? 0 : 1) : 1 - fraction,
      ringKind: ring,
      ringText: ring === "paused" ? TEXT.paused : ring === "pending" ? "wait" : ringLeft(left),
      ringNote: levels ? TEXT.timeLeft : TEXT.toClose,
      // The recorded exit levels; null (not drawn) on a play without them.
      stop: levels ? G.formatPrice(play.stop, play.quote) : null,
      target: levels ? G.formatPrice(play.target, play.quote) : null,
      more: plays.length > 1 ? "+" + (plays.length - 1) + " more open" : null,
      line: lineGeometry(play, LINE_W, LINE_H),
      dueAt: due,
      openedAt: play.opened_at,
    };
  }

  function walletModel(state, game) {
    if (game.selectView(state).kind !== "ready") return { balance: "—" };
    return { balance: game.formatMoney(state.wallet.balance, str(state.currency) || "EUR") || "—" };
  }

  // -- small SVG helpers ------------------------------------------------------------------
  function shade(hex, f) {
    var n = parseInt(hex.slice(1), 16);
    var c = [n >> 16, (n >> 8) & 255, n & 255].map(function (v) { return Math.max(0, Math.min(255, Math.round(v * f))); });
    return "#" + c.map(function (v) { return (v < 16 ? "0" : "") + v.toString(16); }).join("");
  }

  function make(doc, parent, tag, attrs, text) {
    var node = doc.createElementNS(SVG_NS, tag);
    if (attrs) Object.keys(attrs).forEach(function (k) { node.setAttribute(k, String(attrs[k])); });
    if (text !== undefined) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }

  // A shaded box: x, y floor corner, w along x, d along y, z base, h height.
  function box(doc, parent, proj, x, y, w, d, z, h, color) {
    var g = make(doc, parent, "g");
    var top = [proj(x, y, z + h), proj(x + w, y, z + h), proj(x + w, y + d, z + h), proj(x, y + d, z + h)];
    var left = [proj(x, y + d, z), proj(x + w, y + d, z), proj(x + w, y + d, z + h), proj(x, y + d, z + h)];
    var right = [proj(x + w, y, z), proj(x + w, y + d, z), proj(x + w, y + d, z + h), proj(x + w, y, z + h)];
    make(doc, g, "polygon", { points: pts(left), fill: shade(color, 0.72) });
    make(doc, g, "polygon", { points: pts(right), fill: shade(color, 0.88) });
    make(doc, g, "polygon", { points: pts(top), fill: color });
    return g;
  }

  // Wall planes: back wall local (u, v) = (x*S, -z*S); left wall (u, v) = (-y*S, -z*S).
  var BACK_MATRIX = "matrix(" + C30 + ",0.5,0,1," + OX + "," + OY + ")";
  var LEFT_MATRIX = "matrix(" + C30 + ",-0.5,0,1," + OX + "," + OY + ")";

  // -- the characters -----------------------------------------------------------------------
  // Two stand/walk variants (facing along x or along y) so the legs step along the
  // walk; eyes on the face the agent looks towards, hair when it turns its back.
  function faceDetail(doc, parent, axis, front) {
    var g = make(doc, parent, "g", { opacity: 0 });
    if (axis === "x") {
      if (front) {  // east: eyes on the +x face
        [-0.07, 0.05].forEach(function (yy) {
          make(doc, g, "polygon", { points: pts([L(0.13, yy, 1.16), L(0.13, yy + 0.04, 1.16), L(0.13, yy + 0.04, 1.11), L(0.13, yy, 1.11)]), fill: "#1B2230" });
        });
      } else {      // west: hair over the back of the head (+x face)
        make(doc, g, "polygon", { points: pts([L(0.13, -0.13, 1.28), L(0.13, 0.13, 1.28), L(0.13, 0.13, 1.06), L(0.13, -0.13, 1.06)]), fill: COLORS.hair });
      }
    } else if (front) {  // south: eyes on the +y face
      [-0.07, 0.05].forEach(function (xx) {
        make(doc, g, "polygon", { points: pts([L(xx, 0.13, 1.16), L(xx + 0.04, 0.13, 1.16), L(xx + 0.04, 0.13, 1.11), L(xx, 0.13, 1.11)]), fill: "#1B2230" });
      });
    } else {             // north: hair over the back of the head (+y face)
      make(doc, g, "polygon", { points: pts([L(-0.13, 0.13, 1.28), L(0.13, 0.13, 1.28), L(0.13, 0.13, 1.06), L(-0.13, 0.13, 1.06)]), fill: COLORS.hair });
    }
    return g;
  }

  function standVariant(doc, parent, color, axis) {
    var v = make(doc, parent, "g", { opacity: 0 });
    var legs = [];
    var arms = [];
    if (axis === "x") {
      legs.push(box(doc, make(doc, v, "g"), L, -0.08, -0.15, 0.16, 0.13, 0, 0.46, COLORS.trousers));
      legs.push(box(doc, make(doc, v, "g"), L, -0.08, 0.02, 0.16, 0.13, 0, 0.46, COLORS.trousers));
    } else {
      legs.push(box(doc, make(doc, v, "g"), L, -0.15, -0.08, 0.13, 0.16, 0, 0.46, COLORS.trousers));
      legs.push(box(doc, make(doc, v, "g"), L, 0.02, -0.08, 0.13, 0.16, 0, 0.46, COLORS.trousers));
    }
    var upper = make(doc, v, "g");
    if (axis === "x") {
      arms.push(box(doc, make(doc, upper, "g"), L, -0.06, -0.29, 0.12, 0.09, 0.5, 0.42, shade(color, 0.8)));
      box(doc, upper, L, -0.13, -0.2, 0.26, 0.4, 0.44, 0.52, color);
      arms.push(box(doc, make(doc, upper, "g"), L, -0.06, 0.2, 0.12, 0.09, 0.5, 0.42, shade(color, 0.8)));
    } else {
      arms.push(box(doc, make(doc, upper, "g"), L, -0.29, -0.06, 0.09, 0.12, 0.5, 0.42, shade(color, 0.8)));
      box(doc, upper, L, -0.2, -0.13, 0.4, 0.26, 0.44, 0.52, color);
      arms.push(box(doc, make(doc, upper, "g"), L, 0.2, -0.06, 0.09, 0.12, 0.5, 0.42, shade(color, 0.8)));
    }
    var head = make(doc, upper, "g");
    box(doc, head, L, -0.13, -0.13, 0.26, 0.26, 0.98, 0.3, COLORS.skin);
    box(doc, head, L, -0.14, -0.14, 0.28, 0.28, 1.26, 0.06, COLORS.hair);
    var faces = { front: faceDetail(doc, head, axis, true), back: faceDetail(doc, head, axis, false) };
    // Shoulders, for the speaking gesture (rotate the arm about its top).
    var shoulders = axis === "x" ? [L(0, -0.25, 0.92), L(0, 0.25, 0.92)] : [L(-0.25, 0, 0.92), L(0.25, 0, 0.92)];
    return { node: v, legs: legs, arms: arms, upper: upper, head: head, faces: faces, shoulders: shoulders };
  }

  // Seated (at a desk, or awake on the sofa): along y, back or front view.
  function sitVariant(doc, parent, color) {
    var v = make(doc, parent, "g", { opacity: 0 });
    var upper = make(doc, v, "g");
    var arms = [];
    arms.push(box(doc, make(doc, upper, "g"), L, -0.27, -0.3, 0.08, 0.34, 0.5, 0.12, shade(color, 0.8)));
    box(doc, upper, L, -0.19, -0.12, 0.38, 0.26, 0.34, 0.46, color);
    arms.push(box(doc, make(doc, upper, "g"), L, 0.19, -0.3, 0.08, 0.34, 0.5, 0.12, shade(color, 0.8)));
    var head = make(doc, upper, "g");
    box(doc, head, L, -0.13, -0.13, 0.26, 0.26, 0.82, 0.3, COLORS.skin);
    box(doc, head, L, -0.14, -0.14, 0.28, 0.28, 1.1, 0.06, COLORS.hair);
    var front = make(doc, head, "g", { opacity: 0 });
    [-0.07, 0.05].forEach(function (xx) {
      make(doc, front, "polygon", { points: pts([L(xx, 0.13, 1.0), L(xx + 0.04, 0.13, 1.0), L(xx + 0.04, 0.13, 0.95), L(xx, 0.13, 0.95)]), fill: "#1B2230" });
    });
    var back = make(doc, head, "g", { opacity: 0 });
    make(doc, back, "polygon", { points: pts([L(-0.13, 0.13, 1.12), L(0.13, 0.13, 1.12), L(0.13, 0.13, 0.9), L(-0.13, 0.13, 0.9)]), fill: COLORS.hair });
    return { node: v, upper: upper, arms: arms, head: head, faces: { front: front, back: back } };
  }

  // Lying on the sofa seat (drawn from the sofa node, shifted onto the cushions).
  function lieVariant(doc, parent, color) {
    var v = make(doc, parent, "g", { opacity: 0 });
    var body = make(doc, v, "g", { transform: "translate(" + pts([L(0, -0.95, 0)]).replace(",", " ") + ")" });
    box(doc, body, L, -0.75, -0.18, 0.4, 0.34, 0.35, 0.2, COLORS.trousers);
    box(doc, body, L, -0.35, -0.2, 0.55, 0.4, 0.35, 0.26, color);
    box(doc, body, L, 0.22, -0.14, 0.28, 0.28, 0.35, 0.28, COLORS.skin);
    var zs = [];
    var zBase = L(0.3, -0.95, 0.95);
    for (var i = 0; i < 3; i++) {
      var z = make(doc, v, "text", {
        x: r1(zBase[0] + 6), y: r1(zBase[1]), fill: COLORS.muted, "font-family": MONO, "font-weight": 700,
        "font-size": 12 + i * 3, opacity: 0,
      }, "Z");
      zs.push(z);
    }
    return { node: v, zs: zs };
  }

  // -- the view -------------------------------------------------------------------------------
  // host: an element to fill. opts: {doc, win, game (RadarPaperGame), engine (RadarPaperOffice),
  // now, setTimeout, clearTimeout} - the last three are for tests.
  function createOfficeView(host, opts) {
    opts = opts || {};
    var doc = opts.doc || host.ownerDocument;
    var win = opts.win || (typeof window !== "undefined" ? window : null);
    var G = opts.game || (win && win.RadarPaperGame);
    var E = opts.engine || (win && win.RadarPaperOffice);
    var LAYOUT = E.LAYOUT;
    var now = opts.now || function () { return Date.now(); };
    var reduced = !!(win && win.matchMedia && win.matchMedia("(prefers-reduced-motion: reduce)").matches);
    var engine = E.createOffice({
      now: now, reducedMotion: reduced, setTimeout: opts.setTimeout, clearTimeout: opts.clearTimeout,
    });
    var state;
    var set = {};  // last attribute values written per node key, to write only on change

    function put(key, node, attr, value) {
      var k = key + "|" + attr;
      if (set[k] === value) return;
      set[k] = value;
      node.setAttribute(attr, value);
    }
    function say(node, text) { if (node.textContent !== text) node.textContent = text; }

    // -- DOM skeleton --
    var stage = doc.createElement("div");
    stage.className = "pg-stage";
    var svg = make(doc, stage, "svg", {
      viewBox: "0 0 " + VIEW_W + " " + VIEW_H, role: "img", "class": "pg-iso",
      "aria-label": "The AI's office: five agents at their desks, a meeting table, a trading board on the back wall and the wallet screen on the left wall",
    });
    var bubble = doc.createElement("div");
    bubble.className = "pg-bubble";
    bubble.setAttribute("aria-hidden", "true");
    var bubbleWho = doc.createElement("b");
    var bubbleText = doc.createElement("span");
    bubble.appendChild(bubbleWho);
    bubble.appendChild(bubbleText);
    stage.appendChild(bubble);
    var say1 = doc.createElement("div");
    say1.className = "pg-say";
    var sayLive = doc.createElement("p");
    sayLive.className = "pg-say-text";
    sayLive.setAttribute("tabindex", "0");
    sayLive.setAttribute("aria-live", "polite");
    sayLive.setAttribute("aria-atomic", "true");
    sayLive.setAttribute("aria-label", "What the agents are saying");
    var sayQueue = doc.createElement("span");
    sayQueue.className = "pg-say-queue mono";
    say1.appendChild(sayLive);
    say1.appendChild(sayQueue);
    host.appendChild(stage);
    host.appendChild(say1);

    // -- room --
    var W = LAYOUT.width;
    var D = LAYOUT.depth;
    make(doc, svg, "polygon", { points: pts([P(0, 0, 0), P(W, 0, 0), P(W, D, 0), P(0, D, 0)]), fill: COLORS.floor });
    for (var gi = 0; gi <= W; gi++) {
      make(doc, svg, "line", { x1: r1(P(gi, 0, 0)[0]), y1: r1(P(gi, 0, 0)[1]), x2: r1(P(gi, D, 0)[0]), y2: r1(P(gi, D, 0)[1]), stroke: COLORS.grid, "stroke-width": 1 });
    }
    for (var gj = 0; gj <= D; gj++) {
      make(doc, svg, "line", { x1: r1(P(0, gj, 0)[0]), y1: r1(P(0, gj, 0)[1]), x2: r1(P(W, gj, 0)[0]), y2: r1(P(W, gj, 0)[1]), stroke: COLORS.grid, "stroke-width": 1 });
    }
    make(doc, svg, "polygon", { points: pts([P(0, 0, 0), P(W, 0, 0), P(W, 0, WALL_H), P(0, 0, WALL_H)]), fill: COLORS.backWall });
    make(doc, svg, "polygon", { points: pts([P(0, 0, 0), P(0, D, 0), P(0, D, WALL_H), P(0, 0, WALL_H)]), fill: COLORS.leftWall });

    // -- the wall board (back wall plane) --
    var bw = make(doc, svg, "g", { transform: BACK_MATRIX, "class": "pg-board" });
    var bu0 = BOARD.from * S;
    var bu1 = BOARD.to * S;
    var bv0 = -BOARD.zTop * S;
    var bv1 = -BOARD.zBottom * S;
    var bW = bu1 - bu0;
    var bH = bv1 - bv0;
    var frame = make(doc, bw, "rect", { x: r1(bu0), y: r1(bv0), width: r1(bW), height: r1(bH), rx: 4, fill: COLORS.screen, stroke: COLORS.info, "stroke-width": 1.5 });
    var boardPlay = make(doc, bw, "g", { opacity: 0 });
    var boardEmpty = make(doc, bw, "g", { opacity: 0 });
    var b = {};
    // Left column: symbol and arrow, pair and direction, entry and now on their own
    // lines, the price line. Right column: the gain before costs, then the ring.
    b.symbol = make(doc, boardPlay, "text", { x: r1(bu0 + SYMBOL.x), y: r1(bv0 + 30), fill: COLORS.text, "font-family": MONO, "font-size": SYMBOL.size, "font-weight": 700 }, "");
    b.arrow = make(doc, boardPlay, "text", { x: r1(bu0 + SYMBOL.x), y: r1(bv0 + 29), fill: COLORS.info, "font-family": MONO, "font-size": 18, "font-weight": 700 }, "");
    b.pair = make(doc, boardPlay, "text", { x: r1(bu0 + 14), y: r1(bv0 + 50), fill: COLORS.muted, "font-family": SANS, "font-size": 12 }, "");
    b.inRow = make(doc, boardPlay, "text", { x: r1(bu0 + 14), y: r1(bv0 + 68), fill: COLORS.muted, "font-family": MONO, "font-size": 12 }, "");
    b.inLabel = make(doc, b.inRow, "tspan", {}, "in  ");
    b.inValue = make(doc, b.inRow, "tspan", { fill: COLORS.text }, "");
    b.nowRow = make(doc, boardPlay, "text", { x: r1(bu0 + 14), y: r1(bv0 + 85), fill: COLORS.muted, "font-family": MONO, "font-size": 12 }, "");
    b.nowLabel = make(doc, b.nowRow, "tspan", {}, "now ");
    b.nowValue = make(doc, b.nowRow, "tspan", { fill: COLORS.text }, "");
    // Middle column: the recorded stop and target, beside the in/now rows.
    b.stopRow = make(doc, boardPlay, "text", { x: r1(bu0 + LEVELS.x), y: r1(bv0 + LEVELS.stopY), fill: COLORS.muted, "font-family": MONO, "font-size": LEVELS.size }, "");
    b.stopLabel = make(doc, b.stopRow, "tspan", {}, "");
    b.stopValue = make(doc, b.stopRow, "tspan", { x: r1(bu0 + LEVELS.x + LEVELS.valueDx), fill: COLORS.text }, "");
    b.targetRow = make(doc, boardPlay, "text", { x: r1(bu0 + LEVELS.x), y: r1(bv0 + LEVELS.targetY), fill: COLORS.muted, "font-family": MONO, "font-size": LEVELS.size }, "");
    b.targetLabel = make(doc, b.targetRow, "tspan", {}, "");
    b.targetValue = make(doc, b.targetRow, "tspan", { x: r1(bu0 + LEVELS.x + LEVELS.valueDx), fill: COLORS.text }, "");
    var lineBox = make(doc, boardPlay, "g", { transform: "translate(" + r1(bu0 + 14) + " " + r1(bv0 + 92) + ")" });
    b.entryLine = make(doc, lineBox, "line", { x1: 0, x2: LINE_W, y1: 13, y2: 13, stroke: COLORS.dim, "stroke-width": 1, "stroke-dasharray": "3 3", opacity: 0 });
    b.stopLine = make(doc, lineBox, "line", { x1: 0, x2: LINE_W, y1: 0, y2: 0, stroke: COLORS.muted, "stroke-width": 1, "stroke-dasharray": "1 3", opacity: 0 });
    b.targetLine = make(doc, lineBox, "line", { x1: 0, x2: LINE_W, y1: 0, y2: 0, stroke: COLORS.muted, "stroke-width": 1, "stroke-dasharray": "6 3", opacity: 0 });
    b.stopTag = make(doc, lineBox, "text", { x: LINE_W + 4, y: 0, fill: COLORS.muted, "font-family": SANS, "font-size": 8, opacity: 0 }, "stop");
    b.targetTag = make(doc, lineBox, "text", { x: LINE_W + 4, y: 0, fill: COLORS.muted, "font-family": SANS, "font-size": 8, opacity: 0 }, "target");
    b.line = make(doc, lineBox, "polyline", { points: "", fill: "none", stroke: COLORS.flat, "stroke-width": 1.6, "stroke-linejoin": "round", "stroke-linecap": "round" });
    b.dot = make(doc, lineBox, "circle", { cx: 0, cy: 0, r: 2.4, fill: COLORS.flat, opacity: 0 });
    b.gain = make(doc, boardPlay, "text", { x: r1(bu1 - 12), y: r1(bv0 + 30), fill: COLORS.flat, "font-family": MONO, "font-size": 22, "font-weight": 700, "text-anchor": "end" }, "");
    b.gainNote = make(doc, boardPlay, "text", { x: r1(bu1 - 12), y: r1(bv0 + 49), fill: COLORS.muted, "font-family": SANS, "font-size": 10.5, "text-anchor": "end" }, "");
    b.more = make(doc, boardPlay, "text", { x: r1(bu1 - 12), y: r1(bv0 + 64), fill: COLORS.muted, "font-family": SANS, "font-size": 10.5, "text-anchor": "end" }, "");
    var ringC = [bu1 - 32, bv0 + bH - 25];
    var ringR = 15;
    var ringLen = 2 * Math.PI * ringR;
    make(doc, boardPlay, "circle", { cx: r1(ringC[0]), cy: r1(ringC[1]), r: ringR, fill: "none", stroke: "#1E242D", "stroke-width": 3.5 });
    b.ring = make(doc, boardPlay, "circle", {
      cx: r1(ringC[0]), cy: r1(ringC[1]), r: ringR, fill: "none", stroke: COLORS.info, "stroke-width": 3.5,
      "stroke-dasharray": r1(ringLen) + " " + r1(ringLen), "stroke-dashoffset": 0, "stroke-linecap": "round",
      transform: "rotate(-90 " + r1(ringC[0]) + " " + r1(ringC[1]) + ")",
    });
    b.ringText = make(doc, boardPlay, "text", { x: r1(ringC[0]), y: r1(ringC[1] + 3.5), fill: COLORS.text, "font-family": MONO, "font-size": 11, "font-weight": 600, "text-anchor": "middle" }, "");
    // Below the ring's centre, so it stays clear of the target row beside the price line.
    b.ringNote = make(doc, boardPlay, "text", { x: r1(ringC[0] - ringR - 7), y: r1(ringC[1] + 12), fill: COLORS.muted, "font-family": SANS, "font-size": 10.5, "text-anchor": "end" }, TEXT.toClose);
    b.emptyTitle = make(doc, boardEmpty, "text", { x: r1(bu0 + bW / 2), y: r1(bv0 + bH / 2 - 2), fill: COLORS.text, "font-family": SANS, "font-size": 23, "font-weight": 600, "text-anchor": "middle" }, "");
    b.emptySub = make(doc, boardEmpty, "text", { x: r1(bu0 + bW / 2), y: r1(bv0 + bH / 2 + 20), fill: COLORS.muted, "font-family": SANS, "font-size": 12.5, "text-anchor": "middle" }, "");
    b.emptyNote = make(doc, boardEmpty, "text", { x: r1(bu0 + bW / 2), y: r1(bv0 + bH / 2 + 37), fill: COLORS.muted, "font-family": SANS, "font-size": 11.5, "text-anchor": "middle" }, "");

    // -- the wallet screen (left wall plane) --
    var ww = make(doc, svg, "g", { transform: LEFT_MATRIX, "class": "pg-wallet-screen" });
    var wu0 = -WALLET.to * S;
    var wv0 = -WALLET.zTop * S;
    var wW = (WALLET.to - WALLET.from) * S;
    var wH = (WALLET.zTop - WALLET.zBottom) * S;
    var walletGlow = make(doc, ww, "rect", { x: r1(wu0 - 4), y: r1(wv0 - 4), width: r1(wW + 8), height: r1(wH + 8), rx: 6, fill: "none", stroke: COLORS.up, "stroke-width": 4, opacity: 0 });
    make(doc, ww, "rect", { x: r1(wu0), y: r1(wv0), width: r1(wW), height: r1(wH), rx: 3, fill: COLORS.screen, stroke: COLORS.up, "stroke-width": 1.2 });
    var wt = WALLET_TEXT;
    make(doc, ww, "text", { x: r1(wu0 + wW / 2), y: r1(wv0 + wt.title.dy), fill: COLORS.muted, "font-family": SANS, "font-size": wt.title.size, "text-anchor": "middle" }, TEXT.walletLabel);
    var walletValue = make(doc, ww, "text", { x: r1(wu0 + wW / 2), y: r1(wv0 + wt.balance.dy), fill: COLORS.text, "font-family": MONO, "font-size": wt.balance.size, "font-weight": 700, "text-anchor": "middle" }, "—");
    make(doc, ww, "text", { x: r1(wu0 + wW / 2), y: r1(wv0 + wt.pretend.dy), fill: COLORS.warn, "font-family": SANS, "font-size": wt.pretend.size, "text-anchor": "middle" }, TEXT.pretend);

    // -- furniture and agents, depth sorted --
    var scene = make(doc, svg, "g", { "class": "pg-scene" });
    // Name tags sit above the whole scene so furniture never hides who is who.
    var tagLayer = make(doc, svg, "g", { "class": "pg-tags" });
    var objects = [];   // {key, node} static
    var screens = {};   // agent id -> desk screen polygon
    function add(key, draw) {
      var g = make(doc, null, "g");
      draw(g);
      objects.push({ key: key, node: g });
    }
    Object.keys(LAYOUT.desks).forEach(function (id) {
      var d = LAYOUT.desks[id];
      var accent = CAST[id] ? CAST[id].color : COLORS.info;
      add(d.x + d.w / 2 + d.y + d.d / 2, function (g) {
        box(doc, g, P, d.x, d.y, d.w, d.d, 0, 0.55, COLORS.desk);
        box(doc, g, P, d.x + 0.35, d.y + 0.52, 0.9, 0.18, 0.55, 0.03, "#1B2230");      // keyboard
        box(doc, g, P, d.x + 0.3, d.y + 0.12, 1.0, 0.1, 0.55, 0.5, COLORS.screen);     // monitor
        screens[id] = make(doc, g, "polygon", {
          points: pts([P(d.x + 0.37, d.y + 0.22, 0.62), P(d.x + 1.23, d.y + 0.22, 0.62), P(d.x + 1.23, d.y + 0.22, 0.98), P(d.x + 0.37, d.y + 0.22, 0.98)]),
          fill: accent, opacity: 0.5,
        });
      });
    });
    ["seat_scout", "seat_analyst", "seat_strategist", "seat_treasurer", "seat_boss"].forEach(function (name) {
      var n = LAYOUT.nodes[name];
      if (!n) return;
      add(n.x + n.y - 0.05, function (g) { box(doc, g, P, n.x - 0.22, n.y - 0.2, 0.44, 0.44, 0, 0.32, COLORS.chair); });
      add(n.x + n.y + 0.45, function (g) { box(doc, g, P, n.x - 0.22, n.y + 0.2, 0.44, 0.08, 0.32, 0.42, shade(COLORS.chair, 1.15)); });
    });
    var t = LAYOUT.table;
    add(t.x + t.w / 2 + t.y + t.d / 2, function (g) { box(doc, g, P, t.x, t.y, t.w, t.d, 0, 0.5, COLORS.table); });
    var so = LAYOUT.sofa;
    add(so.x + so.w / 2 + so.y + so.d / 2, function (g) {
      box(doc, g, P, so.x, so.y, so.w, so.d, 0, 0.35, COLORS.sofa);
      box(doc, g, P, so.x, so.y, so.w, 0.25, 0.35, 0.4, COLORS.sofaBack);
    });
    (LAYOUT.plants || []).forEach(function (pl) {
      add(pl.x + pl.w / 2 + pl.y + pl.d / 2, function (g) {
        box(doc, g, P, pl.x, pl.y, pl.w, pl.d, 0, 0.35, "#4B3A2E");
        box(doc, g, P, pl.x + 0.05, pl.y + 0.05, pl.w - 0.1, pl.d - 0.1, 0.35, 0.55, "#2E7D5B");
      });
    });

    var agents = {};
    E.AGENTS.forEach(function (a, index) {
      var cast = CAST[a.id] || { name: a.id, color: COLORS.info };
      var g = make(doc, null, "g", { "class": "pg-agent", "data-agent": a.id });
      make(doc, g, "ellipse", { cx: 0, cy: 1, rx: 13, ry: 6.5, fill: "#000", opacity: 0.28 });
      var view = {
        id: a.id, node: g, phase: index * 1.7,
        standX: standVariant(doc, g, cast.color, "x"),
        standY: standVariant(doc, g, cast.color, "y"),
        sit: sitVariant(doc, g, cast.color),
        lie: lieVariant(doc, g, cast.color),
      };
      var tag = make(doc, tagLayer, "g", { "class": "pg-tag" });
      view.tagBg = make(doc, tag, "rect", { x: -34, y: 8, width: 68, height: 16, rx: 8, fill: "#0B0E13", opacity: 0.82 });
      make(doc, tag, "circle", { cx: -25, cy: 16, r: 3, fill: cast.color });
      view.tagText = make(doc, tag, "text", { x: -19, y: 19.8, fill: COLORS.text, "font-family": SANS, "font-size": 10.5, "font-weight": 600 }, cast.name);
      view.tag = tag;
      view.tagWidths = {};
      view.name = cast.name;
      agents[a.id] = view;
    });
    var order = null;

    // -- per-frame drawing --
    var AXIS = { east: [1, 0], west: [-1, 0], south: [0, 1], north: [0, -1] };

    function show(view, key, which) {
      ["standX", "standY", "sit", "lie"].forEach(function (name) {
        put(view.id + name, view[name].node, "opacity", name === which ? "1" : "0");
      });
      return key;
    }

    function drawStand(view, f, t, motion) {
      var axis = f.facing === "east" || f.facing === "west" ? "x" : "y";
      var v = axis === "x" ? view.standX : view.standY;
      var key = view.id + (axis === "x" ? "sx" : "sy");
      show(view, key, axis === "x" ? "standX" : "standY");
      var front = f.facing === "east" || f.facing === "south";
      put(key + "f", v.faces.front, "opacity", front ? "1" : "0");
      put(key + "b", v.faces.back, "opacity", front ? "0" : "1");
      var dir = AXIS[f.facing] || [0, 1];
      var legA = "", legB = "", armA = "", armB = "", upper = "", head = "";
      if (f.pose === "walk" && motion) {
        var ph = f.stride * 2 * Math.PI / 0.9;
        var sw = Math.sin(ph) * 0.12;
        var la = L(dir[0] * sw, dir[1] * sw, Math.max(0, Math.cos(ph)) * 0.05);
        var lb = L(-dir[0] * sw, -dir[1] * sw, Math.max(0, -Math.cos(ph)) * 0.05);
        var aa = L(-dir[0] * sw * 0.8, -dir[1] * sw * 0.8, 0);
        var ab = L(dir[0] * sw * 0.8, dir[1] * sw * 0.8, 0);
        legA = "translate(" + r1(la[0]) + " " + r1(la[1]) + ")";
        legB = "translate(" + r1(lb[0]) + " " + r1(lb[1]) + ")";
        armA = "translate(" + r1(aa[0]) + " " + r1(aa[1]) + ")";
        armB = "translate(" + r1(ab[0]) + " " + r1(ab[1]) + ")";
        upper = "translate(0 " + r1(-Math.abs(Math.sin(ph)) * 1.6) + ")";
      } else if ((f.pose === "speak" || f.pose === "update") && motion) {
        var s = v.shoulders[1];
        var angle = f.pose === "update" ? -62 + Math.sin(t / 380) * 6 : -28 + Math.sin(t / 260 + view.phase) * 22;
        armB = "rotate(" + r1(angle * (axis === "x" ? 1 : -1)) + " " + r1(s[0]) + " " + r1(s[1]) + ")";
        if (f.pose === "speak") {
          var s0 = v.shoulders[0];
          armA = "rotate(" + r1((Math.sin(t / 520 + 1) * 10) * (axis === "x" ? 1 : -1)) + " " + r1(s0[0]) + " " + r1(s0[1]) + ")";
          head = "translate(" + r1(Math.sin(t / 700) * 0.6) + " " + r1(Math.sin(t / 330) * 0.5) + ")";
        }
      } else if (f.pose === "speak" || f.pose === "update") {
        var sr = v.shoulders[1];
        armB = "rotate(" + (f.pose === "update" ? -62 : -30) * (axis === "x" ? 1 : -1) + " " + r1(sr[0]) + " " + r1(sr[1]) + ")";
      } else if (motion) {  // standing idle: small weight shifts, looking around
        upper = "translate(" + r1(Math.sin(t / 1700 + view.phase) * 0.7) + " 0)";
        head = "translate(" + r1(Math.sin(t / 2300 + view.phase) * 1.1) + " 0)";
      }
      put(key + "la", v.legs[0].parentNode, "transform", legA);
      put(key + "lb", v.legs[1].parentNode, "transform", legB);
      put(key + "aa", v.arms[0].parentNode, "transform", armA);
      put(key + "ab", v.arms[1].parentNode, "transform", armB);
      put(key + "u", v.upper, "transform", upper);
      put(key + "h", v.head, "transform", head);
    }

    function drawSit(view, f, t, motion, onSofa) {
      var v = view.sit;
      var key = view.id + "si";
      show(view, key, "sit");
      var front = f.facing === "south" || f.facing === "east";
      put(key + "f", v.faces.front, "opacity", front ? "1" : "0");
      put(key + "b", v.faces.back, "opacity", front ? "0" : "1");
      var off = onSofa ? L(0, -0.75, 0.02) : [0, 0];
      put(key + "n", v.node, "transform", onSofa ? "translate(" + r1(off[0]) + " " + r1(off[1]) + ")" : "");
      var armA = "", armB = "", upper = "", head = "";
      if (f.pose === "type" && motion) {
        var k = Math.floor(t / 110);
        armA = "translate(0 " + ((k % 2) ? -1.4 : 0) + ")";
        armB = "translate(0 " + ((k % 3) === 1 ? -1.4 : 0) + ")";
        head = "translate(0 " + r1(Math.sin(t / 900) * 0.4) + ")";
      } else if (f.pose === "wake" && motion) {
        head = "translate(" + r1(Math.sin(t / 240) * 1.2) + " 0)";   // looking around, waking up
      } else if (motion) {  // seated idle
        upper = "translate(0 " + r1(Math.sin(t / 1400 + view.phase) * 0.6) + ")";
        head = "translate(" + r1(Math.sin(t / 2600 + view.phase) * 1.2) + " 0)";
      }
      put(key + "aa", v.arms[0].parentNode, "transform", armA);
      put(key + "ab", v.arms[1].parentNode, "transform", armB);
      put(key + "u", v.upper, "transform", upper);
      put(key + "h", v.head, "transform", head);
    }

    function drawLie(view, f, t, motion) {
      var v = view.lie;
      var key = view.id + "li";
      show(view, key, "lie");
      v.zs.forEach(function (z, i) {
        var op = "0";
        var tr = "";
        if (f.zzz) {
          var ph = motion ? ((t / 1000) * 0.45 + i / 3) % 1 : [0.15, 0.45, 0.75][i];
          tr = "translate(" + r1(ph * 12) + " " + r1(-ph * 26) + ")";
          op = motion ? String(r1(Math.sin(ph * Math.PI) * 0.9)) : "0.8";
        }
        put(key + "z" + i, z, "transform", tr);
        put(key + "zo" + i, z, "opacity", op);
      });
    }

    function drawAgent(f, t) {
      var view = agents[f.id];
      if (!view) return;
      var p = P(f.x, f.y, 0);
      put(f.id + "pos", view.node, "transform", "translate(" + r1(p[0]) + " " + r1(p[1]) + ")");
      var motion = f.motion !== false && !reduced;
      var onSofa = f.node === "sofa";
      if (f.pose === "sleep" || f.pose === "lie") drawLie(view, f, t, motion);
      else if (f.pose === "sit" || f.pose === "type" || f.pose === "wake") drawSit(view, f, t, motion, onSofa);
      else drawStand(view, f, t, motion);
      var lying = f.pose === "sleep" || f.pose === "lie";
      var label = view.name + (f.pose === "sleep" ? " · asleep" : "");
      say(view.tagText, label);
      var w = tagWidth(view, label);
      put(f.id + "tagw", view.tagBg, "width", String(w));
      var tp = lying || onSofa ? L(0, -0.4, 0) : [0, 0];
      var seated = lying || f.pose === "sit" || f.pose === "type" || f.pose === "wake";
      tags.push({ view: view, id: f.id, x: p[0] + tp[0], y: p[1] + tp[1], w: w, body: bodyBox(p[0], p[1], seated) });
      var typing = f.pose === "type";
      var scr = screens[f.id];
      if (scr) {
        var flick = typing ? (motion ? 0.45 + ((Math.floor(t / 90) * 7919) % 5) * 0.1 : 0.9) : 0.5;
        put(f.id + "scr", scr, "opacity", String(r1(flick)));
      }
    }

    // Agents interleaved with the furniture by depth (x + y of the floor point).
    function depthOrder(frameAgents) {
      var list = objects.slice();
      frameAgents.forEach(function (f) {
        if (agents[f.id]) list.push({ key: f.x + f.y + (f.pose === "sleep" || f.pose === "lie" || f.node === "sofa" ? 0.2 : 0.01), node: agents[f.id].node, agent: f.id });
      });
      list.sort(function (a, b) { return a.key - b.key; });
      var sig = list.map(function (o) { return o.agent || "."; }).join("");
      if (sig === order) return;
      order = sig;
      list.forEach(function (o) { scene.appendChild(o.node); });
    }

    // Name tags sit on the top layer so furniture never hides them, which means a tag
    // can cover a character standing just below its owner, or another tag (agents at
    // the table). Such a tag slides sideways, above its owner's head or a row lower
    // until it clears every other character and tag; the slide eases so a passing
    // walker never makes it jump.
    var tags = [];
    var tagShift = {};
    // The tag's width from its rendered text when the browser can measure it (not
    // while the tab is hidden), else an estimate; measured widths are kept per label.
    function tagWidth(view, label) {
      var known = view.tagWidths[label];
      if (known) return known;
      var len = typeof view.tagText.getComputedTextLength === "function" ? view.tagText.getComputedTextLength() : 0;
      if (isFinite(len) && len > 0) {
        view.tagWidths[label] = Math.ceil(24 + len);
        return view.tagWidths[label];
      }
      return Math.round(24 + label.length * 5.5);
    }
    function placeTags() {
      var targets = placeTagTargets(tags);
      tags.forEach(function (t) {
        var cur = easeTag(tagShift[t.id] || [0, 0], targets[t.id], reduced);
        tagShift[t.id] = cur;
        put(t.id + "tag", t.view.tag, "transform", "translate(" + r1(t.x + cur[0]) + " " + r1(t.y + cur[1]) + ")");
      });
      tags = [];
    }

    var bubbleId = null;
    var lastBubbleText = null;
    function drawBubble(fr) {
      var bb = fr.bubble;
      if (!bb) {
        if (bubbleId !== null) {
          bubbleId = null;
          bubble.className = "pg-bubble";
        }
        say(sayQueue, fr.queued > 0 ? fr.queued + " more to say" : "");
        if (lastBubbleText !== null) { lastBubbleText = null; say(sayLive, ""); }
        return;
      }
      say(sayQueue, fr.queued > 0 ? fr.queued + " more to say" : "");
      if (bb.id === bubbleId) return;
      bubbleId = bb.id;
      var who = CAST[bb.agent] ? CAST[bb.agent].name : "";
      var f = null;
      fr.agents.forEach(function (a) { if (a.id === bb.agent) f = a; });
      // At the wallet screen a bubble above the head would cover the balance, so it
      // goes to the right of the speaker instead, hanging down from head height so it
      // stays clear of the screen's "pretend money" line.
      var side = !!(f && f.node === "wallet");
      var p = f ? P(f.x, f.y, side ? 1.1 : 1.55) : P(8, 6, 1.5);
      var left = side ? p[0] / VIEW_W * 100 : Math.max(14, Math.min(86, p[0] / VIEW_W * 100));
      var top = side ? p[1] / VIEW_H * 100 : Math.max(18, p[1] / VIEW_H * 100);
      bubble.style.left = r1(left) + "%";
      bubble.style.top = r1(top) + "%";
      bubble.style.setProperty("--pg-agent", CAST[bb.agent] ? CAST[bb.agent].color : COLORS.info);
      say(bubbleWho, who);
      say(bubbleText, typeof bb.text === "string" ? bb.text : "");
      bubble.className = side ? "pg-bubble side on" : "pg-bubble on";
      var line = (who ? who + ": " : "") + (typeof bb.text === "string" ? bb.text : "");
      lastBubbleText = line;
      say(sayLive, line);
    }

    // -- board and wallet screen --
    var board = null;
    var symbolWidth = null;
    function drawBoard(t) {
      var m = boardModel(state, t, G);
      var isPlay = m.kind === "play";
      put("bp", boardPlay, "opacity", isPlay ? "1" : "0");
      put("be", boardEmpty, "opacity", isPlay ? "0" : "1");
      if (!isPlay) {
        say(b.emptyTitle, m.title);
        say(b.emptySub, m.sub || "");
        say(b.emptyNote, m.note || "");
        put("bf", frame, "stroke", COLORS.info);
        board = m;
        return;
      }
      var tone = COLORS[m.tone];
      if (b.symbol.textContent !== m.symbol || symbolWidth === null) {
        // Measure the symbol unsqueezed; 0 or no measurement (hidden tab) retries later.
        if (b.symbol.removeAttribute) { b.symbol.removeAttribute("textLength"); b.symbol.removeAttribute("lengthAdjust"); }
        delete set["bs|textLength"];
        delete set["bsa|lengthAdjust"];
        say(b.symbol, m.symbol);
        var len = typeof b.symbol.getComputedTextLength === "function" ? b.symbol.getComputedTextLength() : 0;
        symbolWidth = isFinite(len) && len > 0 ? len : null;
      }
      var sl = symbolLayout(m.symbol, symbolWidth);
      if (sl.squeeze) {
        put("bs", b.symbol, "textLength", String(sl.width));
        put("bsa", b.symbol, "lengthAdjust", "spacingAndGlyphs");
      }
      put("ba", b.arrow, "x", String(r1(bu0 + sl.arrowX)));
      say(b.arrow, m.arrow);
      put("bac", b.arrow, "fill", COLORS.info);
      say(b.pair, [m.pair, m.directionText].filter(Boolean).join(" · "));
      say(b.inValue, m.entry);
      say(b.nowValue, m.now);
      say(b.gain, m.gain);
      put("bgc", b.gain, "fill", tone);
      say(b.gainNote, m.gainNote);
      say(b.more, m.more || "");
      put("bf", frame, "stroke", m.tone === "up" ? COLORS.up : m.tone === "down" ? COLORS.down : COLORS.info);
      say(b.stopLabel, m.stop ? "stop" : "");
      say(b.stopValue, m.stop || "");
      fitLevel("bsv", b.stopValue, m.stop);
      say(b.targetLabel, m.target ? "target" : "");
      say(b.targetValue, m.target || "");
      fitLevel("btv", b.targetValue, m.target);
      say(b.ringNote, m.ringNote);
      var levelLines = [["s", b.stopLine, b.stopTag, m.line && m.line.stopY], ["t", b.targetLine, b.targetTag, m.line && m.line.targetY]];
      levelLines.forEach(function (lv) {
        var shown = typeof lv[3] === "number";
        put("lv" + lv[0] + "o", lv[1], "opacity", shown ? "1" : "0");
        put("lv" + lv[0] + "to", lv[2], "opacity", shown ? "1" : "0");
        if (!shown) return;
        put("lv" + lv[0] + "1", lv[1], "y1", String(lv[3]));
        put("lv" + lv[0] + "2", lv[1], "y2", String(lv[3]));
        put("lv" + lv[0] + "ty", lv[2], "y", String(r1(lv[3] + 2.8)));
      });
      if (m.line) {
        var last = m.line.points[m.line.points.length - 1];
        put("bl", b.line, "points", m.line.points.map(function (q) { return q.x + "," + q.y; }).join(" "));
        put("blc", b.line, "stroke", tone);
        put("bd", b.dot, "transform", "translate(" + last.x + " " + last.y + ")");
        put("bdc", b.dot, "fill", tone);
        put("bdo", b.dot, "opacity", "1");
        put("be1", b.entryLine, "y1", String(m.line.entryY === null ? 13 : m.line.entryY));
        put("be2", b.entryLine, "y2", String(m.line.entryY === null ? 13 : m.line.entryY));
        put("beo", b.entryLine, "opacity", m.line.entryY === null ? "0" : "1");
      } else {
        put("bl", b.line, "points", "");
        put("bdo", b.dot, "opacity", "0");
        put("beo", b.entryLine, "opacity", "0");
      }
      put("rc", b.ring, "stroke", m.ringKind === "pending" ? COLORS.warn : m.ringKind === "running" ? COLORS.info : COLORS.dim);
      put("ro", b.ring, "stroke-dashoffset", String(r1(ringLen * (1 - Math.max(0, Math.min(1, m.remaining))))));
      say(b.ringText, m.ringText);
      board = m;
    }

    // A level value too wide for its slot is squeezed to it; otherwise it keeps its width.
    function fitLevel(key, node, text) {
      var fit = levelFit(text);
      if (fit === null) {
        if (set[key + "|textLength"] !== undefined && node.removeAttribute) {
          node.removeAttribute("textLength");
          node.removeAttribute("lengthAdjust");
        }
        delete set[key + "|textLength"];
        delete set[key + "a|lengthAdjust"];
        return;
      }
      put(key, node, "textLength", String(r1(fit)));
      put(key + "a", node, "lengthAdjust", "spacingAndGlyphs");
    }

    function drawWallet(fr) {
      say(walletValue, walletModel(state, G).balance);
      var on = fr.screen && fr.screen.updating;
      var op = on ? (reduced ? 0.7 : r1(0.45 + Math.sin(fr.t / 160) * 0.35)) : 0;
      put("wg", walletGlow, "opacity", String(op));
    }

    function draw(t) {
      var fr = engine.frame(t);
      depthOrder(fr.agents);
      fr.agents.forEach(function (f) { drawAgent(f, fr.t); });
      placeTags();
      drawBubble(fr);
      drawBoard(fr.t);
      drawWallet(fr);
      return fr;
    }

    // -- loop and visibility --
    var raf = null;
    var onScreen = true;
    var destroyed = false;
    function pageShown() { return !(doc.hidden === true); }
    function running() { return !destroyed && onScreen && pageShown(); }
    function tick() {
      raf = null;
      if (!running()) return;
      draw(now());
      raf = win.requestAnimationFrame(tick);
    }
    function sync() {
      var visible = running();
      engine.setPageVisible(visible);
      if (visible && raf === null && win && win.requestAnimationFrame) raf = win.requestAnimationFrame(tick);
      if (!visible && raf !== null) { win.cancelAnimationFrame(raf); raf = null; }
    }
    var io = null;
    if (win && typeof win.IntersectionObserver === "function") {
      io = new win.IntersectionObserver(function (entries) {
        entries.forEach(function (entry) { onScreen = entry.isIntersecting; });
        sync();
      });
      io.observe(stage);
    }
    function onVisibility() { sync(); }
    if (doc.addEventListener) doc.addEventListener("visibilitychange", onVisibility);
    var mq = win && win.matchMedia ? win.matchMedia("(prefers-reduced-motion: reduce)") : null;
    function onMotion(e) {
      reduced = !!e.matches;
      engine.setReducedMotion(reduced);
      if (!running()) draw(now());
    }
    if (mq) {
      if (mq.addEventListener) mq.addEventListener("change", onMotion);
      else if (mq.addListener) mq.addListener(onMotion);
    }
    engine.mount();
    draw(now());
    sync();

    return {
      // One get_paper_state() reading; the engine turns new activity into scenes.
      update: function (next) {
        state = next;
        if (next && typeof next === "object" && next.available === true) engine.ingest(next.activity);
        if (!running()) draw(now());
      },
      noteDisconnect: function () { engine.noteDisconnect(); },
      frame: function () { return draw(now()); },
      board: function () { return board; },
      destroy: function () {
        destroyed = true;
        if (raf !== null) { win.cancelAnimationFrame(raf); raf = null; }
        if (io) io.disconnect();
        if (doc.removeEventListener) doc.removeEventListener("visibilitychange", onVisibility);
        if (mq) {
          if (mq.removeEventListener) mq.removeEventListener("change", onMotion);
          else if (mq.removeListener) mq.removeListener(onMotion);
        }
        engine.unmount();
        if (stage.parentNode) stage.parentNode.removeChild(stage);
        if (say1.parentNode) say1.parentNode.removeChild(say1);
      },
    };
  }

  return {
    TEXT: TEXT,
    CAST: CAST,
    BOARD: BOARD,
    WALLET: WALLET,
    SYMBOL: SYMBOL,
    LEVELS: LEVELS,
    TAG_GAP: TAG_GAP,
    tagOffset: tagOffset,
    tagRect: tagRect,
    placeTagTargets: placeTagTargets,
    easeTag: easeTag,
    bodyBox: bodyBox,
    walletLabelBox: walletLabelBox,
    symbolLayout: symbolLayout,
    VIEW: { width: VIEW_W, height: VIEW_H, unit: S, originX: OX, originY: OY, wallHeight: WALL_H },
    project: P,
    boardModel: boardModel,
    walletModel: walletModel,
    lineGeometry: lineGeometry,
    ringLeft: ringLeft,
    lastClose: lastClose,
    levelFit: levelFit,
    createOfficeView: createOfficeView,
  };
});
