# Engineering roadmap

Owner: PO/architect. Baseline: 2026-09-14. This replaces inherited phase numbering; previous phase labels describe history only. No calendar estimates are credible before the foundation tasks are reviewed. The target is autonomous **analysis**, not execution.

Every phase is delivered through small briefs, not one prompt. A phase completes only when its acceptance criteria and tests are evidenced. T001–T003, T010–T013, T020 and T021 are PO-accepted and locally committed; T022 is the next eligible task. In Forja run R-20260918-eb85, T004 (quality gate on the real layout, versioned baseline, legacy findings registered as follow-up tasks T005–T009 in `docs/tasks/TASK_CATALOG.md`) is accepted in-run (Reviewer APPROVE) in full; those follow-ups plus the T021 follow-ups (T024, T025) and T026 (pre-existing flaky real-timer tests in `tests/ui_tests/js/test_agent_room.mjs`, tests only) are side-line and do not block T022. T022a (pure OC-1 integrity rules, `radar_v08/domain/integrity.py`, the first critical package under strict mypy) is accepted in-run (Reviewer APPROVE); T022b (public Kraken adapters expose UTC receipt and source time, `radar_v08/adapters/kraken_timestamps.py`, the second critical package) is «Aceite no run Forja R-20260918-eb85 (Reviewer APPROVE)»; T023a (exact public HTTP allowlist and never-followed redirects in `radar_v08/http_client.py`/`security.py`) is «Aceite no run Forja R-20260918-eb85 (Reviewer APPROVE)» (label effective once the Reviewer APPROVE and the Security Reviewer SECURITY-APPROVE, D7, are recorded), see `docs/tasks/results/T023.md`; T023b (integrity validator before L1/L2/L3/router consumption, stale futures rejected independently) is «Aceite no run Forja R-20260918-eb85 (Reviewer APPROVE)» (label effective once the Reviewer APPROVE and the Security Reviewer SECURITY-APPROVE, D7, are recorded), see `docs/tasks/results/T023.md`; the QA that closes R2 (D7) comes next, then T030 is the next eligible task. Accepted design remains distinct from implemented status.

## R0 — Ownership and reproducible development

- **Status:** complete for the accepted foundation scope: reproducible Windows/Python 3.12 dependency verification and the critical quality gate were accepted in T002/T003. T004 (Forja run R-20260918-eb85) pointed the gate at the real package layout instead of nonexistent directories, added a versioned baseline for the legacy findings that surfaced (`scripts/quality_baseline.json`), and registered those findings as follow-up tasks T005–T009 grouped by module in `docs/tasks/TASK_CATALOG.md`. The audited Git baseline and subsequent accepted task commits exist locally. The original historical baseline remains preserved in `TESTING.md`.
- **Goal / why now:** establish trustworthy inputs to development and recoverable changes before modifying product behavior.
- **Dependencies:** inspected source and baseline; no runtime dependency.
- **Scope:** source recovery archive/inventory, authoritative documents, Codex instructions, isolated test command, deterministic optional-SDK tests; observed direct dependency manifests, then a verified lock/clean environment and user-controlled local Git baseline.
- **Out of scope:** model calls, runtime features, database migration, dependency upgrade, cloud removal, automatic commits/pushes.
- **Deliverables:** these documents; T001 test changes/runner; follow-up packaging brief with separate runtime/UI/build dependencies and clean-environment verification.
- **Acceptance:** original 411-test result retained; all deterministic health branches covered without relying on installed SDK/credentials; no external notification/clipboard effects from regression test; full suite exit/count visible; Node suites run, no hidden skips. Clean venv source/UI import and build dependencies are reproducible before R0 closes.
- **Tests:** unittest discovery, Node wrappers, runner isolation tests; install/import/build smoke checks in disposable environments when packaging brief is authorized.
- **Rollback:** restore only changed source files from the takeover archive (before Git) or revert the task commit (after Git). Never restore a live SQLite file from a source archive.

## R1 — Contain current boundary violations

- **Status:** accepted containment for legacy cloud dispatch (T010), Test Mode isolation (T011), explicit CLI selection (T012), and Control Room process ownership (T013). Follow-on product work remains separately gated.
- **Goal / why now:** remove accidental cloud execution and test-state contamination before calling the product local or isolated.
- **Dependencies:** R0 safe test command. Independent narrow tasks may be reviewed separately.
- **Scope:** (a) hard local-only runtime gate at the bridge dispatch boundary, including direct bridge mode and inherited credentials; preserve readable legacy history; (b) Test Mode visual controls separated from operational diagnostics, with backend protection so a hidden button is not the only boundary; (c) make supported entry-mode selection explicit and eliminate accidental v0.7 fallback for invalid flags.
- **Out of scope:** local multi-agent dispatch, deleting analysis history, new trading/account functionality, redesigning the room.
- **Deliverables:** fail-closed cloud-dispatch tests; honest disabled/not-configured UI status; Test Mode implementation that cannot persist or notify; explicit CLI compatibility/deprecation behavior.
- **Acceptance:** fake Anthropic client traps demonstrate zero cloud invocation through every runtime mode under the local-only policy; Test Mode interactions leave production DB rows/output/history unchanged and invoke no model/notifier; visual demo still works; frozen popup safeguard survives. CLI unknown flags fail with usage rather than run legacy code.
- **Tests:** bridge/full/loop integration with fake clients; temporary sentinel DB + file hash assertions; API bypass tests; JS test-mode flows; fresh-process duplicate-window regression.
- **Rollback:** independent commits/flags with safe defaults; rolling back UI enhancement must not re-enable cloud dispatch or test writes. Keep containment fixes if a later feature rolls back.

## R2 — Correct facts and qualify data

- **Status:** T020 pair-pure L1 history and T021 feature semantics are PO-accepted; T022a (pure integrity rules) is accepted in-run in Forja run R-20260918-eb85 (Reviewer APPROVE); T022b (public Kraken adapters expose UTC receipt and source time) is «Aceite no run Forja R-20260918-eb85 (Reviewer APPROVE)»; T023a (exact public HTTP allowlist and never-followed redirects in `radar_v08/http_client.py`/`security.py`) is «Aceite no run Forja R-20260918-eb85 (Reviewer APPROVE)» (label effective once the Reviewer APPROVE and the Security Reviewer SECURITY-APPROVE, D7, are recorded), see `docs/tasks/results/T023.md`; T023b (integrity validator before L1/L2/L3/router consumption, stale futures rejected independently) is «Aceite no run Forja R-20260918-eb85 (Reviewer APPROVE)» (label effective once the Reviewer APPROVE and the Security Reviewer SECURITY-APPROVE, D7, are recorded), see `docs/tasks/results/T023.md`; the QA that closes R2 (D7) comes next, then T030 is the next eligible task. Two T021 findings are registered as separate follow-ups in `docs/tasks/TASK_CATALOG.md` (D8 in `docs/forja/DECISIONS.md`): T024 (the `resample_bars` hour-alignment test, planned, not yet done) and T025 (the `_contiguous_suffix`/`contiguous_tail` duplication, backlog, no refactor this run).
- **Goal / why now:** prevent every analyst from agreeing on incorrect market inputs. Cross-quote L1 history is a concrete first correctness task, not speculative infrastructure.
- **Dependencies:** R0; R1 before any user-run autonomous trial.
- **Scope:** separate brief for pair-specific L1 history and BTC-relative distributions; separate brief for history coverage/ATR horizon semantics; per-instrument source/receive timestamps, clock injection, closed/gap-consistent candles, finite values, book/trade/status/mapping checks; reject stale futures independently. Wire real taker-buy evidence into routing only through a reviewed change.
- **Out of scope:** replacing L0–L3 wholesale, additional exchanges, news/unlock scrapers, model agents, exact-money rewrite of statistics.
- **Deliverables:** deterministic integrity report with explicit reason codes and capability-specific degraded paths; evidence for each corrected feature; fixture-based data contract tests.
- **Acceptance:** inserting EUR/USDT rows cannot change selected-USD pair returns; malformed/missing/NaN/crossed/stale/future-dated inputs cannot appear fully valid; incomplete 24h history is not labeled full 24h; unknown optional metadata remains unknown. No LLM call occurs for failed required integrity checks.
- **Tests:** adversarial mixed-pair fixtures, reset/gap/open-bar cases, injected clock skew, stale-one-of-many futures, OHLC inequalities, insufficient-history and no-network integration tests.
- **Rollback:** additive metadata and versioned calculations; preserve original observations. A corrected feature can be shadow-compared but invalid facts must not be reinstated as valid to regain throughput.

## R3 — Reliable evidence, lifecycle and budgets

- **Goal / why now:** make analysis reproducible, bounded and recoverable before multiple roles exist.
- **Dependencies:** R2 data/identity contracts; R1 local-only dispatch boundary.
- **Scope:** immutable evidence versions; opportunities and invocation records; explicit expiry; atomic dedup/claim/budget reservation; call accounting separated from demand/cooldowns; fenced recovery; transactional lifecycle/outbox; additive UI activity projection.
- **Out of scope:** an LLM orchestrator, semantic-similarity service, remote queue, mandatory multi-agent topology, destructive conversion of historical records.
- **Deliverables:** versioned migrations, deterministic controller with one worker adapter, protocol schemas, model/request IDs, expiry and recovery reason codes, outbox reader.
- **Acceptance:** concurrent workers cannot create duplicate active identity or exceed a reserved budget; failed claim consumes no call allowance; a late lease cannot publish; all stale paths yield ABORT_STALE; crash between transition/export is recoverable; no inferred sender/receiver traffic. Tests explicitly distinguish at-most-once local commit from at-least-once notification delivery.
- **Tests:** two-connection concurrency; crash/restart injection; duplicate delivery; deadline crossing during inference; malformed evidence/citation references; migration of a copied representative database; rollback compatibility.
- **Rollback:** additive versioned tables; disable new worker and keep readable old state. Never roll schemas back by dropping evidence. Old code must ignore new records or refuse incompatible startup explicitly.

## R4 — Cost-correct outcome and ablation foundation

- **Goal / why now:** measure value before paying local inference latency for more agents.
- **Dependencies:** R2 qualified inputs; R3 reproducible identities and lifecycle.
- **Scope:** itemized deterministic cost contract adapted from Sextant; fee/FX/funding provenance; pair/venue/direction-aligned 15m/1h/4h/24h markouts; rejected/aborted cohort tracking; frozen evaluation manifest and experiment ledger.
- **Out of scope:** portfolio trading simulation, performance promises, live account reading, copying Sextant's full backtester/statistical stack.
- **Deliverables:** net/gross/unknown-cost labels; cost sensitivity report; baseline L3 vs current Screener report; versioned cohort/metric definition.
- **Acceptance:** known cashflow fixtures reconcile gross minus both-leg costs to net; unavailable cost never becomes zero or “net”; timestamps honor decision availability; repeated labeling is idempotent; every registered cohort item has an outcome or explicit reason it is missing. Frozen evaluation is reproducible from retained evidence.
- **Tests:** long/short and spot/futures cost fixtures, spread-versus-book-impact convention, FX units, funding coverage gap, horizon tolerance, delisting/missing quote, no future-read, parameter perturbation and deterministic manifest tests.
- **Rollback:** keep raw labels; version new net-label calculation instead of rewriting previous experimental results. Disable erroneous reporting and mark affected experiments invalid while retaining their trial count.

## R5 — Local role profiles and hardware scheduling

- **Goal / why now:** establish practical local latency and reliable schemas before architectural expansion.
- **Dependencies:** R3 worker/deadlines; R4 evaluation baseline.
- **Scope:** provider-neutral role IDs; migrate Screener onto local worker; candidate Deep Analyst/Challenger schemas without risk authority; benchmark already-installed models with user-visible experiment boundaries; serialized model lifecycle/context budgets.
- **Out of scope:** cloud fallback, download-every-model search, assumed simultaneous large-model residency, LLM-directed unbounded tools.
- **Deliverables:** cold/warm p50/p95 load/inference timings, peak VRAM/RAM, bounded context/token profiles, schema/citation failure rates and timeout/abort behavior.
- **Acceptance:** model profile records digest and prompt version; no request exceeds declared budget without timeout/abort; controller rejects risk-owned/extra fields and unknown evidence IDs; runtime continues collecting data while inference is occupied. Candidate promotion requires its predeclared latency/deadline and grounded-output targets; missing hardware capacity is a failed candidate, not a reason to enable cloud.
- **Tests:** fake Ollama timeout/invalid-output/OOM responses plus separately labeled real local benchmarks; cancellation/late result; unloaded-model recovery; malicious free-text tool/risk instructions.
- **Rollback:** retain current Qwen or deterministic L3-only path; disable candidate role through deterministic policy. Unload candidate model without affecting collected evidence.

## R6 — Multi-agent value experiment

- **Goal / why now:** prove incremental benefit; if none appears, keep the simpler pipeline.
- **Dependencies:** R4 labels and R5 profiles. Not authorized as one giant implementation task.
- **Scope:** five arms in DECISIONS E02, ordering and information-visibility experiments; independent Red Team view; an optional bounded routing adviser only after fixed paths establish a baseline.
- **Out of scope:** open-ended conversations, shared unconstrained memory, autonomous execution, selecting a winner after repeatedly changing success metrics.
- **Deliverables:** pre-registration naming primary horizon, practical minimum effect, uncertainty/coverage and latency limits, minimum sample/stopping rule; complete experiment ledger and comparative report.
- **Acceptance:** all arms use the same eligibility/cohort policy or pre-registered randomized assignment; abstentions/failures/ABORT_STALE count in denominators; reported benefits are net of explicit cost scenarios and live delay; grouped time/asset dependence is handled. Promote only if the pre-registered benefit criterion passes; inconclusive or negative results retain the simpler pipeline. Numeric promotion targets are frozen in OC-1 (docs/OPERATING_CONTRACTS.md), not selected from results.
- **Tests:** cohort assignment reproducibility, hidden-confidence leakage tests, same-run evidence constraints, budget/visit limits, chronological leakage and outcome completeness checks.
- **Rollback:** disable experimental arms; keep all manifests and failed/void trials. Existing L3/Screener operation remains independently runnable.

## R7 — A richer, truthful operations floor

- **Goal / why now:** improve operational comprehension once real worker telemetry exists; visual work does not justify fake activity.
- **Dependencies:** R3 real lifecycle/communication events; R5 at least one actual local worker. Backend improvements take priority.
- **Scope:** preserve room geometry/characters; incremental CSS/SVG spatial depth, explicit queued/loading/working/stale states, accessible reduced motion, replay from recorded events in a labeled replay mode. Use the existing HTML/CSS/SVG renderer for R7; T070/T071 define acceptance. If it misses the performance gate, reduce effects or retain the previous rendering. A different rendering engine is outside this roadmap and requires a separate evidenced decision.
- **Out of scope:** replacing PyWebView by preference alone, simulated production conversation, characters moving without corresponding events, trading controls.
- **Deliverables:** prototype against recorded event fixtures, visual acceptance captures at supported window sizes, telemetry-to-pose specification and performance measurements.
- **Acceptance:** every production pulse identifies a real persisted communication; reconnect does not replay old events as current; reduced-motion mode preserves information; Test Mode remains isolated; UI input/poll response p95≤200ms and animation p95 frame time≤33ms pass on the actual machine at 1024×720 and 1440×960 with 12 stations. No renderer change modifies workflow policy.
- **Tests:** Node state/communication/dedup tests, restart/out-of-order replay, visual source/frozen smoke checks, duplicate-window test and API isolation suite.
- **Rollback:** select previous renderer with unchanged event contract. Do not roll back event truth to retain animation.

## Designed future trading extension

The R0–R7 analysis sequence remains intact. The separately designed F0–F7 program is in docs/FUTURE_TRADING_ROADMAP.md, with deterministic decision/risk/execution/position architecture in docs/EXECUTION_ARCHITECTURE.md. It is authorization-gated, not implemented or enabled. Merely completing R7 cannot activate private APIs or financial execution. Every R/F phase is decomposed in docs/tasks/TASK_CATALOG.md. Timing/model/ablation decisions are closed by OC-1 procedures; actual measurements, venue proofs and user permissions remain required evidence rather than architectural blanks.
