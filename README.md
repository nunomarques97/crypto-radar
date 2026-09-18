# Crypto Radar

A Windows desktop and Python market-analysis system using public Kraken spot/perpetual data, deterministic L0–L3 analysis, SQLite, and local Qwen through Ollama. It does not place orders. The Agent Control Room visualizes the pipeline and controls its process.

**Current implementation is not yet the local-only target.** The `full`, `bridge`, and `loop` modes still invoke the optional Claude Bridge. The default command without a mode runs the older v0.7 implementation. The Test Mode panel currently exposes real mock-alert persistence and notification actions; see the audit before using them.

## Documentation and status

The September 14, 2026 takeover audit established 411 tests: 410 passing and one pre-existing environmental failure. T001–T003, T010–T013, T020 and T021 have since been PO-accepted; the latest accepted suite result is 458 passing tests, including both Node wrappers, with no skips. T004 (Forja run R-20260918-eb85, accepted in-run by Reviewer APPROVE — T004a and T004b) raises the suite to 472 passing tests with no skips and points the quality gate at the real package layout; T004b registers the resulting legacy findings as follow-up tasks T005–T009 in `docs/tasks/TASK_CATALOG.md`, plus T024–T026 (T021 follow-ups and the pre-existing flaky `test_agent_room.mjs` timing tests, see `TESTING.md`). T022a (pure OC-1 integrity rules in `radar_v08/domain/integrity.py`, accepted in-run by Reviewer APPROVE) raises the suite to 541 passing tests with no skips. T022b (public Kraken adapters expose UTC receipt and source time in `radar_v08/adapters/kraken_timestamps.py`) is «Aceite no run Forja R-20260918-eb85 (Reviewer APPROVE)» and raises the suite to 574 passing tests with no skips. T023a (exact public HTTP allowlist, redirects never followed, `radar_v08/http_client.py`/`security.py`) is «Aceite no run Forja R-20260918-eb85 (Reviewer APPROVE)» (effective once the Reviewer APPROVE and Security Reviewer SECURITY-APPROVE are recorded) and raises the suite to 599 passing tests with no skips. T023b (OC-1 integrity validator wired before L1/L2/L3/router/Qwen consumption, futures rejected one by one, real taker-buy only when valid) is «Aceite no run Forja R-20260918-eb85 (Reviewer APPROVE)» (effective once the Reviewer APPROVE and Security Reviewer SECURITY-APPROVE are recorded) and raises the suite to 627 passing tests with no skips. T024 (follow-up T021c/D8c, tests only: `resample_bars` hour-alignment/60-minute-span tests, `radar_v08/structure.py` unchanged — no alignment violation found) is «Aceite no run Forja R-20260918-eb85 (Reviewer APPROVE)» (effective once the Reviewer APPROVE and the Lead's on-disk verification are recorded) and raises the suite to 630 passing tests with no skips; the QA that closes R2 (D7) comes next, then T030. T030a (versioned, hash-bound evidence identities in `radar_v08/domain/evidence.py`, pure domain, not yet wired) is «Aceite no run Forja R-20260918-eb85 (Reviewer APPROVE)» (effective once the Reviewer APPROVE and the Lead's on-disk verification are recorded) and raises the suite to 686 passing tests with no skips; T030b (SQLite schema-version ledger, evidence store adapter, additive migration, context builder rejects mismatched evidence) is «Aceite no run Forja R-20260918-eb85 (Reviewer APPROVE)» (effective once the Reviewer APPROVE, the Security Reviewer SECURITY-APPROVE required by D7 and the Lead's on-disk verification are recorded) and raises the suite to 722 passing tests with no skips. T031a (OC-1 invocation identity and atomic claim + budget reservation in SQLite, additive ledger migration v3, Forja run R-20260918-f9fe) is «Aceite no run Forja R-20260918-f9fe (Reviewer APPROVE)» (effective once the Reviewer APPROVE, the Security Reviewer SECURITY-APPROVE required by D20 and the Lead's on-disk verification are recorded) and raises the suite to 779 passing tests with no skips. T031b (heartbeat records demand only, the bridge charges one budget unit per genuine call through that claim, cooldown only after the accepted claim, fenced stale recovery; closes T031) is «Aceite no run Forja R-20260918-f9fe (Reviewer APPROVE)» (effective once the Reviewer APPROVE, the Security Reviewer SECURITY-APPROVE required by D20 and the Lead's on-disk verification are recorded) and raises the suite to 793 passing tests with no skips; T032 is next. See `TESTING.md` and `docs/tasks/results/` for task-level evidence and limitations.

| Document | Responsibility |
|---|---|
| [Architecture](ARCHITECTURE.md) | Verified current system and accepted target boundaries |
| [Roadmap](ROADMAP.md) | Delivery gates, scope, acceptance, rollback |
| [Risk](RISK.md) | Deterministic authority and prohibited capabilities |
| [Control Room design](DESIGN.md) | Visual contract, real activity, Test Mode |
| [Testing](TESTING.md) | Baseline, isolation, verification |
| [Development and Codex](DEVELOPMENT.md) | Environment, setup, handoffs, review, recovery |
| [Takeover audit](docs/TAKEOVER_AUDIT.md) | Evidence, discrepancies, risks and scope of inspection |
| [Sextant reuse matrix](docs/SEXTANT_REUSE.md) | Component-by-component reuse decisions |

The older `docs/RADAR_v0.8_ARCHITECTURE.md` remains historical design context. Its phase numbers and cloud direction do not override these documents.

## Design status and continuation

Read PO handover first when taking ownership. Status vocabulary: **IMPLEMENTED** means source plus recorded acceptance evidence; **ACCEPTED DESIGN** is specified but not implemented; **EXPERIMENTAL** has a fixed benchmark/decision rule and stays disabled until it passes; **DEFERRED / AUTHORIZATION-GATED** is designed but unavailable.

The analysis program remains R0–R7. [Operating contracts](docs/OPERATING_CONTRACTS.md) close queue/context/model/evaluation procedures. [Future execution architecture](docs/EXECUTION_ARCHITECTURE.md) and [future trading roadmap](docs/FUTURE_TRADING_ROADMAP.md) define a separate permission-gated end state. Documenting private/live execution does not enable it. Task catalog is the small-task sequence.

## Run from source

Use Python 3.12 and the dependencies in `DEVELOPMENT.md`. From PowerShell:

```powershell
Set-Location <repo>
python -m ui
```

Opening the UI opens its configured SQLite store and may initialize its schema; it is not a read-only database viewer. Its Start button runs `python radar.py --mode loop`. Until cloud containment is implemented, clear both `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` in the launching process if using the application. This is an operational workaround, not the final architectural gate. No credentials are needed for market data.

Explicit CLI modes:

| Command | Actual behavior |
|---|---|
| `python radar.py --mode heartbeat` | Public data, snapshots, L1/L2, persistence; no full L3/Qwen/bridge dispatch |
| `python radar.py --mode full` | L0–L3, Qwen, router/events, legacy bridge and notification path |
| `python radar.py --mode loop` | Repeated heartbeat/full cycles and legacy bridge draining |
| `python radar.py --mode alerts` | Event history; interactive prompt-copy option |
| `python radar.py --mode prompt --event ID` | Reconstruct and copy a saved prompt |
| `python radar.py --mode shadow` | Comparison with legacy v0.7; not an isolated test mode |

`bridge`, `mock-alert`, and `notify-test` are legacy operational tools with effects; they are not the automated test command. `python radar.py` and even an unrecognized flag such as `--help` can fall back to v0.7 because argument parsing is handwritten. Always give an explicit supported mode.

Configuration is currently environment-based and evaluated at import time in `radar_v08/config.py`. `RADAR_STATE_DIR` sets the state root; individual SQLite/output/log path overrides take precedence. Do not change a live database during development. Ollama defaults to `http://localhost:11434` and `qwen3:14b`. ntfy is optional and involves external network delivery; local inference does not mean the whole application is offline.

## Windows package

The existing `CryptoRadarControlRoom.spec` builds an onedir PyInstaller application with local web assets. It expects the source project and a real Python interpreter nearby; it is not a standalone packaged backend. `RADAR_PYTHON_EXE` can point to the intended interpreter. See `DEVELOPMENT.md` before rebuilding. Preserve the duplicate-window regression safeguard.
