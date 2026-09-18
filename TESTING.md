# Testing

## Takeover baseline — before changes

2026-09-14, Windows, Python 3.12.10, Node 24.14.0. Command: `python -m unittest discover -s tests -v`. This is unittest, not pytest; pytest was not installed in the primary Python environment.

**411 tests run in 33.428 seconds: 410 passed, 1 failed, no skips reported.** Two unittest cases wrap Node suites; 411 is the unittest count, not the sum of every individual JavaScript case. The frozen-launch regression passed in a fresh Python subprocess, not an actual rebuilt executable.

Failure: `test_claude_bridge.TestBridgeHealthDetection.test_no_sdk_and_no_events_reports_offline_without_crashing` expects `OFFLINE`, received `AUTH_ERROR`. Classification: **PRE-EXISTING / ENVIRONMENTAL**. It calls `run_bridge_cycle(env={})` without controlling `_anthropic_available()`. Anthropic 1.5.0 is installed; the implementation therefore reports missing credentials. This is not evidence that a credential should be configured or that production behavior should be changed to match the faulty test.

For the baseline, state/events/SQLite paths were redirected to a new temporary directory, ntfy topic and the API-key variable were cleared in the child process, and bytecode writes were disabled. Test fixtures use temporary databases. Existing desktop-effect test isolation is incomplete: the fresh-process mock regression stubs the popup but not every notification/clipboard effect. T001 addresses that gap. Do not describe the historical baseline as a fully hermetic suite.

Evidence: `docs/audit/2026-09-14/baseline.log`, `source-inventory.json`, `current-schema.json`. The schema was inspected through a SQLite read-only connection; no production data migration or test run against the production DB was performed.

## Standard verification

T001 is PO-accepted. `python scripts/run_tests.py` is the standard isolated runner for the inspected suite; it is not an OS network sandbox. The runner must construct child-only configuration before imports, place every configured runtime artifact under a disposable state directory, clear inherited provider credentials and ntfy destinations, preserve existing user environment, and return the actual unittest exit code.

After T001 acceptance:

```powershell
Set-Location <repo>
python scripts/run_tests.py
```

Direct Node checks when changing room/toggle logic:

```powershell
node --test tests/ui_tests/js/test_agent_room.mjs
node --test tests/ui_tests/js/test_test_mode.mjs
```

Do not hide missing Node behind a Python green result. The current wrappers skip when Node is absent; UI acceptance requires Node execution. Do not globally disable popup behavior just to make tests pass: the regression must still verify that importing `ui` establishes the existing override before config loads.

## Strategy

- Pure deterministic features: known analytical examples, malformed data, boundary conditions, invariance under unrelated symbols/quotes, and parameter perturbation when a parameter changes policy or accounting.
- Store/controller: temporary databases, two connections for concurrency, atomic claims/budgets, idempotency, crash/recovery, fenced late results, migration tests on disposable copies.
- External adapters: recorded/synthetic public-response fixtures and injected clients. No production Kraken, Ollama, Claude, ntfy, clipboard or desktop interaction in ordinary automated tests.
- Models: schema, evidence scope, risk-authority rejection, deadline and retry tests use fake adapters. Real model evaluation is a separately registered experiment, not a unit-test side effect.
- UI: Node contract/state tests plus Python API isolation. A JS visibility toggle test alone does not prove backend isolation. Source/frozen visual and lifecycle smoke tests require a separately controlled environment.
- Outcomes: chronological and instrument alignment, both-leg cost identity, missing-label behavior, delayed publication, repeatable cohort assignment and full denominator accounting.

Use only appropriate checks; do not mass-add tests that mirror trivial implementation. Keep the full regression suite because this package has substantial cross-module import-time configuration.

## Reporting

Every task records command, environment, counts, exit code, skips, and unexpected effects. Categories: NEW REGRESSION (introduced), PRE-EXISTING (observed before), ENVIRONMENTAL (depends on host/setup), EXPECTED (explicitly specified outcome, not a blanket waiver). Multiple labels can apply, as in the baseline health test. Preserve failures and explain each.

## Post-task results

T001 accepted 2026-09-15: **415 tests passed in 31.994s, exit 0, no skips**. Both Node wrappers ran. PO inspected the four-file diff and ran the runner from <home>, proving repository discovery outside its CWD. The original SDK-dependent failure and desktop-effect fixture gap are fixed without runtime edits. See `docs/tasks/results/T001.md` and `docs/audit/2026-09-14/t001-po-review.log`. A separate log-display wrapper hit a cp1252 Unicode error after the successful test child; this was an environmental display failure, not a failing suite. The historical failure remains recorded above.

T002–T003 and T010–T013 accepted 2026-09-15: environment reproducibility/quality gates and containment hardening increased the accepted full-suite count successively to **420, 427, 431, 434, 441, and 443 tests**, each exit 0 with no skips and both embedded Node wrappers passing. See the individual notes in docs/tasks/results/ for commands, scope, limitations, and commit references. T020 accepted 2026-09-15: **449 tests passed in 31.553s, exit 0, no skips**; both embedded Node wrappers passed. The PO independently re-ran the 18 focused tests and the isolated full runner. See `docs/tasks/results/T020.md`. T021 accepted 2026-09-18 (see below); T022 is the next eligible task.

T004a (Forja run R-20260918-eb85, accepted in-run by Reviewer APPROVE): **472 tests in 33.576s, exit 0, no skips**; both embedded Node suites ran (43 and 7 passing, 0 skipped). The quality gate `scripts/run_quality.py` now inspects 99 files with Ruff and 53 with mypy and passes only against the versioned baseline `scripts/quality_baseline.json`. See `docs/tasks/results/T004.md`.

T004b (Forja run R-20260918-eb85, accepted in-run by Reviewer APPROVE, docs-only): re-ran the same suite unchanged — final attempt: **472 tests, exit 0, no skips** in 3 of 3 runs (35.511s, 34.297s, 33.402s); `node --test tests/ui_tests/js/test_agent_room.mjs` 43/43 pass in 5 of 5 runs and `node --test tests/ui_tests/js/test_test_mode.mjs` 7/7 pass, 0 skipped in either; `scripts/run_quality.py` still exits 0 (same 28 ruff / 32 mypy findings, all covered by the unchanged baseline). The 60 legacy findings in `scripts/quality_baseline.json` are now registered as follow-up tasks T005–T009 in `docs/tasks/TASK_CATALOG.md`, grouped by module (`radar_v08`, `ui`, `tests`, `scripts`, `radar.py`); T021's two deferred findings are T024 (resample_bars alignment test, D8c) and T025 (`_contiguous_suffix`/`contiguous_tail` duplication backlog, D8d). ENVIRONMENTAL/PRE-EXISTING flaky tests, catalogued as **T026** (tests only, not fixed in T004b): `tests/ui_tests/js/test_agent_room.mjs` has two real-timer assertions that fail intermittently — "(5) the same communication id cannot trigger the wake-up twice" (fixed `await wait(720)` against the default wake-up) and "(8) backend state remains authoritative even for TEST MODE's forced demo" (`wakeMs: 5, holdMs: 5`, `await wait(25)`; expected `/pose-not-configured/`, got `pose-working`). Observed during T004b with zero code changes: attempt 1, `scripts/run_tests.py` failed **2 of 3** runs (exit 1, both on (5)) and passed the third; direct `node --test tests/ui_tests/js/test_agent_room.mjs` also fails — **2 of 4** runs (Lead) and **1 of 5** runs (Reviewer, on (8), 42 pass / 1 fail); attempt 2, 3 of 3 `run_tests.py` runs and 5 of 5 direct runs passed. The failure is not caused by this task, but it recurs and can hit the (a)/(b) check of any later task until T026 is done; record every observed run honestly. See `docs/tasks/results/T004.md`.

## Sextant verification scope

247 selected tests passed in 19.34s using `.venv/Scripts/python.exe -m pytest -p no:cacheprovider` against money/time, capabilities, LLM risk boundary, costs, backtest correctness, market data, HTTP transport, registry, preflight, carry accounting and registered-value perturbation modules. Full suite/coverage/type/lint checks were not run. See `docs/audit/2026-09-14/sextant-selected.log` and `docs/SEXTANT_REUSE.md`.
