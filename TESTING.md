# Testing

## Standard verification

`python scripts/run_tests.py` is the standard isolated test runner; it is not an OS network sandbox. It builds child-only configuration before imports, places every configured runtime artifact under a disposable state directory, clears inherited provider credentials and ntfy destinations, preserves the rest of the user environment, and returns the actual unittest exit code. The suite uses `unittest`, not pytest.

From the repository root:

```powershell
python scripts/run_tests.py
python scripts/run_quality.py   # ruff, mypy, import boundaries
```

Direct Node checks when changing room/toggle logic:

```powershell
node --test tests/ui_tests/js/test_agent_room.mjs
node --test tests/ui_tests/js/test_test_mode.mjs
```

Do not hide missing Node behind a Python green result. The Python wrappers skip when Node is absent; UI acceptance requires Node execution. Do not globally disable popup behaviour just to make tests pass: the regression must still verify that importing `ui` establishes the existing override before config loads.

The current counts are in the README's Status section. Every run should be offline: no network and no model calls.

## Strategy

- Pure deterministic features: known analytical examples, malformed data, boundary conditions, invariance under unrelated symbols/quotes, and parameter perturbation when a parameter changes policy or accounting.
- Store/controller: temporary databases, two connections for concurrency, atomic claims/budgets, idempotency, crash/recovery, fenced late results, migration tests on disposable copies.
- External adapters: recorded/synthetic public-response fixtures and injected clients. No production Kraken, Ollama, Claude, ntfy, clipboard or desktop interaction in ordinary automated tests.
- Models: schema, evidence scope, risk-authority rejection, deadline and retry tests use fake adapters. Real model evaluation is a separately registered experiment, not a unit-test side effect.
- UI: Node contract/state tests plus Python API isolation. A JS visibility toggle test alone does not prove backend isolation. Source/frozen visual and lifecycle smoke tests require a separately controlled environment.
- Outcomes: chronological and instrument alignment, both-leg cost identity, missing-label behaviour, delayed publication, repeatable cohort assignment and full denominator accounting.

Use only appropriate checks; do not mass-add tests that mirror trivial implementation. Keep the full regression suite because this package has substantial cross-module import-time configuration.

## Reporting

Every verification run records the command, environment, counts, exit code, skips and unexpected effects. Categories: NEW REGRESSION (introduced), PRE-EXISTING (observed before), ENVIRONMENTAL (depends on host/setup), EXPECTED (explicitly specified outcome, not a blanket waiver). Multiple labels can apply. Preserve failures and explain each.
