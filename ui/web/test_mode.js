/* Browser-memory TEST MODE simulation. This module deliberately has no DOM,
 * webview bridge, storage, or backend dependency so it can be tested identically
 * under Node and in the browser. app.js renders its returned presentation
 * data, but is not allowed to route it through production API/history.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.RadarTestMode = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  /**
   * @param {boolean} testMode
   * @param {string|null|undefined} activeTab - the currently selected nav tab
   * @returns {{
   *   testMode: boolean,
   *   simulationHidden: boolean,
   *   bannerHidden: boolean,
   *   toggleActive: boolean,
   *   toggleLabel: string,
   *   togglePressed: string
   * }}
   */
  function computeTestModeView(testMode, activeTab) {
    var on = testMode === true;
    return {
      testMode: on,
      // Simulation controls are browser-local presentation only.
      simulationHidden: !on,
      bannerHidden: !on,
      toggleActive: on,
      toggleLabel: on ? "🧪 TEST MODE: ON" : "🧪 TEST MODE: OFF",
      togglePressed: String(on),
    };
  }

  /**
   * Create a fresh browser-memory simulation session. A page reload creates
   * a new instance, so TEST MODE always starts OFF and simulations have no
   * durable identity or production communication history.
   */
  function createTestModeSession() {
    var enabled = false;
    var sequence = 0;

    return {
      enable: function () { enabled = true; },
      disable: function () { enabled = false; },
      isEnabled: function () { return enabled; },
      simulatedAgent: function (id) {
        if (id === "qwen-14b") return { id: id, status: "IDLE" };
        if (id === "qwen-red-team") return { id: id, status: "NOT_CONFIGURED" };
        return null;
      },
      nextSimulatedCommunication: function () {
        if (!enabled) return null;
        sequence += 1;
        return {
          id: "test-mode-simulation-" + sequence,
          from: "qwen-14b",
          to: "qwen-red-team",
          type: "TEST_MODE_SIMULATION",
          reason: "Browser-local visual simulation only",
        };
      },
    };
  }

  return { computeTestModeView: computeTestModeView, createTestModeSession: createTestModeSession };
});
