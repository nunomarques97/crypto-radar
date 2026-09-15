/* Pure TEST MODE visibility logic - no DOM access, so it can be unit tested
 * identically under Node (tests/ui_tests/js/test_test_mode.mjs) and in the
 * browser. app.js is the only place that touches the DOM with this output.
 *
 * TEST MODE is a UI-only visibility toggle: it never calls a different
 * backend method, never changes what mock_alert.run_mock_alert does, and is
 * never persisted (app.js keeps `testMode` as an in-memory variable that
 * always starts `false` on load - see the "no persistence" note there).
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.RadarTestMode = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var MOCKS_TAB = "mocks";
  var FALLBACK_TAB = "dashboard";

  /**
   * @param {boolean} testMode
   * @param {string|null|undefined} activeTab - the currently selected nav tab
   * @returns {{
   *   testMode: boolean,
   *   testOnlyHidden: boolean,
   *   bannerHidden: boolean,
   *   toggleActive: boolean,
   *   toggleLabel: string,
   *   togglePressed: string,
   *   redirectTab: string|null
   * }}
   */
  function computeTestModeView(testMode, activeTab) {
    var on = testMode === true;
    return {
      testMode: on,
      // every [data-test-only] element (Mocks/Testes nav item + its tab panel)
      testOnlyHidden: !on,
      bannerHidden: !on,
      toggleActive: on,
      toggleLabel: on ? "🧪 TEST MODE: ON" : "🧪 TEST MODE: OFF",
      togglePressed: String(on),
      // OFF while the Mocks tab happens to be selected (e.g. restored from a
      // saved last_tab) must never leave test-only content on screen.
      redirectTab: (!on && activeTab === MOCKS_TAB) ? FALLBACK_TAB : null,
    };
  }

  /** True only for the one tab that is test/mock-only - kept in one place
   * so app.js and this module never disagree on what counts as test-only. */
  function isTestOnlyTab(tabName) {
    return tabName === MOCKS_TAB;
  }

  return { computeTestModeView: computeTestModeView, isTestOnlyTab: isTestOnlyTab };
});
