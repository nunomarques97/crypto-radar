/* Agent Control Room - pure rendering/mapping logic (no DOM access), so it
 * can be unit tested identically under Node (tests/ui_tests/js/test_agent_room.mjs)
 * and in the browser, the same pattern test_mode.js already uses.
 *
 * Two concepts stay independent on purpose (see the task spec's section 12):
 *   - STATE  (poseFor/pillClass): what a single agent's own backend status is.
 *   - COMMUNICATION (triggerCommunication/processCommunications): a
 *     transient pulse on the connection BETWEEN two agents. Phase 2 wires
 *     this to a clean event contract (get_state().agent_communications, see
 *     bridge.py) but nothing in this file or in app.js fabricates an entry -
 *     the list is always empty until a real backend emitter exists. The two
 *     callers today are the TEST MODE "TESTAR COMUNICAÇÃO" demo button
 *     (direct triggerCommunication call, never enters seenIds/history) and
 *     app.js's tick() loop (processCommunications, dedup'd by comm id).
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.RadarAgentRoom = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // -- status -> pill color role (DESIGN.md's five roles) --------------------
  // Single source of truth for every status pill in the app (agents, system
  // status, funnel demand, alert rows) - not agent-room-specific, it just
  // lives here because the room is what needed a second consumer of it.
  var STATUS_CLASS = {
    OK: "ok", ONLINE: "ok", SENT: "ok", RUNNING: "ok", COMPLETED: "ok", ENABLED: "ok",
    STARTING: "warn", STOPPING: "warn", PENDING: "warn", DEGRADED: "warn",
    RATE_LIMITED: "warn", QUOTA_EXHAUSTED: "warn", DEFERRED: "warn",
    ERROR: "err", FAILED: "err", AUTH_ERROR: "err", OFFLINE: "err", STALE: "err", UNAVAILABLE: "err",
    PROCESSING: "info", RECEIVING: "info",
    IDLE: "idle", NOT_CONFIGURED: "idle", UNKNOWN: "idle", DISABLED: "idle",
    WAITING: "idle", "N/A": "idle", STOPPED: "idle",
  };
  function pillClass(status) {
    return STATUS_CLASS[status] || "idle";
  }

  // -- status -> physical pose (what the character in the room is doing) ----
  // Deliberately coarser than STATUS_CLASS: the backend can only ever tell us
  // "nothing to do" / "actively processing" / "just finished" / "unhealthy" /
  // "not built yet" - it has no signal for "awake but bored" vs "asleep", so
  // every flavour of "nothing to do" (IDLE, ONLINE-but-not-processing,
  // OFFLINE, UNKNOWN, WAITING) renders as the one honest SLEEPING pose rather
  // than inventing a distinction the backend doesn't make.
  var POSE = {
    NOT_CONFIGURED: "not-configured",
    IDLE: "sleeping", ONLINE: "sleeping", OFFLINE: "sleeping", UNKNOWN: "sleeping", WAITING: "sleeping",
    PROCESSING: "working", RECEIVING: "receiving",
    COMPLETED: "completed",
    DEGRADED: "error", RATE_LIMITED: "error", QUOTA_EXHAUSTED: "error", AUTH_ERROR: "error", ERROR: "error",
  };
  function poseFor(status) {
    return POSE[status] || "sleeping";
  }

  // -- connections: explicit topology, independent of display order ----------
  // `topology` is the `agent_connections` list from get_state() (sourced
  // from AgentDefinition.connects_to in ui/agents.py - see build_connections()
  // there). Display order (the `agents` array) and communication topology are
  // deliberately two separate inputs: reordering/renaming-nothing in `agents`
  // can ever create a connection that isn't in `topology`, and a topology
  // entry whose id isn't present in `agents` is dropped rather than guessed at.
  function buildConnections(agents, topology) {
    var out = [];
    (topology || []).forEach(function (t) {
      var fromIndex = agents.findIndex(function (a) { return a.id === t.from; });
      var toIndex = agents.findIndex(function (a) { return a.id === t.to; });
      if (fromIndex !== -1 && toIndex !== -1) {
        out.push({ fromId: t.from, toId: t.to, fromIndex: fromIndex, toIndex: toIndex });
      }
    });
    return out;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function pill(status, label) {
    var cls = pillClass(status);
    var text = label || status || "—";
    return '<span class="pill ' + cls + '"><span class="dot"></span>' + escapeHtml(text) + "</span>";
  }

  // -- the character, one shared SVG built from state, not per-agent art -----
  function characterSvg(pose, accentVar) {
    var a = "var(" + accentVar + ")";
    if (pose === "sleeping") {
      return (
        '<svg class="figure figure-sleeping" width="72" height="52" viewBox="0 0 72 52">' +
        '<line x1="16" y1="12" x2="10" y2="7" stroke="var(--muted-dim)" stroke-width="1.2"/>' +
        '<circle cx="9" cy="6" r="2" fill="var(--idle)"/>' +
        '<rect x="16" y="4" width="20" height="16" rx="7" fill="var(--panel-hi)" stroke="var(--border)" stroke-width="1.1"/>' +
        '<line x1="21" y1="12" x2="25" y2="12" stroke="var(--muted-dim)" stroke-width="1.3" stroke-linecap="round"/>' +
        '<line x1="28" y1="12" x2="32" y2="12" stroke="var(--muted-dim)" stroke-width="1.3" stroke-linecap="round"/>' +
        '<rect x="34" y="6" width="34" height="24" rx="11" fill="var(--panel-hi)" stroke="var(--border)" stroke-width="1.1"/>' +
        '<circle class="core core-dim" cx="51" cy="18" r="3.6" fill="var(--idle)"/>' +
        '</svg>'
      );
    }
    if (pose === "not-configured") {
      return (
        '<svg class="figure figure-not-configured" width="56" height="72" viewBox="0 0 56 72">' +
        '<line x1="28" y1="8" x2="28" y2="2" stroke="var(--muted-dim)" stroke-width="1.2" stroke-dasharray="2 2"/>' +
        '<circle cx="28" cy="2" r="2.4" fill="none" stroke="var(--muted-dim)"/>' +
        '<rect x="14" y="9" width="28" height="20" rx="8" fill="var(--panel-hi)" stroke="var(--muted-dim)" stroke-width="1.2" stroke-dasharray="4 3"/>' +
        '<line x1="20" y1="17" x2="24" y2="17" stroke="var(--muted-dim)" stroke-width="1.4"/><line x1="32" y1="17" x2="36" y2="17" stroke="var(--muted-dim)" stroke-width="1.4"/>' +
        '<rect x="10" y="30" width="36" height="30" rx="10" fill="var(--panel-hi)" stroke="var(--border)" stroke-dasharray="4 3"/>' +
        '<circle cx="28" cy="44" r="4.5" fill="none" stroke="var(--muted-dim)" stroke-width="1.2"/>' +
        '<rect x="5" y="36" width="7" height="24" rx="3.5" fill="var(--panel-hi)" stroke="var(--border)"/>' +
        '<rect x="44" y="36" width="7" height="24" rx="3.5" fill="var(--panel-hi)" stroke="var(--border)"/>' +
        '<rect x="17" y="64" width="8" height="8" rx="3" fill="var(--panel-hi)" stroke="var(--border)"/>' +
        '<rect x="31" y="64" width="8" height="8" rx="3" fill="var(--panel-hi)" stroke="var(--border)"/>' +
        '</svg>'
      );
    }
    if (pose === "error") {
      return (
        '<svg class="figure figure-error" width="56" height="86" viewBox="0 0 56 86">' +
        '<line x1="28" y1="8" x2="28" y2="2" stroke="' + a + '" stroke-width="1.4"/>' +
        '<circle class="core" cx="28" cy="2" r="2.4" fill="' + a + '"/>' +
        '<g transform="rotate(6 28 18)">' +
        '<rect x="14" y="8" width="28" height="20" rx="8" fill="var(--panel-hi)" stroke="' + a + '" stroke-width="1.2"/>' +
        '<line x1="20" y1="15" x2="25" y2="18" stroke="' + a + '" stroke-width="1.5"/><line x1="36" y1="15" x2="31" y2="18" stroke="' + a + '" stroke-width="1.5"/>' +
        '</g>' +
        '<rect x="10" y="30" width="36" height="30" rx="10" fill="var(--panel-hi)" stroke="var(--border)"/>' +
        '<circle class="core" cx="28" cy="44" r="4.5" fill="' + a + '"/>' +
        '<rect x="6" y="34" width="7" height="24" rx="3.5" fill="var(--panel-hi)" stroke="var(--border)" transform="rotate(8 9.5 34)"/>' +
        '<rect x="43" y="34" width="7" height="24" rx="3.5" fill="var(--panel-hi)" stroke="var(--border)" transform="rotate(-8 46.5 34)"/>' +
        '<rect x="17" y="64" width="8" height="20" rx="3" fill="var(--panel-hi)" stroke="var(--border)"/>' +
        '<rect x="31" y="64" width="8" height="20" rx="3" fill="var(--panel-hi)" stroke="var(--border)"/>' +
        '</svg>'
      );
    }
    if (pose === "completed") {
      return (
        '<svg class="figure figure-completed" width="56" height="86" viewBox="0 0 56 86">' +
        '<line x1="28" y1="8" x2="28" y2="2" stroke="' + a + '" stroke-width="1.4"/>' +
        '<circle cx="28" cy="2" r="2.4" fill="' + a + '"/>' +
        '<rect x="14" y="8" width="28" height="20" rx="8" fill="var(--panel-hi)" stroke="' + a + '" stroke-width="1.2"/>' +
        '<circle cx="22" cy="17" r="1.8" fill="var(--text)"/><circle cx="34" cy="17" r="1.8" fill="var(--text)"/>' +
        '<rect x="10" y="30" width="36" height="30" rx="10" fill="var(--panel-hi)" stroke="var(--border)"/>' +
        '<circle class="core" cx="28" cy="44" r="4.5" fill="' + a + '"/>' +
        '<path d="M25 43 l2.4 2.6 l4.6 -5.2" fill="none" stroke="var(--panel-hi)" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>' +
        '<rect x="5" y="36" width="7" height="24" rx="3.5" fill="var(--panel-hi)" stroke="var(--border)"/>' +
        '<rect x="44" y="36" width="7" height="24" rx="3.5" fill="var(--panel-hi)" stroke="var(--border)"/>' +
        '<rect x="17" y="64" width="8" height="20" rx="3" fill="var(--panel-hi)" stroke="var(--border)"/>' +
        '<rect x="31" y="64" width="8" height="20" rx="3" fill="var(--panel-hi)" stroke="var(--border)"/>' +
        '</svg>'
      );
    }
    // "working" and "receiving" (receiving is prepared but unused in Phase 1;
    // it gets the same pose plus a distinct ring class for future wiring).
    return (
      '<svg class="figure figure-working" width="56" height="86" viewBox="0 0 56 86">' +
      '<line x1="28" y1="8" x2="28" y2="2" stroke="' + a + '" stroke-width="1.4"/>' +
      '<circle class="core core-pulse" cx="28" cy="2" r="2.4" fill="' + a + '"/>' +
      '<rect x="14" y="8" width="28" height="20" rx="8" fill="var(--panel-hi)" stroke="' + a + '" stroke-width="1.2"/>' +
      '<circle cx="22" cy="17" r="1.8" fill="var(--text)"/><circle cx="34" cy="17" r="1.8" fill="var(--text)"/>' +
      '<rect x="10" y="30" width="36" height="30" rx="10" fill="var(--panel-hi)" stroke="var(--border)"/>' +
      '<circle class="core core-pulse" cx="28" cy="44" r="4.5" fill="' + a + '"/>' +
      '<rect x="6" y="34" width="7" height="16" rx="3.5" fill="var(--panel-hi)" stroke="var(--border)" transform="rotate(35 9.5 34)"/>' +
      '<rect x="43" y="34" width="7" height="16" rx="3.5" fill="var(--panel-hi)" stroke="var(--border)" transform="rotate(-35 46.5 34)"/>' +
      '<rect x="16" y="56" width="24" height="9" rx="3" fill="var(--bg)" stroke="' + a + '" stroke-width="1"/>' +
      '<rect x="17" y="68" width="8" height="16" rx="3" fill="var(--panel-hi)" stroke="var(--border)"/>' +
      '<rect x="31" y="68" width="8" height="16" rx="3" fill="var(--panel-hi)" stroke="var(--border)"/>' +
      '</svg>'
    );
  }

  function zMarks() {
    return '<div class="zmarks"><span>Z</span><span>z</span><span>z</span></div>';
  }

  var POSE_LABEL = {
    sleeping: "SLEEPING", "not-configured": "NOT CONFIGURED", working: "WORKING",
    receiving: "RECEIVING", completed: "COMPLETED", error: null, // error keeps the real status text
  };

  function workstation(agent) {
    var pose = poseFor(agent.status);
    var cls = pillClass(agent.status);
    var accentVar = "--" + cls;
    var metaParts = [];
    metaParts.push("evento " + (agent.current_event ? '<span class="mono">' + escapeHtml(agent.current_event) + "</span>" : "—"));
    metaParts.push("ativ. " + (agent.last_activity ? '<span class="mono">' + escapeHtml(agent.last_activity.replace(/^.*T/, "").replace(/Z$/, "")) + "</span>" : "—"));
    if (agent.last_error) metaParts.push('<span style="color:var(--err)">' + escapeHtml(agent.last_error) + "</span>");
    var meta = metaParts.join(" · ");

    return (
      '<div class="workstation pose-' + pose + '" data-agent-id="' + escapeHtml(agent.id) + '">' +
      '<div class="spotlight spotlight-' + cls + '"></div>' +
      (pose === "sleeping" ? zMarks() : "") +
      characterSvg(pose, accentVar) +
      '<div class="desk desk-' + cls + (pose === "not-configured" ? " desk-dark" : "") + '">' +
      '<div class="screen"><span class="glyph"></span></div></div>' +
      '<div class="nameplate">' +
      '<div class="n">' + escapeHtml(agent.name) + "</div>" +
      '<div class="r">' + escapeHtml(agent.role) + (agent.model ? " · " + escapeHtml(agent.model) : "") + "</div>" +
      pill(agent.status, POSE_LABEL[pose]) +
      '<div class="ws-meta">' + meta + "</div>" +
      "</div>" +
      "</div>"
    );
  }

  function connectionEl(conn) {
    return (
      '<div class="connection" data-from="' + escapeHtml(conn.fromId) + '" data-to="' + escapeHtml(conn.toId) + '">' +
      '<div class="beam"><span class="pulse"></span></div>' +
      "</div>"
    );
  }

  // agents: the array returned by get_state().agents (display order).
  // topology: the array returned by get_state().agent_connections (explicit
  // from/to pairs - see build_connections() in ui/agents.py). Never throws
  // on an empty/unknown-status list or on a topology entry that references
  // an agent not present here.
  //
  // Layout note: a connector is only drawn in the gap between two agents
  // that are ALSO visually adjacent in `agents` (fromIndex + 1 === toIndex).
  // Every connection currently defined is between adjacent agents, so this
  // covers Phase 1 fully; a future non-adjacent connection would need real
  // routing (out of scope here - see the "events travel between agents"
  // phase in the task spec) and is simply not drawn rather than guessed at.
  function buildRoomHTML(agents, topology) {
    if (!agents || agents.length === 0) {
      return '<div class="room-empty">Sem agentes registados.</div>';
    }
    var connections = buildConnections(agents, topology);
    function connectionBetween(i, j) {
      return connections.find(function (c) { return c.fromIndex === i && c.toIndex === j; });
    }
    var parts = [];
    for (var i = 0; i < agents.length; i++) {
      parts.push(workstation(agents[i]));
      if (i < agents.length - 1) {
        var conn = connectionBetween(i, i + 1);
        if (conn) parts.push(connectionEl(conn));
      }
    }
    return '<div class="room-floor">' + parts.join("") + "</div>";
  }

  // -- Phase 2: agent-to-agent communication animation -----------------------
  // A pulse on a connection's beam is a TRANSIENT event, never a function of
  // agent STATE (see the task spec's section 12 - Qwen being WORKING does not
  // imply a Qwen -> Red Team communication, and Qwen being IDLE does not rule
  // one out). The only two callers are:
  //   - the TEST MODE "TESTAR COMUNICAÇÃO" button (app.js), which calls
  //     triggerCommunication() directly with a synthetic pair;
  //   - processCommunications(), the future real-event path, which resolves
  //     `state.agent_communications` entries (see bridge.py's get_state()
  //     contract) into calls to the same triggerCommunication(). It is
  //     currently always fed an empty list in production - nothing in this
  //     file fabricates a communication that didn't come from that list.
  // Per connection element, at most one pulse animates at a time; a second
  // trigger for a connection that's already mid-pulse is queued rather than
  // restarting/corrupting the in-flight one (task spec section 8).
  var pulseQueues = typeof WeakMap !== "undefined" ? new WeakMap() : null;

  function findConnectionElement(roomEl, fromId, toId) {
    var sel = '.connection[data-from="' + fromId + '"][data-to="' + toId + '"]';
    return roomEl.querySelector(sel);
  }

  function dispatchArrived(el, fromId, toId) {
    // "communication-arrived" is a plain observability hook (nothing in this
    // file listens on it - reactToArrival below is called directly from
    // runPulse so the wake-up stays synchronized with the pulse, per the task
    // spec section 7). Guarded so the same code runs unchanged under the Node
    // test suite (no CustomEvent/dispatchEvent there).
    if (typeof el.dispatchEvent !== "function" || typeof CustomEvent === "undefined") return;
    el.dispatchEvent(new CustomEvent("communication-arrived", { detail: { from: fromId, to: toId }, bubbles: true }));
  }

  function runPulse(roomEl, el, fromId, toId, durationMs, opts) {
    el.classList.add("communicating");
    setTimeout(function () {
      el.classList.remove("communicating");
      dispatchArrived(el, fromId, toId);
      // The receiver's wake-up starts exactly here - when the pulse actually
      // reaches `to` - never earlier (task spec section 7). `opts.resolveAgent`
      // is how the caller (app.js) hands us the CURRENT backend agent list
      // without this file ever importing/caching state of its own.
      if (opts && typeof opts.resolveAgent === "function") {
        reactToArrival(roomEl, toId, opts.resolveAgent, opts);
      }
      var queue = pulseQueues && pulseQueues.get(el);
      if (queue && queue.length) {
        var next = queue.shift();
        runPulse(roomEl, el, next.fromId, next.toId, next.durationMs, next.opts);
      }
    }, durationMs || 1400);
  }

  // Pulses roomEl's [fromId -> toId] connection, then returns it to the calm
  // static state. Returns false (and touches nothing) if no such connection
  // is currently rendered - an unknown/renamed agent pair is ignored safely,
  // never guessed at. This is the one function both TEST MODE and the real
  // event path (processCommunications) share.
  //
  // opts (all optional):
  //   resolveAgent(id) -> agent object | undefined - current backend agent,
  //     used to drive the receiver's wake-up reaction (see reactToArrival).
  //     Omit it (as no caller other than app.js's tick()/TEST MODE button
  //     does) and the pulse plays with no receiver reaction at all - this is
  //     how the old Phase 2 unit tests below still pass unmodified.
  //   force: true - bypass the "only wake a genuinely sleeping receiver" gate
  //     (task spec section 14/15). Only the TEST MODE demo button sets this.
  function triggerCommunication(roomEl, fromId, toId, durationMs, opts) {
    var el = findConnectionElement(roomEl, fromId, toId);
    if (!el) return false;
    if (el.classList.contains("communicating")) {
      if (pulseQueues) {
        var queue = pulseQueues.get(el) || [];
        queue.push({ fromId: fromId, toId: toId, durationMs: durationMs, opts: opts });
        pulseQueues.set(el, queue);
      }
      return true;
    }
    runPulse(roomEl, el, fromId, toId, durationMs, opts);
    return true;
  }

  // The real-event entry point: resolves each unseen {id, from, to} in
  // `communications` (state.agent_communications from get_state()) to a
  // connection and pulses it exactly once, ever - `seenIds` is a Set the
  // caller keeps across polling ticks (app.js), so the same communication_id
  // reappearing on the next tick is a no-op rather than a replayed animation
  // (task spec section 10). An entry naming an unknown/unrendered from/to
  // pair is marked seen and otherwise ignored, never thrown on. Returns the
  // list of communication ids actually seen for the first time this call.
  //
  // resolveAgent(id) -> agent object | undefined, same contract as
  // triggerCommunication's opts.resolveAgent - threaded through so every real
  // communication also drives the receiver wake-up (never forced/opts.force,
  // that stays TEST-MODE-only).
  function processCommunications(roomEl, communications, seenIds, durationMs, resolveAgent) {
    var newlySeen = [];
    (communications || []).forEach(function (comm) {
      if (!comm || comm.id === undefined || comm.id === null) return;
      if (seenIds.has(comm.id)) return;
      seenIds.add(comm.id);
      newlySeen.push(comm.id);
      triggerCommunication(roomEl, comm.from, comm.to, durationMs, resolveAgent ? { resolveAgent: resolveAgent } : undefined);
    });
    return newlySeen;
  }

  // -- Phase 3: receiver wake-up reaction -------------------------------------
  // IMPORTANT - state separation (task spec section 2/13): this ONLY ever
  // touches the DOM. It never writes to `state.agents`/get_state() and never
  // invents a backend status. "WAKING" is a presentation-only stage; the
  // stage the workstation settles on afterwards is always re-derived from
  // resolveAgent(toId) - i.e. from whatever the backend says *at the moment
  // the wake-up finishes*, not from a snapshot taken when the pulse left the
  // sender. If the backend has moved the agent to ERROR/NOT_CONFIGURED/etc.
  // in the meantime, that is what gets rendered - never WORKING.
  var WAKE_MS = 700; // SLEEPING -> WAKING transition, within the spec's 500-1500ms window
  var wakeTimers = typeof WeakMap !== "undefined" ? new WeakMap() : null;

  function findWorkstationElement(roomEl, agentId) {
    return roomEl.querySelector('.workstation[data-agent-id="' + agentId + '"]');
  }

  function cancelPendingWake(el) {
    if (!wakeTimers) return;
    var existing = wakeTimers.get(el);
    if (existing) {
      clearTimeout(existing);
      wakeTimers.delete(el);
    }
  }

  // Re-renders one workstation element in place with backend-authoritative
  // markup (via the same `workstation()` builder buildRoomHTML uses) - a
  // single-element swap, not a room rebuild (task spec section 16).
  function settleWorkstation(el, agent) {
    var html = workstation(agent);
    if (typeof el.outerHTML === "string" || "outerHTML" in el) {
      el.outerHTML = html;
    }
  }

  // Drives one receiver's SLEEPING -> WAKING -> (backend-derived) transition.
  // Returns false (touches nothing) when:
  //   - `toId` isn't a rendered workstation (unknown/renamed agent - never
  //     guessed at, same rule as triggerCommunication's connection lookup);
  //   - resolveAgent(toId) has nothing for it (no agent to reconcile to);
  //   - the receiver isn't actually in the SLEEPING pose right now and
  //     `opts.force` wasn't set (task spec 14/15 - a NOT_CONFIGURED or ERROR
  //     agent does not visibly wake just because a communication arrived).
  // `opts.force` (TEST MODE only) skips that last guard so the demo can show
  // the full SLEEPING -> WAKING -> WORKING sequence even against a receiver
  // whose real status is NOT_CONFIGURED/IDLE - it still reconciles to the
  // REAL backend state afterwards, exactly like the production path; nothing
  // is written anywhere, it is a strictly longer-lived presentation stage.
  function reactToArrival(roomEl, toId, resolveAgent, opts) {
    var el = findWorkstationElement(roomEl, toId);
    if (!el) return false;
    var agent = resolveAgent(toId);
    if (!agent) return false;
    var forced = !!(opts && opts.force);
    if (!forced && poseFor(agent.status) !== "sleeping") return false;

    cancelPendingWake(el);
    el.classList.add("waking");
    var wakeMs = (opts && opts.wakeMs) || WAKE_MS;

    var timer = setTimeout(function () {
      if (forced) {
        // Presentation-only "looks like it's working" beat for the demo -
        // built from a synthetic status label solely to reuse the existing
        // workstation()/characterSvg() working pose, never sent anywhere and
        // never mistaken for real data (settleWorkstation() below always
        // has the final word, and it reads the real agent).
        settleWorkstation(el, { id: agent.id, name: agent.name, model: agent.model, role: agent.role, status: "PROCESSING", current_event: agent.current_event, last_activity: agent.last_activity, last_error: agent.last_error });
        var holdMs = (opts && opts.holdMs) || WAKE_MS;
        var el2 = findWorkstationElement(roomEl, toId); // settleWorkstation replaced `el`
        var timer2 = setTimeout(function () {
          if (wakeTimers) wakeTimers.delete(el2);
          settleWorkstation(el2, resolveAgent(toId) || agent);
        }, holdMs);
        if (wakeTimers && el2) wakeTimers.set(el2, timer2);
      } else {
        if (wakeTimers) wakeTimers.delete(el);
        settleWorkstation(el, resolveAgent(toId) || agent);
      }
    }, wakeMs);
    if (wakeTimers) wakeTimers.set(el, timer);
    return true;
  }

  return {
    pillClass: pillClass,
    pill: pill,
    poseFor: poseFor,
    buildConnections: buildConnections,
    buildRoomHTML: buildRoomHTML,
    triggerCommunication: triggerCommunication,
    processCommunications: processCommunications,
    reactToArrival: reactToArrival,
    escapeHtml: escapeHtml,
  };
});
