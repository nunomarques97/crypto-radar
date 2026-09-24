# Operating contracts: evidence, scheduling, context and model evaluation

Status: **ACCEPTED DESIGN**, not implemented. Version OC-1, 2026-09-14. These are concrete engineering defaults for future work, not claims of calibrated performance. A change requires a new version and evidence; failing a limit disables the optional path rather than weakening the limit silently. `ARCHITECTURE.md` describes current implementation. Future trading contracts are in `EXECUTION_ARCHITECTURE.md`.

## 1. Collection, validation and deadlines

Keep L0 collection at a 60-second target cadence, L2 on closed 5-minute bars at a 300-second target cadence, and L3 only for shortlisted opportunities. A collector and one local inference worker are separate processes; SQLite is their durable boundary. No network/message broker is introduced. UI is a client, not the owner of either process.

All external operations have connect/read/wall-clock bounds. Public requests pass a shared rate limiter per venue adapter; start with at most two in-flight public requests, a 1-second minimum interval between starts, bounded exponential backoff (1/2/4 seconds, at most three retries), and honor a longer Retry-After only if the collection deadline permits. These conservative client settings are not an assertion of Kraken's published quota. Adapter tests and current official limits may tighten them. Failed collection produces an explicit incomplete run, never manufactured observations.

| Required observation | Analysis readiness policy |
|---|---|
| Spot ticker/status | Received within 90 seconds; exchange time preserved if supplied; malformed/crossed/nonfinite values fail |
| Selected-pair OHLC | Closed 5m bars, last expected close within 6 minutes; continuous bars for each claimed horizon; otherwise that feature is unavailable |
| ATR | At least 15 valid closed bars for period 14; a 1h ATR needs its own complete resampling coverage, never a relabeled 5m fallback |
| Book | Receipt age ≤15 seconds at evidence seal; valid side ordering, positive prices/nonnegative sizes, uncrossed, required size covered; source age checked when provided |
| Trades | Fetch receipt ≤15 seconds; latest actual trade ≤60 seconds for active-trade claims; no trades is unavailable, not zero buy pressure |
| Futures | Each instrument independently valid; reported trade/quote timestamps ≤60 seconds for claims about current activity; never aggregate “some futures fresh” into all fresh |
| Clock | UTC offset uncertainty ≤500 ms for freshness-sensitive work; backward jumps or unknown synchronization suspend it; monotonic elapsed deadlines remain independent |

Receipt age only bounds when the server response was received. It is explicitly labeled `RECEIPT_ONLY` when source time is absent. Such data can support a receipt-qualified analysis with consistency checks, never a fabricated exchange timestamp. An execution capability profile must prove that its timing and consistency contract is adequate before using it for orders.

Run integrity checks before L1/L2 consumption and before evidence is sealed for inference. A bad optional futures feed removes futures eligibility while leaving independently valid spot analysis possible. A bad selected spot instrument blocks that opportunity. Finite/unit/identity/OHLC violations are hard failures. Missing beta, unlock, listing or funding metadata produces UNKNOWN only for that claim; it cannot silently increase confidence.

| Opportunity evaluation horizon | Absolute analysis deadline from seal | Maximum queued age | Permitted optional roles |
|---|---:|---|
| 15 minutes | 60 seconds | 15 seconds | Screener; Challenger only if predicted complete path fits |
| 1 hour | 120 seconds | 30 seconds | Screener / Challenger / Deep Analyst subject to remaining budget |
| 4 hours or 24h follow-up | 180 seconds | 45 seconds | Same roles; no path guaranteed admission |

The 24h value is primarily an outcome horizon, not permission for 24h-old evidence. Admission uses measured p95 cold/warm load plus remaining nodes and a 10-second finalization reserve. Until profiles are measured, use their hard timeouts as the estimate. If the path cannot fit, select an eligible simpler deterministic path or abstain. Never extend the original deadline after a queue wait, repair or re-observation.

Before publication, fetch a **separate validation receipt** (book ≤5 seconds, ticker ≤15 seconds, relevant status unchanged). It records its own evidence IDs and never edits the original analysis snapshot. Abort if price moved more than 0.25 of the sealed valid 5m ATR, a hard veto changed, required data is missing, source/identity differs, or the deadline expired. Without a usable ATR, price-sensitive publication is not eligible. Publish both analysis-as-of and validation time. The receipt cannot be used to pretend that an old conclusion was derived from new facts.

Material change produces ABORT_STALE and, at most once, a newly collected opportunity version; it never refreshes the old one in place. New versions remain subject to a 30-second per-instrument regeneration cooldown, bounded queue and original-parent retry count. Continue ordinary collection even when an optional model fails.

## 2. Queue, deduplication and backpressure

One running inference and at most 16 queued opportunity versions. Identity is `(venue, market_kind, native_instrument, setup, direction, evidence_hash, policy_version)`, with a unique active identity in SQLite. A changed evidence version is not the same invocation; an identical duplicate returns the existing ID. Exact hashes are the first implementation; approximate semantic merging is rejected for now.

Admission first removes expired or integrity-invalid work, then checks whether a bounded path can finish. Rank by deterministic opportunity score band (high ≥70, eligible 50–69), then earliest deadline, then seal time, then stable ID. No LLM confidence enters scheduling. Every fourth available dispatch slot goes to the oldest admitted lower band that still fits its deadline; unused reserved slots return to higher-band work. This avoids preventable starvation without preserving stale work.

On overflow, compare the new eligible item to the worst queued item using the same ranking. Keep the better one; persist `DROPPED_BACKPRESSURE` for the other. Expired items get ABORT_STALE, not a missing record. At most one queued version per instrument/setup/direction is kept: a genuinely newer compatible version supersedes the older with `SUPERSEDED`, linked both ways. Distinct opposing directions are separate hypotheses and still share per-instrument resource limits.

Do not preempt healthy inference merely for a higher score: load waste and non-deterministic completion are undesirable. Expiry/cancellation can invalidate its result immediately; terminate/restart the worker only after bounded cancellation fails. A result returned after its deadline or lease loss cannot commit. Collection, reconciliation and future position protection never wait for the inference queue.

## 3. Worker and role profiles

All roles use an injected local inference adapter and versioned profile, not a model name embedded in orchestration. No hidden conversational session or tools. Input/output/schema limits are enforced outside the model. Worker concurrency starts at one; one repair call maximum across the **whole opportunity**, only within remaining budget. Total model invocations ≤4 (three roles plus one repair); optional routing adviser is disabled in production OC-1.

| Role | Capability / context | Output cap | Hard call limit | Warm p95 target | Production disposition |
|---|---|---:|---:|---:|---|
| Screener | Structured triage from compact facts; 4,096 context tokens total, input ≤2,800 | 768 tokens | 30s | 20s | Qwen14B is current baseline, not permanent assignment |
| Challenger / Red Team | Counter-hypotheses and evidence gaps; 8,192 context total, input ≤5,600 | 1,536 | 45s | 35s | Disabled until quality/latency and incremental-value gates pass |
| Deep Analyst / Fable | Synthesis, competing scenarios, uncertainty; 8,192 context total, input ≤5,600 | 2,048 | 90s | 60s | Disabled until gates pass; normally infeasible for short horizon |
| Bounded routing adviser | Choose one supplied eligible path enum; 4,096 context, input ≤2,800 | 512 | 15s | 10s | Experimental only; rejected from default workflow |
| Position-analysis adviser | Explain a thesis change from position/evidence summary; 8,192 context, input ≤5,600 | 1,536 | 60s | 45s | Deferred optional experiment; no protection authority |

The hard limit includes loading, prompt processing and generation; no nested inference timeout can exceed the opportunity deadline. Cap whole-opportunity prompt tokens at 15,000 and output tokens at 4,500 across calls; account for repair as another invocation. Input reduction occurs before dispatch according to the context policy; never silently truncate mandatory evidence.

Resource starting envelope on the measured RTX 5060 Ti (~16 GB VRAM), ~31.8 GiB RAM/i5-14400F: keep at least 1.5 GiB VRAM and 4 GiB RAM free for the rest of the system; serial load/unload only. If a candidate/offload profile exceeds the reserve, thrashes, OOMs or misses a deadline, it fails that role. A CPU-offloaded 30B model is allowed to compete; its real wall time and memory, not parameter count, decide. No automatic downloads or cloud fallback.

Initial candidates use already-installed families: `qwen3:14b`, `gpt-oss:20b`, `llama3.2:latest`; the installed `qwen3-coder:30b` family is an optional large/offloaded candidate, not assumed suitable for market reasoning. Screeners/deep roles can share a family if independently evaluated; an independent Challenger must use a different family from the winning Screener. If no diverse model passes, disable Challenger rather than rebrand the same model as independent. Store exact model digest, quantization, context and runtime settings; aliases alone are insufficient provenance.

## 4. Context is selected evidence, not chat memory

Persistent history and inference context are different things. Persist observations, validated feature versions, event/invocation results, policy/fee/model versions, handoffs, outcomes and summary dependencies. Rebuild every role's context from an explicit as-of snapshot. Reset at every opportunity version and every independent role; reuse no provider conversation ID or previous hidden state.

Select historical context by the feature contract:

- Structure/ATR use the necessary complete closed-bar windows (5m ATR14, 4h structure, 24h coverage), plus BTC reference aligned to the same cutoff. Models get computed aggregates and the specific bars supporting claimed extrema/breakouts, not thousands of arbitrary candles.
- Regime summary uses trailing 30 days of eligible closed data with coverage and calculation version. If coverage is insufficient, regime is UNKNOWN; no fabricated long-term context from a short cache.
- Similar-event summary uses only the same version-compatible setup, direction, horizon and liquidity/regime bucket, with outcomes matured **before** current as-of. Use all eligible observations from the trailing 90 days to produce counts, distributions, calibration/coverage and uncertainty. Require at least 50 matured examples to show an empirical summary; otherwise show insufficient sample. The 90-day window is a freshness policy and is tested against 30/180-day alternatives offline, not changed from live results.
- Raw exemplars are not “last five.” Select representative medoids of the eligible cohort by deterministic normalized features plus one adverse-tail example when available, using pre-fixed distance/scaling. Add exemplars until the role's exemplar token allocation is full, with stable tie breaking and no duplicate episode. Aggregate statistics always report the full cohort denominator.

Token allocation within each input budget: mandatory identity/time/integrity/policy 20%, current facts and dependencies 45%, historical aggregates 20%, exemplars 10%, task instructions 5%. Instructions/schema overhead and output reservation count in total context. Tokenize with the target model's actual tokenizer or a conservative measured upper bound; character counts are not accepted as exact token counts. If mandatory fields exceed their allocation, use available exemplar/history space first; if the whole context still cannot hold mandatory evidence, refuse that profile (`CONTEXT_UNFIT`) instead of truncating it. No hidden compression by another unrecorded LLM.

Summaries are deterministic, carry source range/hash/count/missingness, and are versioned. Historical analyst text is excluded from independent role input. A synthesis stage may receive typed prior claims and their evidence IDs, but self-confidence stays separated. Outcome joins use `label_available_at <= decision_as_of`, not merely the event timestamp. Testing includes a future-label poison record, cross-arm/private-memory poison and changed-evidence hash; none may leak into the independent context.

## 5. Retention and replay

Use SQLite for operational state. Retain raw L0/L3 responses and input observation references for 30 days; closed 5m bars and feature/integrity summaries for 400 days; full sealed evidence, invocations, outcomes and experiment manifests for 400 days. Any evidence pinned to an accepted experiment, active position, incident or approval is exempt until explicitly archived. Future order/account/risk audit history is retained for seven years by project policy, subject to a reviewed legal/privacy requirement before private-data activation; seven years is not asserted as a jurisdictional rule.

Prune only finalized/unpinned data with matured 24h labels plus seven days of labeling grace. Store expired/missing label reason before pruning. At 80% of the configured 20 GiB analysis-state quota, archive finalized unpinned evidence with checksum verification; at 90% suspend new optional inference; at 95% stop new opportunity admission and alert. Never delete active-position or unresolved-order evidence to regain space. Backups use SQLite's backup interface or coordinated checkpoint/closed-copy procedures, not copying only a live `.sqlite` file while WAL holds changes. Restore is rehearsed on an isolated destination. Compact append-only audit exports only via verified archival, never by rewriting trial history.

## 6. Model benchmark specification — R5

Prepare a versioned corpus of 300 non-overlapping cases: 100 development and 200 locked holdout. Holdout consists of 40 each: invalid/stale input, conflicting evidence, admissible positive setup, admissible negative/no-edge setup, and insufficient evidence. Do not fabricate a profitable label from an opportunity score. Deterministic validity/arithmetic labels come from fixtures; market-grounding/objection labels require two independent human reviews with disagreements adjudicated and recorded before the holdout is opened. If reliable gold labels are unavailable, model promotion is blocked and deterministic/current baseline remains; the developer does not substitute another LLM as ground truth.

Same cases, visibility view and output schema for all competing models within a role. Use temperature zero, pinned runtime/digest/profile, chronological episode separation and no holdout prompt tuning. Start with at most two viable candidates per role after a 20-case development/resource probe; include the current model when relevant. Profile ten cold loads and twenty warm representative timing runs; run all 200 holdout cases once, then repeat twenty fixed cases three times to quantify stability. Calls are a separately authorized local benchmark, never part of the normal test suite.

Record cold load, warm latency, prompt/generation tokens/sec, p50/p95 end-to-end, peak VRAM/RAM, GPU/CPU offload, schema validity, citation-scope validity, unsupported material claims, abstention errors, timeout, OOM and gold-task performance. Count invalid responses/timeouts in denominators, even if a repair succeeds; also report final accepted-output rate. Record swap cost under the candidate full sequence, not just isolated warm speed.

Promotion gates per role:

1. Zero risk-authority or forbidden-tool acceptance, zero accepted unknown/cross-run evidence IDs, zero OOMs in the benchmark.
2. First-pass schema validity ≥99%; all accepted final outputs schema/scope-valid. Material unsupported factual-claim rate ≤1% observed and its 95% Wilson upper bound ≤2%; any fabricated actionable price/instrument/authority is a hard failure.
3. Abstention/invalid-data rejection recall ≥95% on the appropriate labeled cases; false abstention ≤15% on sufficient-evidence cases. Gold agreement on the role's required classification ≥85%, with confusion matrix. Challenger material-objection precision ≥80%, recall ≥85%; “no material objection” is valid.
4. Role p95 and hard-time/resource limits above pass. Report confidence intervals and warm/cold differences; load time must fit actual opportunity admission.
5. Among passing profiles select the smallest p95 complete-path time within 2 percentage points of best gold score. Prefer lower memory at a timing tie. If the current profile fails, use deterministic analysis until a candidate passes. Gold quality is necessary, not proof of incremental market value: R6 remains required.

## 7. Ablation, confidence and decision gates — R4/R6

Maintain separate fields: model self-confidence (uncalibrated ordinal), deterministic opportunity score (ranking), integrity/coverage (evidence quality), empirical probability (with model/fit period/sample), execution admissibility (boolean/reasons), and portfolio permission (deterministic budget/verdict). No weighted magical confidence combines these dimensions.

Primary analysis metric is **one-hour directional net markout** under an explicit executable-cost scenario. Secondary horizons are 15m/4h/24h. No quote/funding/cost coverage means missing net label, not zero. Define target event `net_markout > 0`; direction/setup are fixed at decision time. Calibrated probability is not used as a substitute for net expected value.

Calibration procedure: per version-compatible strategy/horizon, fit a regularized logistic mapping on deterministic score/features using earliest 60% of eligible matured observations; fit calibration on the next 20%; lock the latest 20% as test. Purge a full maximum label horizon across split boundaries; group same-asset overlapping opportunities into episodes. Minimum 1,000 matured episodes, 200 held-out episodes and 60 calendar days; if absent, probability is `UNCALIBRATED` and no probability-based trading gate can pass. Never calibrate a model's self-score as if it were independent evidence. Refit monthly on trailing up-to-400-day data only after drift checks and repeat holdout validation.

Report Brier score, reliability diagram, sample-weighted calibration error in ten equal-frequency bins (at least 20 held-out samples per bin), discrimination, net-return distribution and coverage by regime/liquidity. Accept empirical probability for decision support only if Brier beats the prevalence baseline and calibration error ≤0.05 on held-out data; otherwise retain score/rank with probability unavailable. Small subgroup bins cannot claim calibrated subgroup probabilities.

Five fixed arms: L3-only; L3+Screener; L3+Screener+Challenger; L3+Screener+Deep; all three roles. First use frozen evidence on the same cohorts to test reasoning; then randomized live assignment by instrument/day block to measure actual delay. Separate independent Challenger-before-Deep and Challenger-after-Deep critique as different experiments; neither silently changes the other's input visibility. Frozen inputs do not measure live capacity or delay.

Primary success rule: the added arm's paired/blocked one-sided 95% lower bound on mean **incremental** one-hour net markout exceeds 5 bps, accepted-analysis coverage falls by no more than 5 percentage points, and stale/timeout rate is ≤5% of admitted work. For testing four additions against baseline, use a familywise 5% Holm correction. Require ≥200 independent instrument/day blocks, ≥1,000 matured opportunities and ≥60 calendar days per comparison; evaluate once at the preregistered endpoint, at most 120 calendar days. If sample remains inadequate or criterion fails, retain simpler baseline. No repeated peeking/prompt changes to rescue an arm. Define bootstrap by calendar-day blocks preserving instruments/arms, 10,000 resamples, fixed seed; report sensitivity to weekly blocks. A single assumption-dependent result does not promote.

Future trade eligibility additionally requires a strategy's one-sided 95% lower bound on mean net edge to exceed `max(5 bps, 25% of expected total round-trip cost)` under the declared scenario, independently of model agreement. This is an engineering research gate, not a prediction of profits or permission to use real money. Missing required calibration/edge evidence means NO_TRADE; live authorization and deterministic risk gates are separate.

## 8. Regime change, drift and disablement

Regime is deterministic: BTC trailing realized volatility percentile (high if ≥80th of prior 180 complete days), trend direction from closed 1h price versus 24h moving average with 1 ATR neutral band, plus selected-instrument liquidity bucket. Missing requisite history yields UNKNOWN. Version these descriptors; do not let an LLM redefine regimes after outcomes.

Monitor daily after labels mature. Alert and suspend *new trading eligibility* for an affected strategy/profile if: rolling 200-episode net-edge lower bound ≤0; held-out/rolling calibration error >0.10 on ≥200 episodes; feature population-stability index >0.25 on a fixed 20-feature reference set with ≥500 observations; or model schema/unsupported-claim hard failure occurs. Two consecutive daily PSI/calibration breaches are needed for statistical drift suspension; data-integrity, money-authority and order-safety violations suspend immediately. A regime change itself re-evaluates eligibility; it is not automatically a trade reversal.

Analysis can continue in labeled shadow mode. Re-enablement requires a new frozen profile, repaired cause, 200 fresh matured episodes over ≥14 days passing the relevant evaluation gates, plus the already-required original minimum dataset. No automatic risk-envelope increase. Active positions remain managed by deterministic protection, independent of disabled LLMs.

## 9. Experimental decisions are closed procedures

No winning model, profitable strategy, agent ordering or calibrated fee is asserted without measurements. The architecture decides **how** each is selected, exact criteria, and failure behavior. A developer implements these procedures; it does not invent a threshold when a benchmark fails. Default disposition: optional role disabled, simpler local analysis, or NO_TRADE. Changes require a documented versioned decision and a new preregistration.

## 10. Exact cohort utility and drift feature definitions

For analysis ablation, eligibility is decided before arm assignment: valid selected-instrument evidence, deterministic setup direction LONG or SHORT, opportunity score≥50, TRADEABLE L3 state, no hard veto and complete declared cost scenario. This is an evaluation cohort, not trading permission. Separately report all screened-but-ineligible observations and reasons. Use a non-overlapping instrument episode per primary one-hour horizon; earliest qualifying evidence seals the episode, later updates link to it without becoming extra independent samples.

Compute every eligible episode's realized market label whether any arm escalates or not. Primary arm utility is net directional markout if that arm issues its final positive advisory decision before deadline, and zero if it rejects/abstains/fails/expires. Costs are charged only to the declared hypothetical acted-on scenario; abstention carries no fictitious transaction cost. Compare paired utility on frozen arms; on randomized live arms estimate assignment-group mean utility with instrument/day blocking. This intention-to-treat definition prevents successful-completion selection bias. Report conditional selected-opportunity return separately, never as the primary causal comparison. If a horizon market quote/cost is missing, mark the common episode label unavailable for all frozen arms, report missingness by live assignment arm, and forbid promotion if missing-label rate exceeds 5% or differs by more than 2 percentage points between randomized arms. Use lower-bound stress sensitivity for missing labels; never impute a favorable zero.

Candidate strategy setup/direction originates in the deterministic strategy profile. An LLM may recommend a different direction, but that is DISAGREEMENT/ABSTAIN for this registered experiment, not a new unregistered strategy. A different direction rule is a separately registered profile. Each profile also fixes action threshold/output enum interpretation before the experiment; the baseline positive decision is eligibility itself. Model self-confidence is recorded but never the action threshold.

The PSI drift vector has exactly 20 versioned canonical features: return1m/5m/15m/1h/4h; BTC-relative return15m/1h; volume intensity5m/1h; spread_bps; tight-band bid/ask depth; book imbalance; taker-buy ratio; trades/hour; observation age; ATR5m/price; ATR1h/price; 24h range/price; and BTC realized-volatility percentile. Compute PSI on reference-training decile boundaries with 0.5 pseudocount per bin and a separate missing bucket. Missing/invalid feature rates rising by 10 percentage points versus reference trigger an integrity investigation even if PSI is unavailable. A feature without a validated implementation is UNKNOWN and the relevant model/strategy profile cannot pretend to have its trained vector. No new feature is chosen because it makes drift disappear.

All windows, sample counts and gates are engineering policy defaults recorded before measurement. They can be too strict and leave a role/strategy disabled; that is the intended safe outcome. A versioned experiment may propose changing them, but a developer may not loosen them to obtain a green result.


## 11. Ordering, visibility and optional routing resolution

The production default remains the accepted simplest arm; no optional agent is enabled merely to obtain an ordering. After an added-role arm passes section 7, test ordering in a new locked experiment with two paths: (A) Screener → blind Challenger → Deep; (B) Screener → Deep → blind Challenger. Both Challenger inputs exclude earlier thesis/confidence; Deep receives current facts and Screener's grounded findings, and in A may receive Challenger objections. The final deterministic synthesis sees the same typed role results. This compares the actual bounded workflow, including the extra information available to Deep in A. A third, separately labeled visibility arm (C) uses B but allows Challenger to see Deep's grounded findings, excluding numeric self-confidence. Do not call C independent analysis. Freeze these views and prompts before assignment.

Compare A and C against B using section 7's 1h intention-to-treat net utility, minimum 1,000 episodes/200 instrument-day blocks/60 days, maximum 120 days, calendar-day bootstrap, coverage and timeout gates. Apply Holm correction to the two comparisons. Adopt a changed path only when its lower confidence bound improves utility by >5 bps; if both pass, choose the larger corrected lower bound, then lower p95 latency, then A as a stable tie-break. Inconclusive retains B for an already-qualified full arm; if the full arm never qualified, retain the earlier simpler arm. This tie-break is policy, not a claim that B is superior.

E04 is a separate optional experiment after a fixed path is qualified. Deterministic routing remains the production default. The adviser receives current qualified evidence, remaining budgets, measured load estimates and an explicit eligible action list: STOP, SCREENER_ONLY, ADD_CHALLENGER, ADD_DEEP, or ADD_BOTH. It returns one enum, grounded reason and evidence IDs, never new actions, tools or revised budgets. It may run once per opportunity and consumes one of the four total invocation slots and existing total token/wall limits; when three analyst roles run, no repair slot remains. Ineligible/invalid/late adviser output falls back to the predeclared fixed path only if still admissible, otherwise ABSTAIN. Never start another adviser or extend the opportunity deadline.

Randomize adviser versus fixed routing by instrument/day; include adviser load and inference in actual end-to-end latency. Use the same section 7 minimum sample/calendar, net utility improvement >5 bps lower-bound, coverage and stale/timeout gates for this single predeclared comparison. Report model calls, tokens, load time and useful completed work per hour. Failure, insufficient sample or no incremental benefit leaves the routing role disabled; cheaper calls alone do not establish better analysis. T060 implements the bounded variants, T061 implements these preregistered comparisons only when their dependencies pass.
