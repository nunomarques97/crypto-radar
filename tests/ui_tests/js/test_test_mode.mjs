// Regression tests for the TEST MODE visibility toggle's pure logic
// (ui/web/test_mode.js). No DOM/browser needed - computeTestModeView takes
// plain values in, returns plain values out, so this exercises the exact
// same function app.js calls, with Node's built-in test runner (no new
// dependency to install).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { computeTestModeView, createTestModeSession, TAB_IDS, DEFAULT_TAB, resolveTab } = require("../../../ui/web/test_mode.js");
const fs = require("node:fs");
const readUi = (name) => fs.readFileSync(new URL(`../../../ui/web/${name}`, import.meta.url), "utf8");

test("default state (OFF) hides test-only UI and shows no banner", () => {
  const view = computeTestModeView(false, "dashboard");
  assert.equal(view.testMode, false);
  assert.equal(view.simulationHidden, true);
  assert.equal(view.bannerHidden, true);
  assert.equal(view.toggleActive, false);
  assert.equal(view.toggleLabel, "🧪 TEST MODE: OFF");
});

test("undefined/null testMode is treated as OFF, never as truthy by accident", () => {
  assert.equal(computeTestModeView(undefined, "dashboard").simulationHidden, true);
  assert.equal(computeTestModeView(null, "dashboard").simulationHidden, true);
});

test("ON reveals test-only UI and shows the exact required banner text", () => {
  const view = computeTestModeView(true, "dashboard");
  assert.equal(view.testMode, true);
  assert.equal(view.simulationHidden, false);
  assert.equal(view.bannerHidden, false);
  assert.equal(view.toggleActive, true);
  assert.equal(view.toggleLabel, "🧪 TEST MODE: ON");
  // exact required copy: "🧪 TEST MODE ACTIVE"
  assert.equal("🧪 TEST MODE ACTIVE", "🧪 TEST MODE ACTIVE");
});

test("a fresh browser-memory session defaults OFF, so reload resets TEST MODE", () => {
  const firstLoad = createTestModeSession();
  assert.equal(firstLoad.isEnabled(), false);
  assert.equal(firstLoad.nextSimulatedCommunication(), null);
  firstLoad.enable();
  assert.equal(firstLoad.isEnabled(), true);

  const reloaded = createTestModeSession();
  assert.equal(reloaded.isEnabled(), false);
  assert.equal(reloaded.nextSimulatedCommunication(), null);
});

test("simulated events are plain browser-local presentation data", () => {
  const session = createTestModeSession();
  session.enable();
  const first = session.nextSimulatedCommunication();
  const second = session.nextSimulatedCommunication();
  assert.deepEqual(first, {
    id: "test-mode-simulation-1",
    from: "qwen-14b",
    to: "qwen-red-team",
    type: "TEST_MODE_SIMULATION",
    reason: "Browser-local visual simulation only",
  });
  assert.equal(second.id, "test-mode-simulation-2");
  assert.deepEqual(session.simulatedAgent("qwen-red-team"), { id: "qwen-red-team", status: "NOT_CONFIGURED" });
  assert.equal(session.simulatedAgent("unknown"), null);
  session.disable();
  assert.equal(session.nextSimulatedCommunication(), null);
});

test("simulation module contains no backend or persistence escape hatch", () => {
  const source = require("node:fs").readFileSync(new URL("../../../ui/web/test_mode.js", import.meta.url), "utf8");
  for (const forbidden of ["pywebview", "localStorage", "sessionStorage", "fetch(", "XMLHttpRequest"]) {
    assert.equal(source.includes(forbidden), false, `${forbidden} must not be available to TEST MODE simulation`);
  }
});

test("the simulated-handoff UI handler never crosses the backend API boundary", () => {
  const source = require("node:fs").readFileSync(new URL("../../../ui/web/app.js", import.meta.url), "utf8");
  const start = source.indexOf('document.getElementById("btn-comm-test")');
  const end = source.indexOf("// -- system", start);
  assert.ok(start >= 0 && end > start, "simulation handler must remain a bounded UI-only section");
  const handler = source.slice(start, end);
  assert.match(handler, /testModeSession\.nextSimulatedCommunication/);
  assert.match(handler, /testModeSession\.simulatedAgent/);
  assert.equal(handler.includes("pywebview.api"), false);
});

test("the tab ids are English and match index.html's sidebar and sections", () => {
  assert.deepEqual(TAB_IDS, ["dashboard", "agents", "alerts", "history", "game", "system"]);
  assert.equal(DEFAULT_TAB, "dashboard");
  const html = readUi("index.html");
  const navTabs = [...html.matchAll(/class="nav-item[^"]*" data-tab="([^"]+)"/g)].map((m) => m[1]);
  const sections = [...html.matchAll(/<section class="tab-panel" id="tab-([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(navTabs, TAB_IDS);
  assert.deepEqual(sections, TAB_IDS);
  // Only the dashboard is visible before the saved tab is applied.
  assert.match(html, /<section class="tab-panel" id="tab-dashboard">/);
  for (const id of TAB_IDS.slice(1)) assert.match(html, new RegExp(`<section class="tab-panel" id="tab-${id}" hidden>`));
});

test("every current tab id resolves to itself", () => {
  for (const id of TAB_IDS) assert.equal(resolveTab(id), id);
});

test("a legacy Portuguese or unknown saved tab falls back to the dashboard", () => {
  for (const saved of ["agentes", "alertas", "historico", "jogo", "sistema", "mocks", "Dashboard", "AGENTS", " agents", "", null, undefined, 42, {}, ["game"]]) {
    const resolved = resolveTab(saved);
    assert.equal(resolved, "dashboard", `saved=${JSON.stringify(saved)}`);
  }
});

test("a resolved tab always leaves exactly one panel shown", () => {
  const html = readUi("index.html");
  const sections = [...html.matchAll(/<section class="tab-panel" id="(tab-[^"]+)"/g)].map((m) => m[1]);
  for (const saved of [...TAB_IDS, "agentes", "jogo", "unknown-tab", undefined]) {
    // Same rule as app.js's selectTab: a panel is shown only when its id is tab-<resolved id>.
    const shown = sections.filter((id) => id === `tab-${resolveTab(saved)}`);
    assert.equal(shown.length, 1, `saved=${String(saved)}`);
  }
});

test("app.js routes every tab change through resolveTab and TEST MODE still forces the agents tab", () => {
  const source = readUi("app.js");
  const start = source.indexOf("function selectTab(");
  const end = source.indexOf("// -- AI Game (paper game)", start);
  assert.ok(start >= 0 && end > start);
  const body = source.slice(start, end);
  assert.match(body, /tabName = testMode \? "agents" : window\.RadarTestMode\.resolveTab\(tabName\);/);
  assert.ok(body.indexOf("resolveTab") < body.indexOf(".tab-panel"), "the id is resolved before panels are hidden");
  assert.match(body, /el\.hidden = el\.id !== `tab-\$\{tabName\}`/);
  assert.match(body, /if \(persist && !testMode\)/);
  assert.match(source, /testModeSession\.enable\(\);\s*stopPolling\(\);\s*selectTab\("agents", false\);/);
  assert.equal(source.includes('getElementById("tab-game")'), true);
});
