// Regression tests for the TEST MODE visibility toggle's pure logic
// (ui/web/test_mode.js). No DOM/browser needed - computeTestModeView takes
// plain values in, returns plain values out, so this exercises the exact
// same function app.js calls, with Node's built-in test runner (no new
// dependency to install).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { computeTestModeView, createTestModeSession } = require("../../../ui/web/test_mode.js");

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
  const end = source.indexOf("// -- sistema", start);
  assert.ok(start >= 0 && end > start, "simulation handler must remain a bounded UI-only section");
  const handler = source.slice(start, end);
  assert.match(handler, /testModeSession\.nextSimulatedCommunication/);
  assert.match(handler, /testModeSession\.simulatedAgent/);
  assert.equal(handler.includes("pywebview.api"), false);
});
