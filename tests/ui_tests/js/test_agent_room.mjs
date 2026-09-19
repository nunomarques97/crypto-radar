// Regression tests for the Agent Control Room's pure logic (ui/web/agent_room.js).
// No DOM/browser needed for the state-mapping and connection-building parts;
// triggerCommunication is exercised with a tiny hand-rolled DOM stub since it
// only ever touches classList/querySelector/setTimeout.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  poseFor, pillClass, buildConnections, buildRoomHTML, triggerCommunication, processCommunications, reactToArrival,
} = require("../../../ui/web/agent_room.js");

function agent(overrides) {
  return {
    id: "a", name: "Agent", model: null, role: "Role", status: "IDLE",
    current_event: null, last_activity: null, events_processed: 0, last_error: null,
    ...overrides,
  };
}

// -- state -> pose mapping is deterministic ----------------------------------
test("every 'nothing to do' backend status maps to the sleeping pose", () => {
  for (const status of ["IDLE", "ONLINE", "OFFLINE", "UNKNOWN", "WAITING"]) {
    assert.equal(poseFor(status), "sleeping", status);
  }
});

test("NOT_CONFIGURED always maps to the not-configured pose (Red Team's pose)", () => {
  assert.equal(poseFor("NOT_CONFIGURED"), "not-configured");
});

test("PROCESSING maps to working, COMPLETED to completed", () => {
  assert.equal(poseFor("PROCESSING"), "working");
  assert.equal(poseFor("COMPLETED"), "completed");
});

test("every unhealthy bridge status maps to the error pose", () => {
  for (const status of ["DEGRADED", "RATE_LIMITED", "QUOTA_EXHAUSTED", "AUTH_ERROR"]) {
    assert.equal(poseFor(status), "error", status);
  }
});

test("an unrecognised/future status falls back to sleeping, never throws", () => {
  assert.equal(poseFor("SOME_FUTURE_STATUS"), "sleeping");
  assert.equal(poseFor(undefined), "sleeping");
});

test("pillClass keeps agreeing with the app-wide status -> color table", () => {
  assert.equal(pillClass("PROCESSING"), "info");
  assert.equal(pillClass("AUTH_ERROR"), "err");
  assert.equal(pillClass("NOT_CONFIGURED"), "idle");
  assert.equal(pillClass("COMPLETED"), "ok");
});

// -- connections come from explicit topology, never from display order ------
const REAL_TOPOLOGY = [{ from: "qwen-14b", to: "qwen-red-team" }];

test("agents adjacent in display order produce NO connection unless topology says so", () => {
  const agents = [agent({ id: "qwen" }), agent({ id: "red-team" }), agent({ id: "sonnet" }), agent({ id: "fable" })];
  assert.deepEqual(buildConnections(agents, []), []);
  assert.deepEqual(buildConnections(agents, undefined), []);
});

test("a single agent with topology referencing agents that aren't there produces zero connections", () => {
  assert.equal(buildConnections([agent({ id: "solo" })], REAL_TOPOLOGY).length, 0);
});

test("topology produces exactly the declared pair when both ids are present", () => {
  const agents = [agent({ id: "qwen-14b" }), agent({ id: "qwen-red-team" }), agent({ id: "sonnet" }), agent({ id: "fable" })];
  const conns = buildConnections(agents, REAL_TOPOLOGY);
  assert.deepEqual(conns.map((c) => [c.fromId, c.toId]), [["qwen-14b", "qwen-red-team"]]);
});

test("reordering the agents array does not change which connections are found - topology is id-based, not position-based", () => {
  const inOrder = [agent({ id: "qwen-14b" }), agent({ id: "qwen-red-team" }), agent({ id: "sonnet" }), agent({ id: "fable" })];
  const shuffled = [agent({ id: "fable" }), agent({ id: "qwen-red-team" }), agent({ id: "sonnet" }), agent({ id: "qwen-14b" })];
  const a = buildConnections(inOrder, REAL_TOPOLOGY).map((c) => [c.fromId, c.toId]);
  const b = buildConnections(shuffled, REAL_TOPOLOGY).map((c) => [c.fromId, c.toId]);
  assert.deepEqual(a, [["qwen-14b", "qwen-red-team"]]);
  assert.deepEqual(a, b);
});

test("a topology entry naming an agent id that doesn't exist (renamed/removed) is dropped, not invented", () => {
  const agents = [agent({ id: "qwen-14b" }), agent({ id: "sonnet" }), agent({ id: "fable" })]; // no qwen-red-team here
  assert.deepEqual(buildConnections(agents, REAL_TOPOLOGY), []);
});

test("appending a future agent to the registry with no declared connection creates none automatically", () => {
  const agents = [agent({ id: "qwen-14b" }), agent({ id: "qwen-red-team" }), agent({ id: "future-liquidity" })];
  // future-liquidity sits right after qwen-red-team in display order, but
  // nothing in REAL_TOPOLOGY mentions it - order must not manufacture a link.
  const conns = buildConnections(agents, REAL_TOPOLOGY);
  assert.deepEqual(conns.map((c) => [c.fromId, c.toId]), [["qwen-14b", "qwen-red-team"]]);
});

// -- room rendering never throws, even on edge-case input ---------------------
test("buildRoomHTML renders all four current agents and exactly the one real connection", () => {
  const agents = [
    agent({ id: "qwen-14b", name: "Qwen 14B", status: "PROCESSING", current_event: "LSK" }),
    agent({ id: "qwen-red-team", name: "Red Team", status: "NOT_CONFIGURED" }),
    agent({ id: "sonnet", name: "Sonnet", status: "ONLINE" }),
    agent({ id: "fable", name: "Fable 5.1", status: "AUTH_ERROR", last_error: "no api key" }),
  ];
  const html = buildRoomHTML(agents, REAL_TOPOLOGY);
  assert.match(html, /Qwen 14B/);
  assert.match(html, /Red Team/);
  assert.match(html, /pose-working/);
  assert.match(html, /pose-not-configured/);
  assert.match(html, /pose-sleeping/);
  assert.match(html, /pose-error/);
  // exactly 1 connection (qwen-14b -> qwen-red-team) even though there are
  // 4 agents and 3 visual gaps - the other two gaps get no connector at all.
  assert.equal((html.match(/class="connection"/g) || []).length, 1);
});

test("buildRoomHTML with no topology renders zero connections even for the same 4 agents", () => {
  const agents = [
    agent({ id: "qwen-14b" }), agent({ id: "qwen-red-team" }), agent({ id: "sonnet" }), agent({ id: "fable" }),
  ];
  const html = buildRoomHTML(agents, []);
  assert.equal((html.match(/class="connection"/g) || []).length, 0);
});

test("an empty agent list renders a calm empty state, not broken markup", () => {
  const html = buildRoomHTML([], REAL_TOPOLOGY);
  assert.match(html, /room-empty/);
});

test("an unknown future agent (from a registry entry not yet given a pose) still renders", () => {
  const agents = [agent({ id: "qwen-14b" }), agent({ id: "future-risk-agent", status: "SOME_NEW_STATUS" })];
  assert.doesNotThrow(() => buildRoomHTML(agents, REAL_TOPOLOGY));
});

// -- communication animation: off by default, only fires when explicitly asked
class FakeClassList {
  constructor() { this.set = new Set(); }
  add(c) { this.set.add(c); }
  remove(c) { this.set.delete(c); }
  contains(c) { return this.set.has(c); }
}
class FakeConnection {
  constructor(from, to) { this.dataset = { from, to }; this.classList = new FakeClassList(); }
}
class FakeRoom {
  constructor(conns) { this.conns = conns; }
  querySelector(sel) {
    const m = sel.match(/data-from="([^"]+)"\]\[data-to="([^"]+)"/);
    return this.conns.find((c) => c.dataset.from === m[1] && c.dataset.to === m[2]) || null;
  }
}

test("triggerCommunication adds 'communicating' then removes it after the duration, only for the requested pair", () => {
  const a = new FakeConnection("qwen-14b", "qwen-red-team");
  const b = new FakeConnection("qwen-red-team", "sonnet");
  const room = new FakeRoom([a, b]);

  const ok = triggerCommunication(room, "qwen-14b", "qwen-red-team", 5);
  assert.equal(ok, true);
  assert.equal(a.classList.contains("communicating"), true);
  assert.equal(b.classList.contains("communicating"), false);

  return new Promise((resolve) => {
    setTimeout(() => {
      assert.equal(a.classList.contains("communicating"), false);
      resolve();
    }, 20);
  });
});

test("triggerCommunication on a non-existent pair returns false and touches nothing", () => {
  const room = new FakeRoom([new FakeConnection("qwen-14b", "qwen-red-team")]);
  assert.equal(triggerCommunication(room, "sonnet", "fable", 5), false);
});

// -- Phase 2: real communication events (processCommunications) -------------
// Test spec section 15, items 1-12.

test("(1) a rendered room with no communication call stays static - no 'communicating' class anywhere", () => {
  const agents = [
    agent({ id: "qwen-14b", status: "PROCESSING" }), // (11) WORKING alone must not animate anything
    agent({ id: "qwen-red-team", status: "NOT_CONFIGURED" }),
  ];
  const html = buildRoomHTML(agents, REAL_TOPOLOGY);
  assert.doesNotMatch(html, /communicating/);
});

test("(2) an explicit communication event triggers exactly one animation", () => {
  const a = new FakeConnection("qwen-14b", "qwen-red-team");
  const room = new FakeRoom([a]);
  const seen = new Set();
  const triggered = processCommunications(room, [{ id: 1, from: "qwen-14b", to: "qwen-red-team" }], seen, 5);
  assert.deepEqual(triggered, [1]);
  assert.equal(a.classList.contains("communicating"), true);
});

test("(3) the pulse travels sender -> receiver: triggerCommunication is called with (from, to), not swapped", () => {
  const a = new FakeConnection("qwen-14b", "qwen-red-team");
  const room = new FakeRoom([a]);
  // The connection element itself only exists for data-from="qwen-14b"
  // data-to="qwen-red-team" - a reversed lookup would find nothing.
  assert.equal(triggerCommunication(room, "qwen-14b", "qwen-red-team", 5), true);
  assert.equal(triggerCommunication(room, "qwen-red-team", "qwen-14b", 5), false);
});

test("(4)+(5) the correct connection is resolved by from+to regardless of registry/array order", () => {
  const ab = new FakeConnection("qwen-14b", "qwen-red-team");
  const bc = new FakeConnection("qwen-red-team", "sonnet");
  const roomInOrder = new FakeRoom([ab, bc]);
  const roomShuffled = new FakeRoom([bc, ab]);
  const seen1 = new Set();
  const seen2 = new Set();
  processCommunications(roomInOrder, [{ id: "x1", from: "qwen-red-team", to: "sonnet" }], seen1, 5);
  processCommunications(roomShuffled, [{ id: "x2", from: "qwen-red-team", to: "sonnet" }], seen2, 5);
  assert.equal(ab.classList.contains("communicating"), false);
  assert.equal(bc.classList.contains("communicating"), true);
});

test("(6) an unknown sender/receiver pair is ignored safely - marked seen, never throws, nothing animates", () => {
  const a = new FakeConnection("qwen-14b", "qwen-red-team");
  const room = new FakeRoom([a]);
  const seen = new Set();
  assert.doesNotThrow(() => {
    const triggered = processCommunications(room, [{ id: "ghost", from: "nope", to: "also-nope" }], seen, 5);
    assert.deepEqual(triggered, ["ghost"]); // seen, so it won't be retried forever
  });
  assert.equal(a.classList.contains("communicating"), false);
});

test("(7) the same communication id does not replay on the next tick", () => {
  const a = new FakeConnection("qwen-14b", "qwen-red-team");
  const room = new FakeRoom([a]);
  const seen = new Set();
  const first = processCommunications(room, [{ id: 42, from: "qwen-14b", to: "qwen-red-team" }], seen, 5);
  assert.deepEqual(first, [42]);
  return new Promise((resolve) => {
    setTimeout(() => {
      assert.equal(a.classList.contains("communicating"), false); // pulse already finished
      // Same id arrives again on a later poll tick (as get_state() would keep
      // returning it until the backend rotates it out) - must not re-animate.
      const second = processCommunications(room, [{ id: 42, from: "qwen-14b", to: "qwen-red-team" }], seen, 5);
      assert.deepEqual(second, []);
      assert.equal(a.classList.contains("communicating"), false);
      resolve();
    }, 20);
  });
});

test("(8) two different connections animate independently from one batch of events", () => {
  const ab = new FakeConnection("qwen-14b", "qwen-red-team");
  const bc = new FakeConnection("qwen-red-team", "sonnet");
  const room = new FakeRoom([ab, bc]);
  const seen = new Set();
  processCommunications(room, [
    { id: "e1", from: "qwen-14b", to: "qwen-red-team" },
    { id: "e2", from: "qwen-red-team", to: "sonnet" },
  ], seen, 30);
  assert.equal(ab.classList.contains("communicating"), true);
  assert.equal(bc.classList.contains("communicating"), true);
});

test("queueing: a second trigger on a connection already mid-pulse is deferred, not corrupted/restarted", () => {
  const a = new FakeConnection("qwen-14b", "qwen-red-team");
  const room = new FakeRoom([a]);
  assert.equal(triggerCommunication(room, "qwen-14b", "qwen-red-team", 15), true);
  assert.equal(a.classList.contains("communicating"), true);
  // Fired while the first pulse is still running - must queue, not double-add
  // or reset the timer.
  assert.equal(triggerCommunication(room, "qwen-14b", "qwen-red-team", 15), true);
  assert.equal(a.classList.contains("communicating"), true);

  return new Promise((resolve) => {
    setTimeout(() => {
      // First pulse has ended; the queued second one should now be running.
      assert.equal(a.classList.contains("communicating"), true);
      setTimeout(() => {
        assert.equal(a.classList.contains("communicating"), false);
        resolve();
      }, 20);
    }, 20);
  });
});

test("(9)/(10) TEST MODE's direct triggerCommunication call never touches a seenIds set - a synthetic pulse cannot pollute real dedup/history", () => {
  const a = new FakeConnection("qwen-14b", "qwen-red-team");
  const room = new FakeRoom([a]);
  const seen = new Set(); // stands in for app.js's real seenCommunicationIds
  assert.equal(triggerCommunication(room, "qwen-14b", "qwen-red-team", 5), true);
  assert.equal(seen.size, 0);
});

test("(11) agent status alone never animates a connection - buildRoomHTML output never contains 'communicating' no matter the statuses", () => {
  const agents = [
    agent({ id: "qwen-14b", status: "PROCESSING" }),
    agent({ id: "qwen-red-team", status: "PROCESSING" }),
  ];
  assert.doesNotMatch(buildRoomHTML(agents, REAL_TOPOLOGY), /communicating/);
});

// -- Phase 3: receiver wake-up reaction --------------------------------------
// Test spec section 17, items 1-13 (14 = "existing Phase 1/2 tests still
// pass", covered by everything above still running unmodified).
class FakeWorkstation {
  constructor(agentId, poseClass) {
    this.dataset = { agentId };
    this.classList = new FakeClassList();
    this.classList.add("workstation");
    if (poseClass) this.classList.add("pose-" + poseClass);
    this.renderedHTML = null;
  }
  set outerHTML(html) {
    this.renderedHTML = html;
    const poseMatch = html.match(/pose-([a-z-]+)/);
    this.classList = new FakeClassList();
    this.classList.add("workstation");
    if (poseMatch) this.classList.add("pose-" + poseMatch[1]);
  }
  get outerHTML() { return this.renderedHTML; }
}
class FakeRoomFull {
  constructor(conns, workstations) {
    this.conns = conns || [];
    this.workstations = workstations || [];
  }
  querySelector(sel) {
    let m = sel.match(/^\.connection\[data-from="([^"]+)"\]\[data-to="([^"]+)"\]$/);
    if (m) return this.conns.find((c) => c.dataset.from === m[1] && c.dataset.to === m[2]) || null;
    m = sel.match(/^\.workstation\[data-agent-id="([^"]+)"\]$/);
    if (m) return this.workstations.find((w) => w.dataset.agentId === m[1]) || null;
    return null;
  }
}
function wait(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

test("(1) communication arrival triggers the receiver's wake-up reaction", async () => {
  const conn = new FakeConnection("qwen-14b", "qwen-red-team");
  const receiver = new FakeWorkstation("qwen-red-team", "sleeping");
  const room = new FakeRoomFull([conn], [receiver]);
  const resolveAgent = (id) => (id === "qwen-red-team" ? agent({ id, status: "IDLE" }) : undefined);

  triggerCommunication(room, "qwen-14b", "qwen-red-team", 5, { resolveAgent });
  assert.equal(receiver.classList.contains("waking"), false); // not yet - pulse still travelling
  await wait(15); // pulse (5ms) arrives, wake-up (WAKE_MS) starts
  assert.equal(receiver.classList.contains("waking"), true);
});

test("(2) the receiver is resolved from `to`, not from registry/array order", async () => {
  const conn = new FakeConnection("qwen-14b", "qwen-red-team");
  // workstations registered in the opposite order from the connection's from/to
  const sonnetWs = new FakeWorkstation("sonnet", "sleeping");
  const receiver = new FakeWorkstation("qwen-red-team", "sleeping");
  const room = new FakeRoomFull([conn], [sonnetWs, receiver]);
  const resolveAgent = (id) => agent({ id, status: "IDLE" });

  triggerCommunication(room, "qwen-14b", "qwen-red-team", 5, { resolveAgent });
  await wait(15);
  assert.equal(receiver.classList.contains("waking"), true);
  assert.equal(sonnetWs.classList.contains("waking"), false);
});

test("(3) the sender never wakes itself", async () => {
  const conn = new FakeConnection("qwen-14b", "qwen-red-team");
  const sender = new FakeWorkstation("qwen-14b", "sleeping");
  const receiver = new FakeWorkstation("qwen-red-team", "sleeping");
  const room = new FakeRoomFull([conn], [sender, receiver]);
  const resolveAgent = (id) => agent({ id, status: "IDLE" });

  triggerCommunication(room, "qwen-14b", "qwen-red-team", 5, { resolveAgent });
  await wait(15);
  assert.equal(sender.classList.contains("waking"), false);
  assert.equal(receiver.classList.contains("waking"), true);
});

test("(4) the pulse and the receiver reaction are synchronized - waking never starts before the pulse arrives", async () => {
  const conn = new FakeConnection("qwen-14b", "qwen-red-team");
  const receiver = new FakeWorkstation("qwen-red-team", "sleeping");
  const room = new FakeRoomFull([conn], [receiver]);
  const resolveAgent = (id) => agent({ id, status: "IDLE" });

  triggerCommunication(room, "qwen-14b", "qwen-red-team", 20, { resolveAgent });
  await wait(5); // well before the 20ms pulse duration
  assert.equal(receiver.classList.contains("waking"), false);
  await wait(25); // now the pulse has arrived and waking has started
  assert.equal(receiver.classList.contains("waking"), true);
});

test("(5) the same communication id cannot trigger the wake-up twice", (t) => {
  // T026: deterministic virtual clock (node:test's built-in timer mock,
  // node: core only) instead of a real-timer `await wait(720)`.
  // agent_room.js looks up the global setTimeout/clearTimeout by name on
  // every call - it never caches a reference at module load - so enabling
  // the mock here, after the module was already required at the top of
  // this file, still intercepts every timer it schedules from this point
  // on. tick() runs the same callback chain synchronously with zero
  // wall-clock wait and zero host-load jitter, so it still proves exactly
  // what the real-timer version proved (the pulse, then the full default
  // WAKE_MS wake-up, run to completion and settle before the dedup is
  // checked) - it just no longer needs a slack margin to tolerate
  // scheduler delay under load, which is what made this assertion flaky
  // (see docs/tasks/results/T026.md).
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const conn = new FakeConnection("qwen-14b", "qwen-red-team");
  const receiver = new FakeWorkstation("qwen-red-team", "sleeping");
  const room = new FakeRoomFull([conn], [receiver]);
  const resolveAgent = (id) => agent({ id, status: "IDLE" });
  const seen = new Set();

  processCommunications(room, [{ id: "c1", from: "qwen-14b", to: "qwen-red-team" }], seen, 5, resolveAgent);
  // Two separate tick() calls, not one tick(705): node:test's mock timers
  // only fire callbacks already due at the moment tick() is invoked, so the
  // WAKE_MS setTimeout that reactToArrival schedules *inside* the pulse's
  // own callback needs its own tick to be seen (verified empirically - a
  // single tick(705) leaves the nested timer unfired).
  t.mock.timers.tick(5); // pulse elapses, reactToArrival schedules the WAKE_MS timer
  t.mock.timers.tick(700); // default WAKE_MS wake-up runs to completion, settles back
  const settledOnce = receiver.renderedHTML;
  assert.ok(settledOnce);

  const second = processCommunications(room, [{ id: "c1", from: "qwen-14b", to: "qwen-red-team" }], seen, 5, resolveAgent);
  assert.deepEqual(second, []); // deduped - never re-enters triggerCommunication/reactToArrival
  t.mock.timers.tick(15);
  assert.equal(receiver.renderedHTML, settledOnce); // untouched by the replay
});

test("(6) a sleeping receiver transitions into the waking stage", () => {
  const receiver = new FakeWorkstation("qwen-red-team", "sleeping");
  const room = new FakeRoomFull([], [receiver]);
  const resolveAgent = () => agent({ id: "qwen-red-team", status: "IDLE" });
  const ok = reactToArrival(room, "qwen-red-team", resolveAgent, { wakeMs: 5000 });
  assert.equal(ok, true);
  assert.equal(receiver.classList.contains("waking"), true);
});

test("(7) waking settles on whatever the backend says at the moment it finishes - not a snapshot from when the pulse left", async () => {
  const receiver = new FakeWorkstation("qwen-red-team", "sleeping");
  const room = new FakeRoomFull([], [receiver]);
  let currentStatus = "IDLE"; // what the backend says when the wake-up STARTS
  const resolveAgent = () => agent({ id: "qwen-red-team", status: currentStatus });

  reactToArrival(room, "qwen-red-team", resolveAgent, { wakeMs: 10 });
  currentStatus = "PROCESSING"; // backend moves the agent to WORKING before the wake-up finishes
  await wait(25);
  assert.match(receiver.renderedHTML, /pose-working/);
});

test("(8) backend state remains authoritative even for TEST MODE's forced demo", (t) => {
  // T026: deterministic virtual clock instead of a real-timer `await
  // wait(25)`. reactToArrival's two nested setTimeouts (wakeMs then
  // holdMs) total exactly 10ms here; tick(10) runs both to completion
  // synchronously, proving the same thing the original assertion proved
  // (the forced demo still reconciles to the real NOT_CONFIGURED backend
  // state, never freezes on the synthetic "working" beat) without the
  // wall-clock margin that made it flaky under load - see
  // docs/tasks/results/T026.md.
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const receiver = new FakeWorkstation("qwen-red-team", "not-configured");
  const room = new FakeRoomFull([], [receiver]);
  // Real backend status stays NOT_CONFIGURED throughout - the demo must
  // still land back here, never invent a permanent WORKING state.
  const resolveAgent = () => agent({ id: "qwen-red-team", status: "NOT_CONFIGURED" });

  reactToArrival(room, "qwen-red-team", resolveAgent, { force: true, wakeMs: 5, holdMs: 5 });
  // Two ticks, not one tick(10): the holdMs timer is scheduled inside the
  // wakeMs timer's own callback, so it only becomes "due" from tick()'s
  // point of view once a first tick has run that callback (see the (5)
  // test above for the same node:test mock-timer behaviour).
  t.mock.timers.tick(5); // wakeMs elapses - synthetic "working" beat, schedules holdMs
  t.mock.timers.tick(5); // holdMs elapses - reconciled back to the real backend state
  assert.match(receiver.renderedHTML, /pose-not-configured/);
});

test("(9) an ERROR receiver never becomes WORKING just because a communication arrived", async () => {
  const receiver = new FakeWorkstation("fable", "error");
  const room = new FakeRoomFull([], [receiver]);
  const resolveAgent = () => agent({ id: "fable", status: "AUTH_ERROR" });

  const ok = reactToArrival(room, "fable", resolveAgent, { wakeMs: 5 });
  assert.equal(ok, false); // guarded - never even starts waking
  assert.equal(receiver.classList.contains("waking"), false);
  await wait(15);
  assert.equal(receiver.renderedHTML, null); // never touched
});

test("(10) a NOT_CONFIGURED receiver never becomes WORKING just because a communication arrived", async () => {
  const receiver = new FakeWorkstation("qwen-red-team", "not-configured");
  const room = new FakeRoomFull([], [receiver]);
  const resolveAgent = () => agent({ id: "qwen-red-team", status: "NOT_CONFIGURED" });

  const ok = reactToArrival(room, "qwen-red-team", resolveAgent, { wakeMs: 5 });
  assert.equal(ok, false);
  assert.equal(receiver.classList.contains("waking"), false);
});

test("(11) TEST MODE's forced demo runs the exact same wake-up pipeline against a NOT_CONFIGURED receiver, and shows a working beat before settling", async () => {
  const receiver = new FakeWorkstation("qwen-red-team", "not-configured");
  const room = new FakeRoomFull([], [receiver]);
  const resolveAgent = () => agent({ id: "qwen-red-team", status: "NOT_CONFIGURED" });

  const ok = reactToArrival(room, "qwen-red-team", resolveAgent, { force: true, wakeMs: 5, holdMs: 5 });
  assert.equal(ok, true);
  assert.equal(receiver.classList.contains("waking"), true);
  await wait(10); // wakeMs elapsed - the demo's presentation-only "working" beat
  assert.match(receiver.renderedHTML, /pose-working/);
  await wait(15); // holdMs elapsed - reconciled back to the real backend state
  assert.match(receiver.renderedHTML, /pose-not-configured/);
});

test("(12) TEST MODE's forced demo never mutates the agent object it was given (no backend state is written anywhere)", async () => {
  const receiver = new FakeWorkstation("qwen-red-team", "not-configured");
  const room = new FakeRoomFull([], [receiver]);
  const real = agent({ id: "qwen-red-team", status: "NOT_CONFIGURED" });
  const resolveAgent = () => real;

  reactToArrival(room, "qwen-red-team", resolveAgent, { force: true, wakeMs: 5, holdMs: 5 });
  await wait(25);
  assert.equal(real.status, "NOT_CONFIGURED"); // the only object standing in for "backend state" is untouched
});

test("(13) two communications to different receivers wake up independently without corrupting each other", async () => {
  const redTeam = new FakeWorkstation("qwen-red-team", "sleeping");
  const sonnet = new FakeWorkstation("sonnet", "sleeping");
  const room = new FakeRoomFull([], [redTeam, sonnet]);
  const resolveAgent = (id) => agent({ id, status: "IDLE" });

  reactToArrival(room, "qwen-red-team", resolveAgent, { wakeMs: 5 });
  reactToArrival(room, "sonnet", resolveAgent, { wakeMs: 30 });
  await wait(15);
  assert.equal(redTeam.renderedHTML !== null, true); // already settled
  assert.equal(sonnet.classList.contains("waking"), true); // still mid-wake, untouched by red-team's settle
  await wait(30);
  assert.equal(sonnet.renderedHTML !== null, true);
});

test("a receiver that resolveAgent can't find is left completely untouched", () => {
  const receiver = new FakeWorkstation("qwen-red-team", "sleeping");
  const room = new FakeRoomFull([], [receiver]);
  const ok = reactToArrival(room, "qwen-red-team", () => undefined, { wakeMs: 5 });
  assert.equal(ok, false);
  assert.equal(receiver.classList.contains("waking"), false);
});

test("an unknown/unrendered receiver id is ignored safely, never throws", () => {
  const room = new FakeRoomFull([], []);
  assert.doesNotThrow(() => {
    assert.equal(reactToArrival(room, "ghost-agent", () => agent({ id: "ghost-agent", status: "IDLE" }), { wakeMs: 5 }), false);
  });
});
