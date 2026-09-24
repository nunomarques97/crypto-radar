# Code audit — 2026-09-14

Primary: this repository. Reference: [Sextant](https://github.com/nunomarques97/sextant). The findings below come from the code itself, not from earlier documentation. Runtime source was not modified during the audit.

## Current primary implementation

Substantial functioning pipeline: public Kraken spot/perpetual ingestion, universe normalization, SQLite snapshots, MAD-based anomalies, incremental OHLC/cache, L2 ATR/structure/setups/opportunity, L3 depth/trades/tradeability/cost preview, local Qwen, deterministic routing, event persistence, legacy Claude Bridge, Windows/ntfy/prompt notifications and PyWebView Control Room. There is no true multi-agent backend; Red Team is NOT_CONFIGURED and real communication collection returns an empty list.

The project had no Git repository, dependency manifest, README or authoritative current architecture/roadmap at the time. `radar.py` is still a complete v0.7 implementation plus explicit-mode dispatch to v0.8. The original v0.8 document calls itself a proposal without code. Build/dist executables exist, but their correspondence to source and reproducibility were not proved.

Inventory: 108 source/test/design files before changes. Actual SQLite schema read through a read-only connection: 14 application tables plus SQLite's sequence table. No production rows were used as fixtures or edited.

Baseline before changes: **411 unittest tests in 33.428s; 410 passed, 1 failed, no skips reported.** Failure is the known pre-existing environmental missing-SDK health assumption, not an instruction to configure a cloud key. Node tests are wrapped by two unittest cases. Frozen-launch safeguard passed in a fresh source subprocess; no frozen executable smoke test was run.

The original regression fixture still has unisolated desktop notification/clipboard helpers. The baseline was redirected to temporary state with ntfy topic/API key cleared, but is not claimed fully hermetic. The isolated test runner (`scripts/run_tests.py`) later fixed the test boundary. No live radar/model cycle, UI launch, notification diagnostic or executable build was used as an audit step.

## Current Sextant implementation

Commit `47cf962`. Domain/ports/engine/adapters/app separation, uv lock, strict typing/lint, executable import constraints, public archive acquisition, cost/provenance models and walk-forward backtest accounting exist. `engine/risk/__init__.py` is a placeholder; a real general Risk Engine and local LLM runtime do not exist. Exchange book base method is unimplemented and private permission probe returns UNKNOWN. No trading adapter was inferred from type names.

**247 selected component tests passed in 19.34s** in Sextant's own environment. Money/time, capability, LLM authority, costs, backtest correctness, market/HTTP, trial registry, preflight, carry accounting and parameter perturbation were exercised. Full suite/coverage/static checks and research economic results were not rerun. [Reuse matrix](SEXTANT_REUSE.md) records dependencies and evidence per component; nothing was blindly migrated.

## Prioritized findings

| Priority | Finding and evidence | Required response |
|---|---|---|
| P1 | `cli.py` full/loop/bridge can call `claude_bridge._default_create` using inherited credentials | Hard local-only dispatch containment; historical analyses remain readable |
| P1 | `ui/web/test_mode.js` only controls visibility; `ui/bridge.Api.run_mock_alert` uses production store/notifier | Separate visual Test Mode from operational diagnostics and guard the backend |
| P1 | `heartbeat.py` stores all pairs; `anomaly.py` and `store.asset_history` use asset-only lookup | Pair-specific L1 history: prevent USD/EUR/USDT contamination |
| P1 | Heartbeat consumes budget/cooldown before dedup; bridge consumes budget again before claim | Atomic call reservation separate from demand/cooldown, two-connection race tests |
| P1 | Events have no evidence version/deadline; dedup preserves old context and bridge retries later | Immutable evidence, expiry/ABORT_STALE, fenced worker recovery |
| P1 | Spot ingest time is local, stale-futures test is aggregate, L3 lacks source freshness | Per-source/instrument integrity and explicit unknown timestamp semantics |
| P1 | `analysis_schema.py` accepts leverage/margin/notional/max_loss/stop/net_rr from model | Do not reuse as future risk/decision authority; closed advisory schemas |
| P1 | `tradeability.build_cost_preview` uses missing as zero subtotal and max buy/sell slippage; L3 prefers futures presence | Define both-leg cashflows, missing-cost state, provenance and suitability |
| P2 | `l2_features.py` can label incomplete windows as full horizon and use 5m ATR fallback for other horizons | Coverage/versioned feature correction with golden examples |
| P2 | `anomaly.py` relative-BTC metric uses a mismatched historical distribution | Match reference distribution to computed feature, preserve unknown history |
| P2 | `microstructure.py` computes taker-buy ratio but heartbeat passes None to router | Wire actual evidence through a tested contract, not an assumed confirmation |
| P2 | Dedup find/insert has no unique active constraint; budgets are process-local; JSONL separate from DB | Constraints, explicit transactions, outbox; no exactly-once delivery claim |
| P2 | Processing recovery has no generation fence; late analysis can outlive a lease | Fenced ownership and final deadline validation |
| P2 | Forward labels cover raw spot 15m/1h/4h only | Add 24h/venue/direction/policy/model/arm/cost linkage and missingness |
| P2 | UI cached state can imply current health; PENDING can display PROCESSING | Real timestamps/invocations, queued/loading/running distinction |
| P2 | Adopted PID has no Popen handle; stop can clear lock without stopping it; log queues unbounded | Separate process-lifecycle task, bounded buffering and ownership/restart tests |
| P2 | `radar.py:_parse_mode` defaults to v0.7 even for unrelated flags | Explicit CLI usage/error handling; preserve only deliberate legacy mode |

GET/private-path checks are useful but not a full host/path/redirect allowlist. Prompt/context construction references external trading journals that are not authoritative project inputs. UI popup suppression uses `setdefault`, so an inherited `1` can bypass its default. Preserve the fix and cover explicit overrides before tightening it. Current Python functions catch broad exceptions/fall back to empty context in places; UNKNOWN must become a typed result, not silently plausible data.

## Hardware and model facts

Verified i5-14400F, 10 cores/16 logical processors; 34,130,698,240 bytes (~31.8 GiB) system RAM; RTX 5060 Ti with 16,311 MiB VRAM. Installed model artifacts: Qwen3 14B (~9.3GB), gpt-oss 20B (~13GB), Qwen3 coder 30B variants (~18GB), llama3.2 (~2GB). No residency, inference speed, model quality or superiority was inferred from file size. R5 supplies the actual benchmark.

Historical “600+ assets / 250+ perpetuals” was not reproduced by a fresh scan. Neither the radar nor this audit establishes a profitable strategy. Negative Sextant research reports are useful methodological history, not validated results for radar.

## Documentation and design outcome

README/ARCHITECTURE/ROADMAP/RISK/TESTING/DEVELOPMENT are the consolidated authority; DESIGN governs truthful UI mapping; the old v0.8 proposal is historical. OPERATING_CONTRACTS fixes evidence, context, queue, benchmark and drift procedures. EXECUTION_ARCHITECTURE designs future trading without enabling it. FUTURE_TRADING_ROADMAP turns design into gated small work.

Direct dependency pins recorded the working global versions at the time, not a validated fresh transitive lock.

## Verification limits

Source families, tests, schema, configuration, build spec, docs and relevant legacy components were inspected; this is not exhaustive branch-level verification. Secrets, local tool settings, binary internals and large production/research datasets were not used as instructions. Clean installation/build, native UI/frozen lifecycle, actual model benchmarks and all future trading capability proofs remain explicit gates. Accepted future design must never be described as implemented.
