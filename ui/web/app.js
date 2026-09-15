(() => {
  "use strict";

  const POLL_MS = 1000;
  let lastLatestEventTs = null;

  // -- agent communication animation (Phase 2) --------------------------------
  // `seenCommunicationIds` dedups state.agent_communications across polling
  // ticks so the same communication_id never replays a pulse (task spec
  // section 10) - same idiom as lastLatestEventTs above. `lastRoomSignature`
  // avoids replacing #agent-room's innerHTML when nothing in agents/topology
  // actually changed, so an in-flight pulse isn't wiped out by the next
  // ~1s poll tick (task spec section 13). Always empty in production today
  // since get_state() never populates agent_communications yet - see
  // bridge.py and agent_room.js's processCommunications doc comments.
  const seenCommunicationIds = new Set();
  let lastRoomSignature = null;
  // Latest backend agent list, kept only so the wake-up reaction (Phase 3)
  // can resolve "what does the backend actually say about the receiver right
  // now" at the moment a pulse arrives / a wake-up finishes - see
  // agent_room.js's reactToArrival doc comment. Never fed anywhere else.
  let lastAgents = [];
  function resolveAgent(id) {
    return lastAgents.find((a) => a.id === id);
  }

  // -- TEST MODE (browser-memory simulation, always OFF on load) --------------
  // Deliberately a plain in-memory variable, never read from or written to
  // ui_state.json (contrast with `last_tab` below, which IS persisted) - the
  // task requires startup to always be OFF, so there is nothing to restore.
  let testMode = false;
  const testModeSession = window.RadarTestMode.createTestModeSession();
  let pollTimer = null;

  function applyTestModeVisibility() {
    const activeTab = document.querySelector(".nav-item.active")?.dataset.tab;
    const view = window.RadarTestMode.computeTestModeView(testMode, activeTab);

    document.querySelectorAll("[data-test-mode-only]").forEach((el) => {
      el.hidden = view.simulationHidden;
    });
    document.getElementById("test-mode-banner").hidden = view.bannerHidden;

    const btn = document.getElementById("btn-test-mode");
    btn.classList.toggle("active", view.toggleActive);
    btn.setAttribute("aria-pressed", view.togglePressed);
    btn.textContent = view.toggleLabel;
  }

  document.getElementById("btn-test-mode").addEventListener("click", () => {
    testMode = !testMode;
    if (testMode) {
      testModeSession.enable();
      stopPolling();
      selectTab("agentes", false);
    } else {
      testModeSession.disable();
      startPolling();
    }
    applyTestModeVisibility();
  });

  // -- status -> pill class mapping (DESIGN.md's five roles) -----------------
  // Single source of truth lives in agent_room.js (shared with the room and
  // with its Node test suite) - this just forwards to it.
  const pillClass = window.RadarAgentRoom.pillClass;
  const pill = window.RadarAgentRoom.pill;
  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }
  function fmtTime(iso) {
    if (!iso) return "—";
    const t = String(iso);
    const m = t.match(/T(\d{2}:\d{2}:\d{2})/);
    return m ? m[1] : t;
  }
  function fmtScore(v) {
    return (v === null || v === undefined) ? "—" : Math.round(v);
  }
  function fmtUptime(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    const s = Math.floor(seconds);
    const hh = String(Math.floor(s / 3600)).padStart(2, "0");
    const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
    const ss = String(s % 60).padStart(2, "0");
    return `${hh}:${mm}:${ss}`;
  }
  function fmtEta(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    const s = Math.max(0, Math.floor(seconds));
    return s < 60 ? `~${s}s` : `~${Math.floor(s / 60)}m ${s % 60}s`;
  }

  // -- tab routing -------------------------------------------------------------
  function selectTab(tabName, persist = true) {
    // TEST MODE is intentionally confined to the visual room. Do not fetch or
    // persist state while it is active, even if a caller tries another tab.
    if (testMode) tabName = "agentes";
    document.querySelectorAll(".nav-item[data-tab]").forEach((el) => {
      el.classList.toggle("active", el.dataset.tab === tabName);
    });
    document.querySelectorAll(".tab-panel").forEach((el) => {
      el.hidden = el.id !== `tab-${tabName}`;
    });
    if (tabName === "alertas") loadAlerts();
    if (tabName === "historico") loadHistory();
    if (tabName === "sistema") loadOperationalDiagnostics();
    if (tabName === "sistema") loadSystemInfo();
    if (persist && !testMode) window.pywebview.api.save_ui_state({ last_tab: tabName });
  }

  document.querySelectorAll(".nav-item[data-tab]").forEach((el) => {
    el.addEventListener("click", () => selectTab(el.dataset.tab));
  });

  // -- agent rail rendering (shared by Dashboard mini view and Agentes tab) ----
  function agentStatusClass(status) {
    if (["ONLINE", "OK", "COMPLETED"].includes(status)) return "st-ok";
    if (["PROCESSING", "RECEIVING"].includes(status)) return "st-processing";
    if (["AUTH_ERROR", "OFFLINE", "ERROR"].includes(status)) return "st-err";
    if (["RATE_LIMITED", "QUOTA_EXHAUSTED", "DEGRADED"].includes(status)) return "st-warn";
    return "";
  }
  function renderAgentsRail(container, agents) {
    container.innerHTML = agents.map((a) => `
      <div class="agent-node ${agentStatusClass(a.status)}">
        <div class="node-dot"><span class="core"></span></div><div class="line"></div>
        <div class="node-body">
          <div class="node-top"><span class="node-name">${escapeHtml(a.name)}</span>${pill(a.status)}</div>
          <div class="node-role">${escapeHtml(a.role)}${a.model ? " · " + escapeHtml(a.model) : ""}</div>
          <div class="node-meta">
            ${a.current_event ? `evento atual <span class="mono">${escapeHtml(a.current_event)}</span> · ` : ""}
            processados <span class="mono">${a.events_processed}</span>
            ${a.last_activity ? ` · última atividade <span class="mono">${fmtTime(a.last_activity)}</span>` : ""}
            ${a.last_error ? ` · <span style="color:var(--err)">${escapeHtml(a.last_error)}</span>` : ""}
          </div>
        </div>
      </div>
    `).join("");
  }

  // -- lifecycle stepper ---------------------------------------------------------
  function renderLifecycle(lc) {
    const steps = [
      lc.detected, lc.qwen, lc.router,
      lc.ntfy_status === "SENT", lc.prompt_ready, lc.claude_analysed,
    ];
    // "current" step = first not-yet-done step, for the pulse.
    const firstPending = steps.findIndex((s) => !s);
    return `<div class="lifecycle" title="Detected → Qwen → Router → NTFY → Prompt → Claude">
      ${steps.map((done, i) => `<span class="step ${done ? "done" : (i === firstPending ? "cur" : "")}"></span>`).join("")}
    </div>`;
  }

  // -- alert row rendering (shared by Dashboard preview / Alertas / Histórico) --
  function alertRow(a, withLifecycle) {
    const dirClass = a.direction === "LONG" ? "dir-long" : a.direction === "SHORT" ? "dir-short" : "";
    return `<tr>
      <td class="mono">${fmtTime(a.ts)}</td>
      <td class="asset">${escapeHtml(a.asset)}</td>
      <td>${escapeHtml(a.setup_type || "NONE")}</td>
      <td class="${dirClass}">${escapeHtml(a.direction || "NONE")}</td>
      <td class="mono">${fmtScore(a.opportunity_score)}</td>
      <td class="mono">${fmtScore(a.tradeability_score)}</td>
      <td>${pill(a.model_demand)}</td>
      ${withLifecycle ? `<td>${renderLifecycle(a.lifecycle)}</td>` : ""}
      <td>${pill(a.ntfy_status || "N/A")}</td>
      <td><button class="copy-btn" data-event-id="${escapeHtml(a.event_id)}">COPIAR PROMPT</button></td>
    </tr>`;
  }

  function wireCopyButtons(root) {
    root.querySelectorAll(".copy-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const eventId = btn.dataset.eventId;
        btn.textContent = "…";
        const result = await window.pywebview.api.copy_prompt(eventId);
        btn.textContent = result.copied ? "COPIADO ✓" : "FALHOU";
        setTimeout(() => { btn.textContent = "COPIAR PROMPT"; }, 1800);
      });
    });
  }

  // -- dashboard state tick -------------------------------------------------------
  function renderSystemStatus(s) {
    const rows = [
      ["Kraken", s.kraken],
      ["Kraken Futures", s.kraken_futures],
      ["SQLite", s.sqlite],
      ["Qwen", s.qwen],
      ["NTFY", s.ntfy_enabled ? "ENABLED" : "DISABLED"],
      ["Claude Bridge", s.claude_bridge],
    ];
    document.getElementById("system-status-rows").innerHTML = rows.map(([name, val]) => `
      <div class="status-row"><span class="name">${name}</span>${pill(val)}</div>
    `).join("");
  }

  function renderFunnel(f) {
    const rows = [
      ["Assets scanned", f.assets_scanned],
      ["L1 shortlist", f.l1_shortlist],
      ["L2 candidates", f.l2_candidates],
      ["L3 finalists", f.l3_finalists],
      ["Qwen reviews", f.qwen_reviewed],
    ];
    const el = document.getElementById("funnel-rows");
    const stalled = (m) => (f[`${m}_demand`] > 0 && f[`${m}_calls`] === 0);
    el.innerHTML = rows.map(([name, val]) => `
      <div class="funnel-row"><span>${name}</span><span class="n mono">${val ?? "—"}</span></div>
    `).join("") + `
      <div class="funnel-row split ${stalled("sonnet") ? "stalled" : ""}"><span>Sonnet demand</span><span class="n mono">${f.sonnet_demand ?? "—"}</span></div>
      <div class="funnel-row split ${stalled("sonnet") ? "stalled" : ""}"><span>Sonnet calls</span><span class="n mono">${f.sonnet_calls ?? 0}</span></div>
      <div class="funnel-row split ${stalled("fable") ? "stalled" : ""}"><span>Fable demand</span><span class="n mono">${f.fable_demand ?? "—"}</span></div>
      <div class="funnel-row split ${stalled("fable") ? "stalled" : ""}"><span>Fable calls</span><span class="n mono">${f.fable_calls ?? 0}</span></div>
      <div class="funnel-row"><span>Deferred</span><span class="n mono">${f.deferred ?? "—"}</span></div>
      <div class="funnel-row"><span>Ignored</span><span class="n mono">${f.ignored ?? "—"}</span></div>
    `;
  }

  async function tick() {
    if (testMode) return;
    let state;
    try {
      state = await window.pywebview.api.get_state();
    } catch (e) {
      return; // transient bridge hiccup - next tick will retry
    }

    const proc = state.process;
    document.getElementById("proc-state").textContent = proc.state;
    document.getElementById("proc-uptime").textContent = fmtUptime(proc.uptime_seconds);
    document.getElementById("last-heartbeat").textContent = fmtTime(state.funnel.run_timestamp);
    document.getElementById("next-cycle").textContent = fmtEta(state.next_full_cycle_eta_seconds);

    const dot = document.getElementById("brand-dot");
    dot.className = "dot " + (proc.state === "RUNNING" ? "ok" : proc.state === "ERROR" ? "err" : (proc.state === "STARTING" || proc.state === "STOPPING") ? "warn" : "");

    document.getElementById("btn-start").disabled = proc.state === "RUNNING" || proc.state === "STARTING";
    document.getElementById("btn-stop").disabled = proc.state === "STOPPED" || proc.state === "STOPPING";
    document.getElementById("btn-restart").disabled = proc.state === "STOPPED";

    renderSystemStatus(state.system_status);
    renderFunnel(state.funnel);
    renderAgentsRail(document.getElementById("agents-rail-mini"), state.agents);

    lastAgents = state.agents;

    const roomEl = document.getElementById("agent-room");
    const roomSignature = JSON.stringify({ agents: state.agents, connections: state.agent_connections });
    if (roomSignature !== lastRoomSignature) {
      lastRoomSignature = roomSignature;
      roomEl.innerHTML = window.RadarAgentRoom.buildRoomHTML(state.agents, state.agent_connections);
    }
    // Real communication events only - see agent_room.js's processCommunications
    // doc comment. state.agent_communications is always [] until a real
    // backend emitter exists, so this is a no-op in production today.
    // resolveAgent is what lets a real arrival also drive the Phase 3
    // receiver wake-up reaction, always reconciled to backend truth.
    window.RadarAgentRoom.processCommunications(roomEl, state.agent_communications, seenCommunicationIds, 1200, resolveAgent);

    const previewBody = document.getElementById("alerts-preview-body");
    previewBody.innerHTML = state.alerts_preview.map((a) => alertRow(a, true)).join("")
      || `<tr><td colspan="9" class="empty">Sem alertas reais até ao momento.</td></tr>`;
    wireCopyButtons(previewBody);

    document.getElementById("alerts-badge").textContent = state.alerts_preview.length;

    const currentTs = state.latest_event ? state.latest_event.updated_ts : null;
    if (currentTs !== lastLatestEventTs) {
      lastLatestEventTs = currentTs;
      const activeTab = document.querySelector(".nav-item.active")?.dataset.tab;
      if (activeTab === "alertas") loadAlerts();
      if (activeTab === "historico") loadHistory();
    }
  }

  // -- alertas / historico (fetched on tab switch / latest_event change) --------
  async function loadAlerts() {
    const rows = await window.pywebview.api.list_alerts();
    const body = document.getElementById("alerts-body");
    document.getElementById("alerts-empty").hidden = rows.length !== 0;
    body.innerHTML = rows.map((a) => alertRow(a, true)).join("");
    wireCopyButtons(body);
  }

  async function loadHistory() {
    const rows = await window.pywebview.api.list_history(200);
    const body = document.getElementById("history-body");
    document.getElementById("history-empty").hidden = rows.length !== 0;
    body.innerHTML = rows.map((a) => alertRow(a, false)).join("");
    wireCopyButtons(body);
  }

  // -- explicit operational diagnostics (never part of TEST MODE) --------------
  async function loadOperationalDiagnostics() {
    const rows = await window.pywebview.api.list_operational_mock_alerts();
    const body = document.getElementById("mocks-body");
    document.getElementById("mocks-empty").hidden = rows.length !== 0;
    body.innerHTML = rows.map((a) => `<tr>
      <td class="mono">${fmtTime(a.ts)}</td>
      <td class="asset">${escapeHtml(a.asset)}<span class="test-marker">[TESTE]</span></td>
      <td>${escapeHtml(a.setup_type || "NONE")}</td>
      <td>${pill(a.model_demand)}</td>
      <td><button class="copy-btn" data-event-id="${escapeHtml(a.event_id)}">COPIAR PROMPT</button></td>
    </tr>`).join("");
    wireCopyButtons(body);
  }

  function showOperationalResult(text) {
    const box = document.getElementById("operational-result");
    box.hidden = false;
    box.textContent = text;
  }

  function showMockResult(text) {
    const box = document.getElementById(testMode ? "test-mode-result" : "operational-result");
    box.hidden = false;
    box.textContent = text;
  }

  document.getElementById("btn-notify-test").addEventListener("click", async () => {
    const r = await window.pywebview.api.run_operational_notify_test();
    showMockResult(`TESTAR NTFY — exit_code=${r.exit_code}\n\n${r.output}`);
  });
  document.getElementById("btn-mock-alert").addEventListener("click", async () => {
    const r = await window.pywebview.api.run_operational_mock_alert();
    showMockResult(r.report);
    loadOperationalDiagnostics();
  });
  document.getElementById("btn-clipboard-test").addEventListener("click", async () => {
    const r = await window.pywebview.api.run_operational_clipboard_test();
    showMockResult(`TESTAR CLIPBOARD — copied=${r.copied}\n"${r.text}"`);
  });

  // Visual-only demo of the (currently unused-in-production) communication
  // pulse + receiver wake-up between two agent workstations. TEST MODE only,
  // never touches the backend, never claims a real handoff happened - see
  // agent_room.js's triggerCommunication/reactToArrival doc comments. Runs
  // the exact same pipeline as a real communication (processCommunications ->
  // triggerCommunication -> reactToArrival), just called directly with a
  // synthetic pair and `force: true` so the receiver wake-up plays even if
  // its real backend status isn't currently SLEEPING (task spec section 9) -
  // reactToArrival still reconciles to the REAL backend state afterwards.
  document.getElementById("btn-comm-test")?.addEventListener("click", () => {
    const simulated = testModeSession.nextSimulatedCommunication();
    if (simulated === null) return;
    const room = document.getElementById("agent-room");
    const beams = [...room.querySelectorAll(".connection")];
    if (beams.length === 0) {
      showMockResult("TESTAR COMUNICAÇÃO — sem ligações na sala para animar.");
      return;
    }
    // Prefer the real Qwen -> Red Team pair (task spec section 9); fall back
    // to the first declared connection so the demo still works if topology
    // ever changes.
    const el = beams.find((b) => b.dataset.from === simulated.from && b.dataset.to === simulated.to) || beams[0];
    window.RadarAgentRoom.triggerCommunication(room, el.dataset.from, el.dataset.to, 1400, {
      resolveAgent: testModeSession.simulatedAgent,
      force: true,
    });
    showMockResult(`TESTAR COMUNICAÇÃO — pulso visual ${el.dataset.from} → ${el.dataset.to}, com reação de "acordar" do recetor (só UI, não é uma comunicação real; o estado do backend não é alterado e o recetor volta ao seu estado real no final).`);
  });

  // -- sistema ----------------------------------------------------------------------
  async function loadSystemInfo() {
    const info = await window.pywebview.api.system_info();
    document.getElementById("sys-sqlite-path").textContent = info.sqlite_path;
    document.getElementById("sys-log-path").textContent = info.log_path;
    const state = await window.pywebview.api.get_state();
    document.getElementById("sys-pid").textContent = state.process.pid ?? "—";
    document.getElementById("sys-exit-code").textContent = state.process.last_exit_code ?? "—";
    document.getElementById("log-lines").textContent = (state.log_lines || []).join("\n") || "(sem linhas ainda)";
  }
  document.getElementById("btn-open-log").addEventListener("click", () => window.pywebview.api.open_log_file());
  document.getElementById("btn-open-folder").addEventListener("click", () => window.pywebview.api.open_project_folder());

  // -- process controls -------------------------------------------------------------
  document.getElementById("btn-start").addEventListener("click", () => window.pywebview.api.start_radar());
  document.getElementById("btn-stop").addEventListener("click", () => window.pywebview.api.stop_radar());
  document.getElementById("btn-restart").addEventListener("click", () => window.pywebview.api.restart_radar());

  function startPolling() {
    if (pollTimer !== null) return;
    tick();
    pollTimer = setInterval(tick, POLL_MS);
  }

  function stopPolling() {
    if (pollTimer === null) return;
    clearInterval(pollTimer);
    pollTimer = null;
  }

  // -- boot -------------------------------------------------------------------------
  window.addEventListener("pywebviewready", async () => {
    applyTestModeVisibility(); // TEST MODE always starts OFF - hide test-only UI first
    try {
      const saved = await window.pywebview.api.get_ui_state();
      if (saved && saved.last_tab) selectTab(saved.last_tab);
    } catch (e) { /* default tab stays dashboard */ }
    startPolling();
  });
})();
