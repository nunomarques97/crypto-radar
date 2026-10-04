# Testing

## Standard verification

`python scripts/run_tests.py` is the standard isolated test runner; it is not an OS network sandbox. It builds child-only configuration before imports, places every configured runtime artifact under a disposable state directory, clears inherited provider credentials and ntfy destinations, preserves the rest of the user environment, and returns the actual unittest exit code. The suite uses `unittest`, not pytest.

From the repository root:

```powershell
python scripts/run_tests.py
python scripts/run_quality.py   # ruff, mypy, import boundaries
```

The runner is parallel by default: it discovers the same test set as `unittest discover -s tests`, runs each module (or, for the modules in `SPLIT_MODULES`, each test class) in its own worker process with its own disposable state directory, and exits 0 only when every discovered id ran exactly once and passed or was skipped. `--jobs N` sets the worker count (default: CPU count), `--serial` runs a single `python -m unittest discover -s tests -v` process, and `--list` prints the discovered ids without running them. On 2026-10-04 `--list` reports `2693 tests discovered in 122 modules`, and a full parallel run with the default 16 workers took about 56 s.

Direct Node checks when changing room/toggle logic:

```powershell
node --test tests/ui_tests/js/test_agent_room.mjs
node --test tests/ui_tests/js/test_test_mode.mjs
```

Do not hide missing Node behind a Python green result. The Python wrappers skip when Node is absent; UI acceptance requires Node execution. Do not globally disable popup behaviour just to make tests pass: the regression must still verify that importing `ui` establishes the existing override before config loads.

## Trend paper tests

The research-only trend paper module (see [docs/guides/TREND-PAPER.md](docs/guides/TREND-PAPER.md)) is covered by these modules. The counts are the ids that `python -B scripts/run_tests.py --list` discovered on 2026-10-04 (401 in total). Every module runs offline: the market adapters are exercised over fake sessions (`tests/trend_paper_fakes.py`) and vendored fixtures under `tests/fixtures/trend/`.

| Module | Tests | Covers |
|---|---:|---|
| `tests/test_trend_engine.py` | 35 | Engine timing, costs, validation, per-day lookahead audit (`LookaheadAudit`), FIFO tax rules, metrics, rule arithmetic, module boundary |
| `tests/test_trend_parity.py` | 6 | Parity with the research references to 1e-9 on vendored data (`RobustParity`, `HarnessParity`) and sha256 binding of the rule sources |
| `tests/test_trend_registry.py` | 21 | Imported registry: hash chain, registration policy, holdout-once, store concurrency, DSR, the imported records |
| `tests/test_trend_paper.py` | 73 | Paper books and golden window, ledger chain, refusal and lock, the Binance public adapter (allowlist, redirects, failure modes), gap policy and skip lines |
| `tests/test_trend_paper_cli.py` | 21 | `scripts/run_trend_paper.py`: commands, report text, exit codes |
| `tests/test_trend_hook.py` | 26 | Start hook: never blocks or raises, flag parsing in a fresh interpreter, failure isolation |
| `tests/test_trend_alerts.py` | 38 | Exposure-change alerts: dedupe file, burst limit, local toast only, isolation from the ledger |
| `tests/test_trend_kraken_ohlc.py` | 45 | Kraken public OHLC adapter: boundary, transport, envelope and row failures |
| `tests/test_trend_kraken.py` | 37 | Kraken EUR books: same signals, Kraken fills, ledger format, catch-up, skips, locks, hook isolation |
| `tests/test_trend_kraken_cli.py` | 17 | `scripts/run_trend_paper_kraken.py`: commands, report, exit codes |
| `tests/ui_tests/test_trend_reader.py` | 48 | `ui/trend_reader.py`: read-only payloads (empty, populated, refused, skipped days, unavailable), catch-up action, bridge |
| `tests/ui_tests/test_trend_paper_js.py` | 6 | Runs the Node suite `tests/ui_tests/js/test_trend_paper.mjs` (it fails, rather than skips, when Node is missing), the panel's fixed texts, and the wording scan `TrendWordingTestCase` over the panel, script, reader, reports, toast and the operator guide |
| `tests/test_trend_paper_e2e.py` | 28 | End to end with offline fixtures: 27 radar starts over 60 paper days through the real hook, adapters, store and reader; byte-for-byte goldens; socket `connect`, `create_connection` and `getaddrinfo` refused; an audit hook proves every write stays in the run's state dir |

Focused runs:

```powershell
python -B -m unittest discover -s tests -p "test_trend_*.py"
python -B -m unittest tests.ui_tests.test_trend_reader tests.ui_tests.test_trend_paper_js
node --test tests/ui_tests/js/test_trend_paper.mjs
python -B -m unittest tests.test_trend_paper_e2e
```

The end-to-end module took about 22 s on 2026-10-04. Its fixtures and goldens are in `tests/fixtures/trend/e2e/` with a `MANIFEST.json` of every file's bytes and sha256; `.gitattributes` keeps `tests/fixtures/trend/**` free of line-ending conversion.

Replay and goldens (`scripts/replay_trend_paper.py`, standard library and project code only, sockets refused, `config.STATE_DIR` patched to its own state dir):

```powershell
python -B scripts/replay_trend_paper.py                 # fresh temporary state dir, per-start summary
python -B scripts/replay_trend_paper.py --out DIR       # into DIR (new or empty, never the radar state dir; else exit 2)
python -B scripts/replay_trend_paper.py --check         # exit 0 when every candle file, golden and MANIFEST.json matches, 1 on the first mismatch
python -B scripts/replay_trend_paper.py --write-golden  # regenerate tests/fixtures/trend/e2e/ and its MANIFEST.json
```

`--check` took about 9 s. The test never rewrites a golden; only `--write-golden` does, and only under `tests/fixtures/trend/e2e/`.

## Counts

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
