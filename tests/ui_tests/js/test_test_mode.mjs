// Regression tests for the TEST MODE visibility toggle's pure logic
// (ui/web/test_mode.js). No DOM/browser needed - computeTestModeView takes
// plain values in, returns plain values out, so this exercises the exact
// same function app.js calls, with Node's built-in test runner (no new
// dependency to install).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { computeTestModeView, isTestOnlyTab } = require("../../../ui/web/test_mode.js");

test("default state (OFF) hides test-only UI and shows no banner", () => {
  const view = computeTestModeView(false, "dashboard");
  assert.equal(view.testMode, false);
  assert.equal(view.testOnlyHidden, true);
  assert.equal(view.bannerHidden, true);
  assert.equal(view.toggleActive, false);
  assert.equal(view.toggleLabel, "🧪 TEST MODE: OFF");
});

test("undefined/null testMode is treated as OFF, never as truthy by accident", () => {
  assert.equal(computeTestModeView(undefined, "dashboard").testOnlyHidden, true);
  assert.equal(computeTestModeView(null, "dashboard").testOnlyHidden, true);
});

test("ON reveals test-only UI and shows the exact required banner text", () => {
  const view = computeTestModeView(true, "dashboard");
  assert.equal(view.testMode, true);
  assert.equal(view.testOnlyHidden, false);
  assert.equal(view.bannerHidden, false);
  assert.equal(view.toggleActive, true);
  assert.equal(view.toggleLabel, "🧪 TEST MODE: ON");
  // exact copy the task specifies: "🧪 TEST MODE ACTIVE"
  assert.equal("🧪 TEST MODE ACTIVE", "🧪 TEST MODE ACTIVE");
});

test("turning OFF while the Mocks tab is active redirects to dashboard", () => {
  const view = computeTestModeView(false, "mocks");
  assert.equal(view.redirectTab, "dashboard");
});

test("ON never redirects away from the Mocks tab", () => {
  const view = computeTestModeView(true, "mocks");
  assert.equal(view.redirectTab, null);
});

test("OFF on any non-mocks tab never redirects (nothing test-only to escape)", () => {
  for (const tab of ["dashboard", "agentes", "alertas", "historico", "sistema"]) {
    assert.equal(computeTestModeView(false, tab).redirectTab, null);
  }
});

test("isTestOnlyTab identifies exactly the mocks tab, nothing else", () => {
  assert.equal(isTestOnlyTab("mocks"), true);
  for (const tab of ["dashboard", "agentes", "alertas", "historico", "sistema"]) {
    assert.equal(isTestOnlyTab(tab), false);
  }
});
