/* AI Game (paper game) - the office choreography engine (DESIGN.md "AI Game
 * (paper game) - scoped direction": what triggers movement, never invented).
 *
 * UMD like paper_game.js, and no DOM: it turns the real activity entries of
 * Api.get_paper_state().activity (ui/paper_reader.py) into a deterministic
 * scene plan that the office renderer plays. Tested under Node by
 * tests/ui_tests/js/test_paper_office_engine.mjs.
 *
 * Honesty rule: every action in the plan and every speech bubble carries the id
 * of the activity entry that caused it; with no new entry the agents stay at
 * their resting pose (seated idle, the Boss asleep on the sofa). Bubble text is
 * the entry's text, verbatim.
 *
 * Time is an injected clock (opts.now, milliseconds). The plan is absolute
 * times on that clock, so frame(t) is a pure lookup the renderer calls from its
 * requestAnimationFrame loop. The only timer is an optional single wake-up at
 * the next plan boundary (onChange), armed while the office is mounted and the
 * page visible.
 *
 * Floor coordinates follow the mock (docs/design/mocks/paper-game-office.html):
 * x runs along the back wall (screen right-down), y along the left wall
 * (screen left-down), 16 x 10 units. Facing names use those axes: "east" = +x,
 * "west" = -x, "south" = +y (towards the viewer), "north" = -y (towards the
 * back wall).
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.RadarPaperOffice = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // -- timing (milliseconds) and speed ---------------------------------------------------
  var TIMING = {
    typeMs: 6000,      // the Scout's reaction to a finished radar cycle
    bubbleMs: 5000,    // each speech bubble is readable for at least 4 s
    lingerMs: 1500,    // stays at the table after speaking, before walking back
    wakeMs: 1500,      // the Boss waking up on the sofa
    settleMs: 600,     // sitting down / lying down after the walk back
    walkSpeed: 1.4,    // floor units per second at cruising speed
  };
  var MIN_BUBBLE_MS = 4000;
  var BACKLOG_LIMIT = 5;
  var SEEN_CAP = 2000;

  // -- the office floor ------------------------------------------------------------------
  function rect(x, y, w, d) { return { x: x, y: y, w: w, d: d }; }

  var LAYOUT = {
    width: 16,
    depth: 10,
    wallHeight: 2.2,
    desks: {
      scout: rect(1.5, 1.2, 1.6, 0.9),
      analyst: rect(4.0, 1.2, 1.6, 0.9),
      strategist: rect(11.3, 1.2, 1.6, 0.9),
      treasurer: rect(2.2, 6.4, 1.6, 0.9),
      boss: rect(13.2, 6.2, 1.6, 0.9),
    },
    table: rect(6.0, 5.3, 2.4, 1.6),
    sofa: rect(13.4, 2.6, 2.0, 0.8),
    plants: [rect(15.0, 0.4, 0.5, 0.5), rect(0.4, 4.6, 0.5, 0.5)],
    // On the walls (x = 0 is the left wall, y = 0 the back wall); z is height.
    board: { wall: "back", from: 5.5, to: 10.5, zBottom: 0.7, zTop: 1.95 },
    walletScreen: { wall: "left", from: 6.0, to: 8.6, zBottom: 0.7, zTop: 1.8 },
    // Walkable graph. `face` is where an agent standing or sitting there looks.
    nodes: {
      seat_scout: { x: 2.3, y: 2.5, face: "north" },
      seat_analyst: { x: 4.8, y: 2.5, face: "north" },
      seat_strategist: { x: 12.1, y: 2.5, face: "north" },
      seat_treasurer: { x: 3.0, y: 7.8, face: "north" },
      seat_boss: { x: 14.0, y: 7.5, face: "north" },
      sofa: { x: 14.4, y: 4.0, face: "south" },
      hall_a: { x: 2.3, y: 3.7, face: "south" },
      hall_b: { x: 4.8, y: 3.7, face: "south" },
      hall_c: { x: 7.2, y: 3.7, face: "south" },
      hall_d: { x: 12.1, y: 3.7, face: "south" },
      table_n: { x: 7.2, y: 4.8, face: "south" },
      table_w: { x: 5.5, y: 6.1, face: "east" },
      table_e: { x: 8.9, y: 6.1, face: "west" },
      table_s: { x: 7.2, y: 7.4, face: "north" },
      corner_nw: { x: 5.5, y: 4.8, face: "south" },
      corner_ne: { x: 8.9, y: 4.8, face: "south" },
      corner_sw: { x: 5.5, y: 7.4, face: "north" },
      corner_se: { x: 8.9, y: 7.4, face: "north" },
      // Just in front of the wallet screen's right end, so the Treasurer never stands
      // over its "pretend money" line.
      wallet: { x: 0.8, y: 9.0, face: "west" },
    },
    edges: [
      ["seat_scout", "hall_a"], ["hall_a", "hall_b"], ["seat_analyst", "hall_b"],
      ["hall_b", "hall_c"], ["hall_c", "hall_d"], ["seat_strategist", "hall_d"],
      ["hall_d", "sofa"], ["hall_b", "corner_nw"], ["hall_c", "table_n"], ["hall_d", "corner_ne"],
      ["corner_nw", "table_n"], ["table_n", "corner_ne"], ["corner_ne", "table_e"], ["table_e", "corner_se"],
      ["corner_se", "table_s"], ["table_s", "corner_sw"], ["corner_sw", "table_w"], ["table_w", "corner_nw"],
      ["seat_treasurer", "corner_sw"], ["seat_treasurer", "wallet"], ["corner_se", "seat_boss"],
    ],
  };

  var AGENTS = [
    { id: "scout", home: "seat_scout", rest: "sit" },
    { id: "analyst", home: "seat_analyst", rest: "sit" },
    { id: "strategist", home: "seat_strategist", rest: "sit" },
    { id: "boss", home: "sofa", rest: "sleep" },
    { id: "treasurer", home: "seat_treasurer", rest: "sit" },
  ];

  // Which agent reacts to each activity kind and where it goes (null = at its desk).
  // `sonnet`/`fable` are the router's decision on a real alert, not model calls.
  var KINDS = {
    cycle: { agent: "scout", spot: null },
    alert: { agent: "scout", spot: "table_w" },
    qwen: { agent: "analyst", spot: "table_n" },
    sonnet: { agent: "strategist", spot: "table_e" },
    fable: { agent: "boss", spot: "table_s" },
    play_open: { agent: "treasurer", spot: "wallet" },
    play_close: { agent: "treasurer", spot: "wallet" },
  };

  // -- graph -----------------------------------------------------------------------------
  function distance(a, b) {
    var dx = b.x - a.x;
    var dy = b.y - a.y;
    return Math.sqrt(dx * dx + dy * dy);
  }

  function neighbours(layout) {
    var adj = {};
    Object.keys(layout.nodes).forEach(function (name) { adj[name] = []; });
    layout.edges.forEach(function (edge) {
      adj[edge[0]].push(edge[1]);
      adj[edge[1]].push(edge[0]);
    });
    Object.keys(adj).forEach(function (name) { adj[name].sort(); });
    return adj;
  }

  function isNeighbour(layout, a, b) {
    return layout.edges.some(function (edge) {
      return (edge[0] === a && edge[1] === b) || (edge[0] === b && edge[1] === a);
    });
  }

  // Shortest path by floor distance (Dijkstra; ties broken by node name so the
  // plan is deterministic). Returns node names from `from` to `to`, or null.
  function shortestPath(layout, from, to) {
    var nodes = layout.nodes;
    if (!nodes[from] || !nodes[to]) return null;
    if (from === to) return [from];
    var adj = neighbours(layout);
    var dist = {};
    var prev = {};
    var done = {};
    Object.keys(nodes).forEach(function (name) { dist[name] = Infinity; });
    dist[from] = 0;
    for (;;) {
      var current = null;
      Object.keys(nodes).sort().forEach(function (name) {
        if (!done[name] && dist[name] < Infinity && (current === null || dist[name] < dist[current])) current = name;
      });
      if (current === null) return null;
      if (current === to) break;
      done[current] = true;
      adj[current].forEach(function (next) {
        var d = dist[current] + distance(nodes[current], nodes[next]);
        if (d < dist[next] - 1e-9) { dist[next] = d; prev[next] = current; }
      });
    }
    var path = [to];
    while (path[0] !== from) path.unshift(prev[path[0]]);
    return path;
  }

  function facingOf(dx, dy) {
    if (Math.abs(dx) >= Math.abs(dy)) return dx >= 0 ? "east" : "west";
    return dy >= 0 ? "south" : "north";
  }

  // -- easing ----------------------------------------------------------------------------
  // The walk starts with an ease-in segment and ends with an ease-out one; the
  // segments between run at cruising speed. Ease durations are doubled so the
  // speed is continuous at the joins (the quadratic reaches 2x its mean speed).
  var EASE = {
    "in": function (u) { return u * u; },
    "out": function (u) { return 1 - (1 - u) * (1 - u); },
    "in-out": function (u) { return u < 0.5 ? 2 * u * u : 1 - 2 * (1 - u) * (1 - u); },
    linear: function (u) { return u; },
  };

  // A walk along `path` (node names) starting at t0.
  function buildWalk(layout, path, t0, timing, reducedMotion) {
    var nodes = layout.nodes;
    var speed = timing.walkSpeed / 1000; // units per ms
    var count = path.length - 1;
    var waypoints = path.map(function (name) { return { node: name, x: nodes[name].x, y: nodes[name].y }; });
    var segments = [];
    var t = t0;
    var travelled = 0;
    for (var i = 0; i < count; i++) {
      var a = waypoints[i];
      var b = waypoints[i + 1];
      var len = distance(a, b);
      var ease = count === 1 ? "in-out" : i === 0 ? "in" : i === count - 1 ? "out" : "linear";
      var duration = reducedMotion ? 0 : (ease === "linear" ? len / speed : 2 * len / speed);
      segments.push({
        from: a.node, to: b.node,
        x0: a.x, y0: a.y, x1: b.x, y1: b.y,
        length: len, offset: travelled,
        facing: facingOf(b.x - a.x, b.y - a.y),
        ease: ease, t0: t, t1: t + duration, duration: duration,
      });
      travelled += len;
      t += duration;
    }
    return { waypoints: waypoints, segments: segments, length: travelled, t0: t0, t1: t };
  }

  // -- helpers ---------------------------------------------------------------------------
  function agentById(id) {
    for (var i = 0; i < AGENTS.length; i++) if (AGENTS[i].id === id) return AGENTS[i];
    return null;
  }

  function validEntry(entry) {
    if (!entry || typeof entry !== "object") return false;
    if (typeof entry.id !== "string" || entry.id === "") return false;
    var kind = Object.prototype.hasOwnProperty.call(KINDS, entry.kind) ? KINDS[entry.kind] : null;
    if (kind === null) return false;
    if (entry.agent !== undefined && entry.agent !== null && entry.agent !== kind.agent) return false;
    if (kind.spot !== null && (typeof entry.text !== "string" || entry.text.trim() === "")) return false;
    return true;
  }

  function order(a, b) {
    var ta = typeof a.ts === "string" ? a.ts : "";
    var tb = typeof b.ts === "string" ? b.ts : "";
    if (ta !== tb) return ta < tb ? -1 : 1;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  }

  function copy(value) { return JSON.parse(JSON.stringify(value)); }

  // -- the office ------------------------------------------------------------------------
  function createOffice(opts) {
    opts = opts || {};
    var layout = opts.layout || LAYOUT;
    var timing = {};
    Object.keys(TIMING).forEach(function (key) {
      timing[key] = opts.timing && typeof opts.timing[key] === "number" ? opts.timing[key] : TIMING[key];
    });
    if (timing.bubbleMs < MIN_BUBBLE_MS) timing.bubbleMs = MIN_BUBBLE_MS;
    var now = opts.now || function () { return Date.now(); };
    var setT = opts.setTimeout || function (fn, ms) { return setTimeout(fn, ms); };
    var clearT = opts.clearTimeout || function (id) { clearTimeout(id); };
    var onChange = opts.onChange || null;
    var reducedMotion = !!opts.reducedMotion;

    var seen = new Map();        // entry id -> true, insertion ordered (capped)
    var baselined = false;
    var interrupted = false;     // hidden, unmounted or disconnected since the last ingest
    var pending = null;          // latest activity received while not visible
    var mounted = false;
    var pageVisible = true;
    var timer = null;
    var bubbles = [];
    var bubbleFree = -Infinity;
    var actions = {};
    AGENTS.forEach(function (agent) { actions[agent.id] = []; });

    function visible() { return mounted && pageVisible; }

    function remember(id) {
      if (seen.has(id)) return;
      seen.set(id, true);
      if (seen.size > SEEN_CAP) seen.delete(seen.keys().next().value);
    }

    // Where the agent is when its last planned action ends, and when that is.
    function lastState(agentId) {
      var list = actions[agentId];
      var agent = agentById(agentId);
      if (list.length === 0) return { node: agent.home, free: -Infinity, asleep: agent.rest === "sleep" };
      var last = list[list.length - 1];
      return { node: last.node, free: last.t1, asleep: last.node === agent.home && agent.rest === "sleep" && last.type === "settle" };
    }

    function push(agentId, action) { actions[agentId].push(action); return action; }

    function walkTo(agentId, fromNode, toNode, t0, entryId, returning) {
      if (fromNode === toNode) return t0;
      var path = shortestPath(layout, fromNode, toNode);
      var walk = buildWalk(layout, path, t0, timing, reducedMotion);
      push(agentId, {
        type: "walk", pose: "walk", node: toNode, from: fromNode,
        waypoints: walk.waypoints, segments: walk.segments, length: walk.length,
        t0: walk.t0, t1: walk.t1, entry: entryId, returning: returning,
      });
      return walk.t1;
    }

    // A walk back home that has not started yet is dropped when the same agent
    // has somewhere else to be; one that already started is finished (no teleport).
    // Only a trailing return is dropped: anything planned after it starts at home.
    function cancelPendingReturn(agentId, t) {
      var list = actions[agentId];
      while (list.length > 0 && list[list.length - 1].returning && list[list.length - 1].t0 > t) list.pop();
    }

    function schedule(entry, t) {
      var kind = KINDS[entry.kind];
      var agentId = kind.agent;
      var agent = agentById(agentId);
      if (kind.spot === null) {
        var state = lastState(agentId);
        var start = Math.max(t, state.free);
        push(agentId, { type: "type", pose: "type", node: agent.home, t0: start, t1: start + timing.typeMs, entry: entry.id, returning: false });
        return;
      }
      cancelPendingReturn(agentId, t);
      var st = lastState(agentId);
      var clock = Math.max(t, st.free);
      if (st.asleep) {
        var wakeMs = reducedMotion ? 0 : timing.wakeMs;
        push(agentId, { type: "wake", pose: "wake", node: agent.home, t0: clock, t1: clock + wakeMs, entry: entry.id, returning: false });
        clock += wakeMs;
      }
      clock = walkTo(agentId, st.node, kind.spot, clock, entry.id, false);
      var speakAt = Math.max(clock, bubbleFree);
      if (speakAt > clock) {
        push(agentId, { type: "wait", pose: "stand", node: kind.spot, t0: clock, t1: speakAt, entry: entry.id, returning: false });
      }
      var speakEnd = speakAt + timing.bubbleMs;
      var pose = kind.spot === "wallet" ? "update" : "speak";
      push(agentId, { type: pose, pose: pose, node: kind.spot, t0: speakAt, t1: speakEnd, entry: entry.id, returning: false });
      bubbles.push({ id: entry.id, agent: agentId, kind: entry.kind, text: entry.text, start: speakAt, end: speakEnd });
      bubbleFree = speakEnd;
      push(agentId, { type: "linger", pose: "stand", node: kind.spot, t0: speakEnd, t1: speakEnd + timing.lingerMs, entry: entry.id, returning: true });
      var back = walkTo(agentId, kind.spot, agent.home, speakEnd + timing.lingerMs, entry.id, true);
      var settleMs = reducedMotion ? 0 : timing.settleMs;
      push(agentId, {
        type: "settle", pose: agent.rest === "sleep" ? "lie" : "sit", node: agent.home,
        t0: back, t1: back + settleMs, entry: entry.id, returning: true,
      });
    }

    function prune(t) {
      AGENTS.forEach(function (agent) {
        var list = actions[agent.id];
        while (list.length > 1 && list[0].t1 < t) list.shift();
      });
      while (bubbles.length > 0 && bubbles[0].end < t) bubbles.shift();
    }

    function process(activity) {
      var t = now();
      var list = Array.isArray(activity) ? activity : [];
      if (!baselined) {
        list.forEach(function (entry) { if (entry && typeof entry.id === "string") remember(entry.id); });
        baselined = true;
        interrupted = false;
        return [];
      }
      var fresh = [];
      list.forEach(function (entry) {
        if (!entry || typeof entry.id !== "string" || seen.has(entry.id)) return;
        remember(entry.id);
        if (validEntry(entry)) fresh.push(entry);
      });
      fresh.sort(order);
      if (interrupted && fresh.length > BACKLOG_LIMIT) {
        var newest = {};
        fresh.forEach(function (entry) { newest[KINDS[entry.kind].agent] = entry; });
        fresh = fresh.filter(function (entry) { return newest[KINDS[entry.kind].agent] === entry; });
      }
      interrupted = false;
      prune(t);
      fresh.forEach(function (entry) { schedule(entry, t); });
      arm();
      return fresh.map(function (entry) { return entry.id; });
    }

    // -- frame -------------------------------------------------------------------------
    function restFrame(agent, node, t) {
      var n = layout.nodes[node];
      var pose = node === agent.home ? agent.rest : "stand";
      return { id: agent.id, x: n.x, y: n.y, facing: n.face, pose: pose, node: node, entry: null, stride: 0, progress: 0 };
    }

    function agentFrame(agent, t) {
      var list = actions[agent.id];
      var before = null;
      for (var i = 0; i < list.length; i++) {
        var action = list[i];
        if (t < action.t0) break;
        before = action;
        if (t < action.t1) return activeFrame(agent, action, t);
      }
      if (before === null) {
        // Not started yet (or nothing planned): where the agent was before.
        var first = list[0];
        var node = first ? (first.type === "walk" ? first.from : first.node) : agent.home;
        return restFrame(agent, node, t);
      }
      return restFrame(agent, before.node, t);
    }

    function activeFrame(agent, action, t) {
      var n = layout.nodes[action.node];
      if (action.type !== "walk") {
        return {
          id: agent.id, x: n.x, y: n.y, facing: n.face, pose: action.pose, node: action.node,
          entry: action.entry, stride: 0, progress: (t - action.t0) / (action.t1 - action.t0),
        };
      }
      var seg = action.segments[0];
      for (var i = 0; i < action.segments.length; i++) {
        seg = action.segments[i];
        if (t < seg.t1) break;
      }
      var u = seg.duration > 0 ? Math.min(1, Math.max(0, (t - seg.t0) / seg.duration)) : 1;
      var e = EASE[seg.ease](u);
      var travelled = seg.offset + e * seg.length;
      return {
        id: agent.id,
        x: seg.x0 + (seg.x1 - seg.x0) * e,
        y: seg.y0 + (seg.y1 - seg.y0) * e,
        facing: seg.facing, pose: "walk", node: null, from: seg.from, to: seg.to,
        entry: action.entry, stride: travelled,
        progress: action.length > 0 ? travelled / action.length : 1,
      };
    }

    function bubbleAt(t) {
      for (var i = 0; i < bubbles.length; i++) {
        var b = bubbles[i];
        if (b.start <= t && t < b.end) return { id: b.id, agent: b.agent, kind: b.kind, text: b.text, start: b.start, end: b.end };
      }
      return null;
    }

    function frame(t) {
      if (typeof t !== "number") t = now();
      var agents = AGENTS.map(function (agent) {
        var f = agentFrame(agent, t);
        f.motion = !reducedMotion;
        f.zzz = f.pose === "sleep";
        return f;
      });
      var treasurer = agents[AGENTS.length - 1];
      return {
        t: t,
        agents: agents,
        bubble: bubbleAt(t),
        queued: bubbles.filter(function (b) { return b.start > t; }).length,
        screen: { updating: treasurer.pose === "update", entry: treasurer.pose === "update" ? treasurer.entry : null },
      };
    }

    // The next time the frame changes state (an action or bubble starts or ends).
    function nextChange(t) {
      var next = null;
      function consider(x) { if (x > t && (next === null || x < next)) next = x; }
      AGENTS.forEach(function (agent) {
        actions[agent.id].forEach(function (action) { consider(action.t0); consider(action.t1); });
      });
      bubbles.forEach(function (b) { consider(b.start); consider(b.end); });
      return next;
    }

    // -- lifecycle ---------------------------------------------------------------------
    function disarm() {
      if (timer !== null) { clearT(timer); timer = null; }
    }

    function arm() {
      disarm();
      if (!visible()) return;
      var t = now();
      var next = nextChange(t);
      if (next === null) return;
      timer = setT(function () {
        timer = null;
        if (!visible()) return;
        if (onChange) onChange(frame(now()));
        arm();
      }, Math.max(0, next - t));
    }

    function becameVisible() {
      if (pending !== null) {
        var activity = pending;
        pending = null;
        process(activity);
      } else {
        arm();
      }
    }

    function setVisibility(nextMounted, nextPage) {
      var was = visible();
      mounted = nextMounted;
      pageVisible = nextPage;
      var is = visible();
      if (was && !is) { interrupted = true; disarm(); }
      if (!was && is) becameVisible();
    }

    return {
      // Feeds one get_paper_state().activity list; returns the ids that will be
      // animated now (none for the baseline, for repeats or while not visible).
      ingest: function (activity) {
        if (!visible()) {
          pending = Array.isArray(activity) ? activity : [];
          interrupted = true;
          return [];
        }
        return process(activity);
      },
      frame: frame,
      bubbleAt: bubbleAt,
      nextChange: function (t) { return nextChange(typeof t === "number" ? t : now()); },
      plan: function () {
        var out = { agents: {}, bubbles: copy(bubbles) };
        AGENTS.forEach(function (agent) { out.agents[agent.id] = copy(actions[agent.id]); });
        return out;
      },
      mount: function () { setVisibility(true, pageVisible); },
      unmount: function () { setVisibility(false, pageVisible); },
      setPageVisible: function (value) { setVisibility(mounted, !!value); },
      noteDisconnect: function () { interrupted = true; },
      setReducedMotion: function (value) { reducedMotion = !!value; },
      hasSeen: function (id) { return seen.has(id); },
      isMounted: function () { return mounted; },
      pendingTimer: function () { return timer !== null; },
    };
  }

  return {
    TIMING: TIMING,
    MIN_BUBBLE_MS: MIN_BUBBLE_MS,
    BACKLOG_LIMIT: BACKLOG_LIMIT,
    LAYOUT: LAYOUT,
    AGENTS: AGENTS,
    KINDS: KINDS,
    EASE: EASE,
    shortestPath: shortestPath,
    isNeighbour: isNeighbour,
    facingOf: facingOf,
    buildWalk: buildWalk,
    createOffice: createOffice,
  };
});
