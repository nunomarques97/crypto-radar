// Regression tests for the office choreography engine (ui/web/paper_office_engine.js):
// real activity entries -> queued scenes, walks along the office graph, poses and
// speech bubbles. Everything runs on an injected clock and fake timers.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const E = require("../../../ui/web/paper_office_engine.js");
const { LAYOUT, TIMING } = E;

// -- helpers ------------------------------------------------------------------------
function clock(start = 1_000_000) {
  let t = start;
  return { now: () => t, set: (v) => { t = v; }, add: (ms) => { t += ms; return t; } };
}

function fakeTimers() {
  const pending = new Map();
  let id = 0;
  return {
    setTimeout: (fn, ms) => { id += 1; pending.set(id, { fn, ms }); return id; },
    clearTimeout: (h) => { pending.delete(h); },
    pending,
    fire() {
      const entries = [...pending.entries()];
      pending.clear();
      entries.forEach(([, h]) => h.fn());
    },
  };
}

const TEXT = {
  cycle: "I checked 600 coins; 3 look interesting.",
  alert: "SOL went up fast with many buyers. I think it keeps going.",
  qwen: "I read the numbers on SOL: no veto.",
  sonnet: "The router says SOL deserves a closer look.",
  fable: "The router says SOL is a big one: call the Boss.",
  play_open: "I bet 100.00 EUR on SOL going up. I close in 1 hour.",
  play_close: "SOL play closed: won 0.42 EUR after costs.",
};
const AGENT = {
  cycle: "scout", alert: "scout", qwen: "analyst", sonnet: "strategist",
  fable: "boss", play_open: "treasurer", play_close: "treasurer",
};

let serial = 0;
function entry(kind, overrides = {}) {
  serial += 1;
  const minute = String(serial % 60).padStart(2, "0");
  return {
    id: `${kind}:${serial}`, ts: `2026-09-29T10:${minute}:00Z`, agent: AGENT[kind], kind,
    asset: kind === "cycle" ? null : "SOL", text: TEXT[kind], ...overrides,
  };
}

function office(extra = {}) {
  const c = clock();
  const timers = fakeTimers();
  const changes = [];
  const o = E.createOffice({
    now: c.now, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    onChange: (f) => changes.push(f), ...extra,
  });
  o.mount();
  return { o, c, timers, changes };
}

// A mounted office whose first (baseline) payload was `baseline`.
function started(baseline = [], extra = {}) {
  const env = office(extra);
  assert.deepEqual(env.o.ingest(baseline), [], "the first payload is a baseline, never animated");
  return env;
}

function agentAt(o, id, t) { return o.frame(t).agents.find((a) => a.id === id); }

function allActions(plan) {
  return Object.entries(plan.agents).flatMap(([agent, list]) => list.map((a) => ({ agent, ...a })));
}

function actionEnd(plan) {
  return Math.max(...allActions(plan).map((a) => a.t1), ...plan.bubbles.map((b) => b.end));
}

// -- layout and paths ---------------------------------------------------------------
function inside(r, x, y, pad) {
  return x > r.x - pad && x < r.x + r.w + pad && y > r.y - pad && y < r.y + r.d + pad;
}

const OBSTACLES = [
  ...Object.values(LAYOUT.desks), LAYOUT.table, LAYOUT.sofa, ...LAYOUT.plants,
];

test("the walkable graph is connected, inside the room and clear of the furniture", () => {
  const names = Object.keys(LAYOUT.nodes);
  for (const a of names) {
    for (const b of names) assert.ok(E.shortestPath(LAYOUT, a, b), `${a} -> ${b} is reachable`);
  }
  for (const [a, b] of LAYOUT.edges) {
    const p = LAYOUT.nodes[a];
    const q = LAYOUT.nodes[b];
    for (let i = 0; i <= 200; i++) {
      const x = p.x + (q.x - p.x) * (i / 200);
      const y = p.y + (q.y - p.y) * (i / 200);
      assert.ok(x > 0.2 && x < LAYOUT.width - 0.2 && y > 0.2 && y < LAYOUT.depth - 0.2, `${a}-${b} stays in the room`);
      for (const r of OBSTACLES) assert.ok(!inside(r, x, y, 0.2), `${a}-${b} does not cross furniture at ${x.toFixed(2)},${y.toFixed(2)}`);
    }
  }
  // Every agent's home and every kind's spot is a node.
  for (const agent of E.AGENTS) assert.ok(LAYOUT.nodes[agent.home], agent.id);
  for (const kind of Object.values(E.KINDS)) assert.ok(kind.spot === null || LAYOUT.nodes[kind.spot]);
});

test("a walk has consecutive graph-neighbour waypoints, a facing per segment and eased, continuous timing", () => {
  const path = E.shortestPath(LAYOUT, "seat_scout", "table_w");
  const walk = E.buildWalk(LAYOUT, path, 5000, TIMING, false);
  assert.equal(walk.waypoints[0].node, "seat_scout");
  assert.equal(walk.waypoints.at(-1).node, "table_w");
  for (let i = 1; i < walk.waypoints.length; i++) {
    assert.ok(E.isNeighbour(LAYOUT, walk.waypoints[i - 1].node, walk.waypoints[i].node));
  }
  assert.equal(walk.segments[0].ease, "in");
  assert.equal(walk.segments.at(-1).ease, "out");
  walk.segments.slice(1, -1).forEach((s) => assert.equal(s.ease, "linear"));
  let t = 5000;
  for (const s of walk.segments) {
    assert.equal(s.t0, t, "segments are contiguous in time");
    assert.ok(s.duration > 0);
    assert.equal(s.facing, E.facingOf(s.x1 - s.x0, s.y1 - s.y0));
    t = s.t1;
  }
  assert.equal(walk.t1, t);
  assert.equal(walk.segments[0].facing, "south", "leaving the desk towards the viewer");
  // Speed is continuous at the joins: an ease-in ends at cruising speed.
  const cruise = TIMING.walkSpeed / 1000;
  const first = walk.segments[0];
  const endSpeed = (first.length * 2) / first.duration; // d/du(u^2) = 2 at u = 1
  assert.ok(Math.abs(endSpeed - cruise) < 1e-9);
  // A single-segment walk eases in and out.
  const short = E.buildWalk(LAYOUT, ["hall_c", "table_n"], 0, TIMING, false);
  assert.equal(short.segments.length, 1);
  assert.equal(short.segments[0].ease, "in-out");
  // The easing curves start at 0 and end at 1.
  for (const name of Object.keys(E.EASE)) {
    assert.equal(E.EASE[name](0), 0);
    assert.equal(E.EASE[name](1), 1);
  }
});

// -- nothing happens without an entry -----------------------------------------------
test("with no new activity the agents rest: seated idle, the Boss asleep with Z marks, no bubble", () => {
  const baseline = [entry("cycle"), entry("alert"), entry("fable"), entry("play_open")];
  const { o, c, timers } = started(baseline);
  assert.deepEqual(o.ingest(baseline), [], "the same payload again animates nothing");
  const plan = o.plan();
  assert.equal(allActions(plan).length, 0);
  assert.equal(plan.bubbles.length, 0);
  assert.equal(timers.pending.size, 0, "nothing to wake up for");
  for (const t of [c.now(), c.now() + 60_000]) {
    const f = o.frame(t);
    assert.equal(f.bubble, null);
    for (const a of f.agents) {
      const agent = E.AGENTS.find((x) => x.id === a.id);
      assert.equal(a.node, agent.home);
      assert.equal(a.pose, agent.id === "boss" ? "sleep" : "sit");
      assert.equal(a.zzz, agent.id === "boss");
      assert.equal(a.entry, null);
      assert.equal(a.motion, true, "idle motion is on without reduced motion");
    }
  }
});

// -- every kind ---------------------------------------------------------------------
test("cycle: the Scout sits and types at his desk for the fixed reaction time, no bubble", () => {
  const { o, c } = started();
  const e = entry("cycle");
  assert.deepEqual(o.ingest([e]), [e.id]);
  const plan = o.plan();
  assert.equal(plan.bubbles.length, 0);
  assert.deepEqual(plan.agents.scout.map((a) => [a.type, a.node, a.t1 - a.t0, a.entry]),
    [["type", "seat_scout", TIMING.typeMs, e.id]]);
  const t0 = c.now();
  assert.equal(agentAt(o, "scout", t0 + 10).pose, "type");
  assert.equal(agentAt(o, "scout", t0 + TIMING.typeMs - 1).pose, "type");
  assert.equal(agentAt(o, "scout", t0 + TIMING.typeMs).pose, "sit");
  for (const other of ["analyst", "strategist", "boss", "treasurer"]) assert.equal(plan.agents[other].length, 0);
});

function speakingScene(kind, agent, spot, pose) {
  const { o, c } = started();
  const e = entry(kind);
  assert.deepEqual(o.ingest([e]), [e.id]);
  const t0 = c.now();
  const plan = o.plan();
  const list = plan.agents[agent];
  const types = list.map((a) => a.type);
  const speak = list.find((a) => a.type === pose);
  assert.ok(speak, `${kind} has a ${pose} action`);
  assert.equal(speak.node, spot);
  assert.equal(plan.bubbles.length, 1);
  assert.deepEqual(plan.bubbles[0], { id: e.id, agent, kind, text: e.text, start: speak.t0, end: speak.t1 });
  assert.equal(speak.t1 - speak.t0, TIMING.bubbleMs);
  // Out to the spot along the graph, back home afterwards; all tied to the entry.
  const walks = list.filter((a) => a.type === "walk");
  assert.equal(walks.length, 2);
  assert.equal(walks[0].to ?? walks[0].node, spot);
  assert.equal(walks[1].node, E.AGENTS.find((a) => a.id === agent).home);
  list.forEach((a) => assert.equal(a.entry, e.id));
  for (let i = 1; i < list.length; i++) assert.equal(list[i].t0, list[i - 1].t1, "actions are back to back");
  assert.equal(list[0].t0, t0);
  // Other agents do nothing.
  for (const other of E.AGENTS.map((a) => a.id).filter((id) => id !== agent)) assert.equal(plan.agents[other].length, 0, other);
  // Frames: walking mid-way, speaking with the bubble, home at the end.
  const midWalk = (walks[0].t0 + walks[0].t1) / 2;
  const walking = agentAt(o, agent, midWalk);
  assert.equal(walking.pose, "walk");
  assert.ok(walking.stride > 0);
  const f = o.frame(speak.t0 + 1);
  assert.equal(f.agents.find((a) => a.id === agent).pose, pose);
  assert.equal(f.bubble.text, e.text);
  const end = actionEnd(plan);
  const home = agentAt(o, agent, end + 1);
  assert.equal(home.node, E.AGENTS.find((a) => a.id === agent).home);
  return { o, plan, types, e, speak };
}

test("alert: the Scout walks to the meeting table and speaks the alert text", () => {
  const { types } = speakingScene("alert", "scout", "table_w", "speak");
  assert.deepEqual(types, ["walk", "speak", "linger", "walk", "settle"]);
});

test("qwen: the Analyst joins at the table", () => {
  const { types } = speakingScene("qwen", "analyst", "table_n", "speak");
  assert.deepEqual(types, ["walk", "speak", "linger", "walk", "settle"]);
});

test("sonnet: the Strategist joins at the table", () => {
  const { types } = speakingScene("sonnet", "strategist", "table_e", "speak");
  assert.deepEqual(types, ["walk", "speak", "linger", "walk", "settle"]);
});

test("play_open and play_close: the Treasurer walks to the wallet screen and updates it", () => {
  for (const kind of ["play_open", "play_close"]) {
    const { o, types, speak } = speakingScene(kind, "treasurer", "wallet", "update");
    assert.deepEqual(types, ["walk", "update", "linger", "walk", "settle"]);
    const f = o.frame(speak.t0 + 10);
    assert.equal(f.screen.updating, true);
    assert.equal(f.screen.entry, speak.entry);
    assert.equal(agentAt(o, "treasurer", speak.t0 + 10).facing, "west", "facing the left-wall screen");
    assert.equal(o.frame(speak.t1 + 1).screen.updating, false);
  }
});

test("a closed play's bubble names its recorded close reason, verbatim", () => {
  const { o } = started();
  const closes = [
    entry("play_close", { text: "I closed the SOL play at the stop: lost €0.45." }),
    entry("play_close", { text: "I closed the ETH play at the target: won €1.23." }),
    entry("play_close", { text: "I closed the ADA play at the 24-hour limit: lost €0.10." }),
    entry("play_close", { text: "I closed the DOT play: won €0.20." }),
  ];
  assert.deepEqual(o.ingest(closes), closes.map((e) => e.id));
  const plan = o.plan();
  assert.deepEqual(plan.bubbles.map((b) => [b.agent, b.text]), closes.map((e) => ["treasurer", e.text]));
});

test("the Boss sleeps until a real Fable activity, then wakes, walks to the table and goes back to sleep", () => {
  const { o, c } = started();
  // Everything but fable leaves the Boss asleep.
  const others = ["cycle", "alert", "qwen", "sonnet", "play_open", "play_close"].map((k) => entry(k));
  o.ingest(others);
  assert.equal(o.plan().agents.boss.length, 0);
  for (let t = c.now(); t < c.now() + 120_000; t += 1000) {
    const boss = agentAt(o, "boss", t);
    assert.equal(boss.pose, "sleep");
    assert.equal(boss.zzz, true);
  }
  c.add(200_000);
  const f = entry("fable");
  const { types, speak, plan } = (() => {
    assert.deepEqual(o.ingest([...others, f]), [f.id]);
    const p = o.plan();
    const list = p.agents.boss;
    return { types: list.map((a) => a.type), speak: list.find((a) => a.type === "speak"), plan: p };
  })();
  assert.deepEqual(types, ["wake", "walk", "speak", "linger", "walk", "settle"]);
  const wake = plan.agents.boss[0];
  assert.equal(wake.t0, c.now());
  assert.equal(wake.t1 - wake.t0, TIMING.wakeMs);
  const waking = agentAt(o, "boss", wake.t0 + 10);
  assert.equal(waking.pose, "wake");
  assert.equal(waking.zzz, false, "no Z marks once awake");
  assert.equal(speak.node, "table_s");
  assert.equal(plan.agents.boss.at(-1).pose, "lie");
  const after = agentAt(o, "boss", actionEnd(plan) + 1);
  assert.equal(after.pose, "sleep");
  assert.equal(after.zzz, true);
  assert.equal(after.node, "sofa");
});

test("malformed or inconsistent entries never move anyone", () => {
  const { o } = started();
  const bad = [
    { id: "x:1", ts: "2026-09-29T10:00:00Z", kind: "dance", agent: "scout", text: "hi" },
    { id: "", ts: "2026-09-29T10:00:00Z", kind: "alert", agent: "scout", text: "hi" },
    { id: "x:2", ts: "2026-09-29T10:00:00Z", kind: "alert", agent: "boss", text: "hi" },
    { id: "x:3", ts: "2026-09-29T10:00:00Z", kind: "qwen", agent: "analyst", text: "" },
    { id: "x:4", ts: "2026-09-29T10:00:00Z", kind: "fable", agent: "boss" },
    null, 42,
  ];
  assert.deepEqual(o.ingest(bad), []);
  assert.equal(allActions(o.plan()).length, 0);
  assert.deepEqual(o.ingest("not a list"), []);
});

// -- bubble queue -------------------------------------------------------------------
test("bubbles: one at a time, FIFO, each >= 4 s, text verbatim; the agent waits for its turn", () => {
  const { o, c } = started();
  const odd = "  <b>Weird</b> & \"quoted\" text  ";
  const list = [entry("alert"), entry("qwen", { text: odd }), entry("sonnet"), entry("play_open")];
  assert.deepEqual(o.ingest(list), list.map((e) => e.id));
  const plan = o.plan();
  assert.deepEqual(plan.bubbles.map((b) => b.id), list.map((e) => e.id), "FIFO in activity order");
  assert.deepEqual(plan.bubbles.map((b) => b.text), list.map((e) => e.text), "verbatim text");
  for (let i = 0; i < plan.bubbles.length; i++) {
    const b = plan.bubbles[i];
    assert.ok(b.end - b.start >= 4000);
    if (i > 0) assert.ok(b.start >= plan.bubbles[i - 1].end, "never overlapping");
  }
  // Sample the injected clock: at most one bubble, and it matches the plan.
  const start = c.now();
  const end = actionEnd(plan);
  const shown = [];
  for (let t = start; t <= end + 1000; t += 50) {
    const b = o.frame(t).bubble;
    if (b && shown.at(-1) !== b.id) shown.push(b.id);
  }
  assert.deepEqual(shown, list.map((e) => e.id));
  // An agent that arrives while another is speaking stands and waits.
  const analyst = plan.agents.analyst;
  const wait = analyst.find((a) => a.type === "wait");
  assert.ok(wait, "the Analyst waits at the table");
  assert.equal(wait.pose, "stand");
  assert.equal(wait.t1, plan.bubbles[1].start);
  const queued = o.frame(plan.bubbles[0].start + 1).queued;
  assert.equal(queued, 3);
});

test("a bubble timing below 4 s is raised to the minimum", () => {
  const { o } = started([], { timing: { bubbleMs: 1000 } });
  o.ingest([entry("alert")]);
  const b = o.plan().bubbles[0];
  assert.equal(b.end - b.start, E.MIN_BUBBLE_MS);
});

test("a second call for an agent still at the table cancels the walk home that has not started", () => {
  const { o, c } = started();
  const first = entry("alert");
  o.ingest([first]);
  const speak = o.plan().agents.scout.find((a) => a.type === "speak");
  c.set(speak.t1 + 100); // lingering at the table
  const second = entry("alert");
  o.ingest([first, second]);
  const list = o.plan().agents.scout;
  const types = list.map((a) => a.type);
  assert.deepEqual(types.slice(-5), ["linger", "speak", "linger", "walk", "settle"], "no walk home and back");
  assertContinuous(o.plan());
});

test("a cycle while the Scout is at the table types after he is back at his desk", () => {
  const { o, c } = started();
  const alert = entry("alert");
  o.ingest([alert]);
  c.add(1000);
  const cycle = entry("cycle");
  o.ingest([alert, cycle]);
  const list = o.plan().agents.scout;
  const settle = list.find((a) => a.type === "settle");
  const type = list.find((a) => a.type === "type");
  assert.equal(type.t0, settle.t1);
  // A later alert must then walk from the desk (the return is not cancelled under the typing).
  c.add(1000);
  const again = entry("alert");
  o.ingest([alert, cycle, again]);
  assertContinuous(o.plan());
});

// Positions never jump: each action starts where the previous one ended, walks
// go between graph neighbours, and sampled frames move a bounded distance.
function assertContinuous(plan) {
  for (const [agentId, list] of Object.entries(plan.agents)) {
    const agent = E.AGENTS.find((a) => a.id === agentId);
    let at = null;
    for (const a of list) {
      const start = a.type === "walk" ? a.from : a.node;
      if (at !== null) assert.equal(start, at, `${agentId}: ${a.type} starts where the last action ended`);
      if (a.type === "walk") {
        for (let i = 1; i < a.waypoints.length; i++) {
          assert.ok(E.isNeighbour(LAYOUT, a.waypoints[i - 1].node, a.waypoints[i].node), `${agentId} walks along edges`);
        }
      }
      at = a.node;
    }
    if (list.length > 0 && list[0].returning === false && list[0].type !== "wait") {
      const first = list[0].type === "walk" ? list[0].from : list[0].node;
      assert.ok(first === agent.home || at !== null);
    }
  }
}

test("path continuity: under a burst of activity no agent ever teleports", () => {
  const { o, c } = started();
  const kinds = ["alert", "qwen", "cycle", "sonnet", "fable", "play_open", "alert", "cycle", "play_close", "qwen"];
  const all = [];
  for (const kind of kinds) {
    all.push(entry(kind));
    o.ingest(all);
    c.add(2500);
  }
  const plan = o.plan();
  assertContinuous(plan);
  const end = actionEnd(plan);
  const start = Math.min(...allActions(plan).map((a) => a.t0));
  const maxStep = (TIMING.walkSpeed / 1000) * 2 * 20 + 1e-6; // peak speed x 20 ms
  const last = {};
  for (let t = start; t <= end + 20; t += 20) {
    for (const a of o.frame(t).agents) {
      if (last[a.id]) {
        const d = Math.hypot(a.x - last[a.id].x, a.y - last[a.id].y);
        assert.ok(d <= maxStep, `${a.id} moved ${d.toFixed(3)} units in 20 ms at ${t - start}`);
      }
      last[a.id] = a;
    }
  }
  for (const a of o.frame(end + 1).agents) {
    assert.equal(a.node, E.AGENTS.find((x) => x.id === a.id).home, `${a.id} ends at home`);
  }
});

// -- reduced motion -----------------------------------------------------------------
test("reduced motion: walks are instant jumps along the same path, idle motion is off", () => {
  const { o, c } = started([], { reducedMotion: true });
  const e = entry("fable");
  o.ingest([e]);
  const t0 = c.now();
  const list = o.plan().agents.boss;
  const walks = list.filter((a) => a.type === "walk");
  assert.equal(walks.length, 2);
  for (const w of walks) {
    assert.equal(w.t1, w.t0, "instant move");
    w.segments.forEach((s) => assert.equal(s.duration, 0));
  }
  const wake = list.find((a) => a.type === "wake");
  assert.equal(wake.t1, wake.t0);
  const speak = list.find((a) => a.type === "speak");
  assert.equal(speak.t0, t0, "speaks at once");
  assert.equal(speak.t1 - speak.t0, TIMING.bubbleMs, "the bubble still stays readable");
  const f = o.frame(t0);
  const boss = f.agents.find((a) => a.id === "boss");
  assert.equal(boss.node, "table_s");
  assert.equal(boss.pose, "speak");
  f.agents.forEach((a) => assert.equal(a.motion, false));
  // Never observed in a walking pose.
  for (let t = t0; t < actionEnd(o.plan()) + 100; t += 10) {
    assert.notEqual(agentAt(o, "boss", t).pose, "walk");
  }
  // Switching the preference live turns idle motion back on for the next frame.
  o.setReducedMotion(false);
  assert.equal(o.frame(t0).agents[0].motion, true);
});

// -- dedup, backlog, lifecycle ------------------------------------------------------
test("baseline and no replay: ids from the first payload never animate, each new id animates once", () => {
  const old = [entry("alert"), entry("qwen"), entry("fable")];
  const { o, c } = started(old);
  assert.equal(allActions(o.plan()).length, 0);
  const fresh = entry("sonnet");
  assert.deepEqual(o.ingest([...old, fresh]), [fresh.id]);
  c.add(500);
  assert.deepEqual(o.ingest([...old, fresh]), [], "the next poll repeats nothing");
  assert.deepEqual(o.ingest([fresh, ...old]), [], "order does not matter");
  assert.equal(o.plan().bubbles.length, 1);
  assert.ok(o.hasSeen(old[0].id) && o.hasSeen(fresh.id));
});

test("an empty first payload is still the baseline; the next entry animates", () => {
  const { o } = started([]);
  const e = entry("alert");
  assert.deepEqual(o.ingest([e]), [e.id]);
});

test("after the tab was hidden a backlog of more than 5 collapses to the newest entry per agent", () => {
  const { o, c } = started();
  o.setPageVisible(false);
  const backlog = [
    entry("cycle"), entry("alert"), entry("qwen"), entry("qwen"), entry("cycle"),
    entry("play_open"), entry("sonnet"), entry("play_close"),
  ];
  assert.deepEqual(o.ingest(backlog), [], "nothing is planned while hidden");
  assert.equal(allActions(o.plan()).length, 0);
  c.add(60_000);
  o.setPageVisible(true);
  const plan = o.plan();
  const ids = plan.bubbles.map((b) => b.id);
  // newest per agent: scout = second cycle, analyst = second qwen, strategist, treasurer = play_close
  const expected = [backlog[3], backlog[6], backlog[7]].map((e) => e.id);
  assert.deepEqual(ids, expected);
  assert.deepEqual(plan.agents.scout.map((a) => [a.type, a.entry]), [["type", backlog[4].id]]);
  assert.equal(plan.agents.boss.length, 0);
  assert.deepEqual(o.ingest(backlog), [], "the collapsed ones are not replayed later");
});

test("a backlog of 5 or fewer after a hidden tab plays in full; without a hide a big batch plays in full", () => {
  const { o } = started();
  o.setPageVisible(false);
  const five = [entry("alert"), entry("qwen"), entry("qwen"), entry("sonnet"), entry("alert")];
  o.ingest(five);
  o.setPageVisible(true);
  assert.equal(o.plan().bubbles.length, 5);

  const env = started();
  const many = [entry("alert"), entry("qwen"), entry("qwen"), entry("sonnet"), entry("alert"), entry("qwen")];
  assert.equal(env.o.ingest(many).length, 6);
});

test("a disconnect (failed polls) also collapses the backlog on reconnect", () => {
  const { o } = started();
  o.noteDisconnect();
  const backlog = [entry("alert"), entry("alert"), entry("alert"), entry("qwen"), entry("qwen"), entry("qwen")];
  assert.deepEqual(o.ingest(backlog), [backlog[2].id, backlog[5].id]);
});

test("unmount/mount: entries seen while unmounted are deferred, collapsed and never replayed", () => {
  const { o, timers } = started();
  const a = entry("alert");
  o.ingest([a]);
  assert.equal(timers.pending.size, 1);
  o.unmount();
  assert.equal(timers.pending.size, 0, "unmount clears the timer");
  const later = [a, entry("qwen"), entry("sonnet"), entry("qwen"), entry("sonnet"), entry("play_open"), entry("play_close")];
  assert.deepEqual(o.ingest(later), []);
  o.mount();
  const bubbles = o.plan().bubbles.map((b) => b.id);
  assert.deepEqual(bubbles, [a.id, later[3].id, later[4].id, later[6].id]);
  assert.equal(timers.pending.size, 1);
  o.unmount();
  o.mount();
  assert.deepEqual(o.ingest(later), [], "re-mounting replays nothing");
  assert.deepEqual(o.plan().bubbles.map((b) => b.id), bubbles, "no duplicate queue entries");
});

test("setup/cleanup/setup (StrictMode-like) leaves one timer and no duplicate queue entries", () => {
  const { o, c, timers, changes } = started();
  const e = entry("alert");
  for (let i = 0; i < 3; i++) {
    o.mount();
    o.mount();
    o.ingest([e]);
    o.unmount();
    o.mount();
    o.ingest([e]);
  }
  assert.equal(timers.pending.size, 1, "a single wake-up timer");
  assert.equal(o.plan().bubbles.length, 1);
  assert.equal(o.plan().agents.scout.filter((a) => a.type === "speak").length, 1);
  // The timer fires at plan boundaries and re-arms itself once; onChange sees the bubble.
  let guard = 0;
  while (timers.pending.size > 0 && guard < 50) {
    const [[, pendingTimer]] = [...timers.pending.entries()];
    c.add(pendingTimer.ms);
    timers.fire();
    assert.ok(timers.pending.size <= 1);
    guard += 1;
  }
  assert.ok(guard < 50);
  assert.ok(changes.some((f) => f.bubble && f.bubble.id === e.id && f.bubble.text === e.text));
  assert.equal(changes.at(-1).bubble, null);
  assert.equal(timers.pending.size, 0, "idle once everything played");
  o.unmount();
  assert.equal(timers.pending.size, 0);
});

test("a timer that fires after unmount does nothing", () => {
  const { o, timers, changes } = started();
  o.ingest([entry("alert")]);
  const [[, pendingTimer]] = [...timers.pending.entries()];
  o.unmount();
  pendingTimer.fn();
  assert.equal(changes.length, 0);
  assert.equal(timers.pending.size, 0);
});

test("the plan is deterministic for the same inputs and clock", () => {
  const inputs = [[entry("alert"), entry("qwen")], [entry("fable"), entry("cycle"), entry("play_open")]];
  const run = () => {
    const { o, c } = started();
    const acc = [];
    for (const batch of inputs) { acc.push(...batch); o.ingest(acc); c.add(3000); }
    return o.plan();
  };
  assert.deepEqual(run(), run());
});
