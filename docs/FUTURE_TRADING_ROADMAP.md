# Future autonomous trading program

Status: **DESIGNED; IMPLEMENTATION AND PRIVATE/LIVE ACTIVATION NOT AUTHORIZED BY THIS DOCUMENT.** This follows, and does not replace, R0–R7 in ROADMAP. The user requested end-state architecture, not live execution. Each future phase needs an explicitly scoped implementation task; authenticated access and every live promotion require separate affirmative user authorization.

Architecture authority: EXECUTION_ARCHITECTURE (decisions, risk, execution and position contracts), OPERATING_CONTRACTS (evidence/measurement limits), FAILURE_AND_QUALITY (failure ownership). Task decomposition is in TASK_CATALOG. No phase is labeled IMPLEMENTED until its code and verification exist.

## Fixed progression and permissions

`ANALYSIS_ONLY → RETROSPECTIVE → PAPER → SHADOW_LIVE → MICRO_LIVE → CONSTRAINED_LIVE → APPROVED_ENVELOPE`.

These are explicit modes, not an inferred property of which credentials happen to be present. ANALYSIS_ONLY/RETROSPECTIVE/PAPER cannot construct an order-capable private adapter. SHADOW_LIVE may use specifically authorized account reads but its composition root has no submission port. Live modes require all of: explicit live mode, local enable control, unexpired sponsor approval bound to account/strategy/code/policy/capability/limits, reconciled state, current cost/data gates, and passing risk/admission checks. Presence of credentials alone enables nothing.

Default first live capability is **cash-funded spot LONG only**, with mandatory proven native protection. Short, margin, leveraged and perpetual execution remain disabled, even though public futures analysis exists. Unknown venue capability, account eligibility, fees, minimums or protection behavior fails the promotion gate. No generic “normal unrestricted mode” exists: APPROVED_ENVELOPE is still bounded and explicitly approved.

## F0 — Deterministic trading decision and risk contracts

- **Goal:** turn an advisory opportunity into an auditable TRADE_LONG / NO_TRADE / WAIT / ABSTAIN decision, distinct from risk permission and an order.
- **Why now:** synthesis, abstention and direction must be unambiguous before portfolio/order code exists.
- **Dependencies:** R2 valid facts, R3 identities/controller, R4 calibrated cost/outcome evidence; completed future specification.
- **Scope:** typed decision/advisory/disagreement records; deterministic strategy-to-direction rules; empirical edge gate; separate account/risk/cost verdict inputs; configurable versioned paper policy within fixed hard maxima; forbidden-field/authority tests.
- **Out of scope:** credentials, private reads, order submission, calling every deterministic service an agent, authorizing shorts from a model.
- **Deliverables:** pure Trade Decision Engine and Risk Engine ports/types; exact accepted/rejected/wait reason codes; replay fixtures linking evidence to decisions; schema/typing/import gates.
- **Acceptance:** same input/version always gives same decision; models cannot add allowed actions or risk fields; deterministic veto always wins; missing calibration/cost/account evidence never returns permission; agent disagreement follows the fixed policy rather than majority vote; no output is an exchange command.
- **Tests:** ≥200 distinct adversarial decision fixtures, seeded combinations across every veto/unknown/contradiction, replay identity, enum/schema and unit checks; mutation/perturbation of every risk setting changes the expected permission/size where applicable.
- **Rollback/failure:** disable decision emission and continue ANALYSIS_ONLY; preserve rejected decisions and versions. No exposure exists.

## F1 — Account/portfolio ledger and deterministic risk simulation

- **Goal:** prove reservation, position, cash, fee and risk arithmetic without connecting an account.
- **Why now:** submitted exposure cannot be bounded if open orders/partial fills/reservations are absent from risk state.
- **Dependencies:** F0, itemized Cost Engine, strict monetary/identity domain.
- **Scope:** synthetic account snapshots, bot-owned inventory scope, balances/liabilities/open orders/positions/reservations; atomic portfolio risk evaluation; spot cash/lot/minimum sizing; simulated daily-loss/drawdown/correlation limits and kill switches.
- **Out of scope:** authentic account values, private API, derivative margin model, production profit claims.
- **Deliverables:** pure risk/sizing service, exact ledger, concurrency-safe reservation persistence, explicit stale/unknown state, risk event audit.
- **Acceptance:** aggregate committed+pending exposure never exceeds policy; quantity rounds down to valid lot; insufficient min-notional means NO_TRADE; cost/FX/fees included in cash reservation; sell cannot exceed bot-owned available inventory; no stale account approval; independent simultaneous proposals cannot overspend.
- **Tests:** exact cashflow/gross-cost-net identities, two-writer reservations, rounding/NaN/float rejection, partial fill/cancel state, counterparty/manual holdings separation, daily-loss/drawdown and correlation-boundary fixtures.
- **Rollback/failure:** PAPER disabled on invariant failure; ledger restored/replayed from synthetic event fixtures only; no private side effect.

## F2 — Paper order engine and Position Manager

- **Goal:** exercise the complete lifecycle and hard protection under realistic failures with synthetic fills.
- **Why now:** a profitable retrospective signal does not establish executable fill/protection behavior.
- **Dependencies:** F1; analysis evaluation must pass before claims of useful trading performance, though failure-mechanics tests can run independently.
- **Scope:** deterministic Order Planner, order lifecycle/reconciler and Position Manager over a simulated adapter; maker non-fill/partial-fill/adverse-selection scenarios, both-leg costs, gap/stale/outage/restart/cancel races; native-protection capability modeled explicitly as a capability requirement.
- **Out of scope:** optimistic touch=fill assumptions, private APIs, automatic paper-to-live switch, using an LLM as a stop monitor.
- **Deliverables:** paper exchange simulator, event ledger and replay, protective-order accounting, incident controls and adversarial integration suite.
- **Acceptance:** 10,000 seeded order/position event sequences preserve invariants; every injected crash point recovers to a documented state; unknown submission never blindly resends; partial fills are protected/accounted; failed maker fill can produce NO_TRADE rather than automatic taker. Zero unresolved protection/duplicate-order defects. Prospective paper run ≥60 calendar days and ≥200 completed independent trade episodes before advancement, with positive pre-registered net-edge gate and realistic stress costs.
- **Tests:** crash before/after durable intent, submission, ack and fill; duplicated/reordered messages; cancel/fill races; stale intents; DB locks/disk pressure; model/UI/collector crash while positions exist; kill switch does not remove protection.
- **Rollback/failure:** freeze paper entries, replay affected cases and retain void experiments. Restart at PAPER only after repaired invariants and a new version; no real money exists.

## F3 — Authorized private-read reconciliation and shadow operation

- **Goal:** prove account/instrument/fee/capability mapping and full would-submit plans against current exchange state with no submission capability.
- **Why now:** private-state semantics cannot be certified from public fixtures or a backtest.
- **Dependencies:** F2; **new user authorization for private read-only integration and credentials setup outside prompts**.
- **Scope:** least-privilege Kraken account-read adapter, fee/capability discovery, authoritative REST snapshot plus WS reconciliation, account inventory ownership declaration, stale-state rejection, shadow Order Planner and Position Manager observations.
- **Out of scope:** order/cancel/transfer endpoints, handling secrets in LLM contexts, test orders, adopting unrelated holdings automatically.
- **Deliverables:** current primary-source API contract dossier, sanitized recorded fixtures, account reconciliation journal, capability proof including native stop/partial-fill/OCO-or-equivalent protection and fee tier, shadow discrepancies report.
- **Acceptance:** SHADOW_LIVE cannot construct/call submission/cancel methods; ≥30 calendar days and ≥200 would-submit intents reconcile without unexplained identity/quantity/fee mismatches; at least 20 forced disconnect/restart cycles recover; every observed external/manual inventory change blocks conflicting bot intent; source/receipt/account age and clock limits pass. Any unsupported mandatory native-protection capability means no live promotion.
- **Tests:** private responses are supplied via sanitized fixtures for CI; local read-only shadow run separately authorized; REST/WS sequence gaps, fee tier change, rejected credentials, stale balances and external-position fixtures. No order-capable credential is needed here.
- **Rollback/failure:** revoke/disable private connector, return to PAPER/ANALYSIS_ONLY; never erase observed discrepancies. Credentials remain outside repository and model context.

## F4 — Execution adapter proof and live readiness

- **Goal:** establish exact exchange behavior and failure recovery before allowing capital at risk.
- **Why now:** client IDs, timeout/duplicate behavior, native protection and cancel semantics cannot be assumed from API method names.
- **Dependencies:** F3; separately authorized implementation of order-capable adapter; no real submission until F5 approval.
- **Scope:** narrow allowlisted private submit/query/cancel adapter; request signing/security isolation; client-order identity and durable submission ledger; verified protection/minimums/tick/lot/fee contracts; REST/WS lifecycle; Windows service deployment/restart and operator runbooks. Use a venue-provided test environment only if officially verified available for the selected product; otherwise deterministic fakes cannot be misreported as live proof.
- **Out of scope:** funding transfers, credential withdrawal permission, derivatives, unbounded retry, hidden test orders on mainnet.
- **Deliverables:** adapter contract tests, capability proof matrix (PROVED / UNSUPPORTED / UNKNOWN), service/recovery/kill-switch drills, signed-off risk envelope and promotion artifact template bound to code/account/policy version.
- **Acceptance:** timeout/ambiguous submission resolves by exchange identity/reconciliation or halts; no blind retry; no unprotected entry path; emergency procedures preserve protective orders; read-only modes cannot load this adapter. Every mainnet-only behavior still unproved is explicitly listed and bounded in a separately approved canary plan; if it cannot be tested within native protection/risk limits, live remains blocked.
- **Tests:** request-signing fixed vectors, redaction, endpoint allowlist, response classification, 10,000 state-machine event sequences, multi-instance lock, service crash/restart, backup/restore, unresolved exchange identity and loss of connectivity.
- **Rollback/failure:** no new exposure; adapter disabled; return to SHADOW_LIVE. An artifact marked UNKNOWN cannot become PROVED because unit tests pass.

## F5 — Explicit micro-live canary

- **Goal:** verify minimal real execution/protection behavior within an explicitly approved tiny envelope, not establish profitability from a handful of trades.
- **Why now:** only after F0–F4 prove mechanics can limited venue-specific unknowns be tested safely enough for user review.
- **Dependencies:** every prior gate; affirmative sponsor approval for this exact account, cash-funded spot-long capability, named strategies/instruments, monetary/risk limits, code/policy hashes, expiry and canary procedure. Approval is not inferred from this document.
- **Scope:** one simultaneous bot-owned position, native protective order for every filled quantity, minimum feasible order bounded by approved cash/loss limits, operator-visible incidents, deterministic service supervision. No autonomous envelope increase.
- **Out of scope:** derivatives/shorts/leverage, retries of unknown submission, adding funds, changing limits, qualifying a strategy from canary returns.
- **Deliverables:** exchange-grounded ledger, reconciled fills/costs/protection, latency/slippage report and incident register.
- **Acceptance:** ≥14 calendar days and ≥20 completed canary episodes (or remain MICRO_LIVE); zero duplicate/unowned/unprotected execution incidents, 100% trade/fee/position reconciliation, zero unexplained position drift, all risk limits honored. Protective-order outages or irreconcilable execution trigger immediate entry halt and the documented reduction policy, not waiting for the next evaluation. These mechanics gates do not waive R4/R6 empirical edge gates.
- **Tests:** all prior offline checks plus explicitly approved small real canary scenarios; never call a real order an automated test without authorization. Reconcile before and after each scenario.
- **Rollback/failure:** halt new entries; preserve native protection; close/reduce only according to approved recovery policy and reconciled ownership; return to shadow after flatness is verified. Approval expires after seven days unless renewed, and immediately on material code/policy/capability change; sufficient elapsed time does not renew it.

## F6 — Constrained and approved-envelope autonomy

- **Goal:** permit repeatable operation inside a fixed deterministic envelope after measured reliability.
- **Why now:** expansion must be evidence-driven and separately approved, not a mode toggle after a successful demo.
- **Dependencies:** F5, preserved positive strategy/calibration gates, explicit renewed sponsor approval for any new envelope.
- **Scope:** CONSTRAINED_LIVE then APPROVED_ENVELOPE; same native protection, ownership, reconciliation and decision/risk chain. Adjust only one dimension per approved promotion (notional, instrument set, strategy or concurrent positions), never all simultaneously. Limits are minima of approved envelope and engine hard caps.
- **Out of scope:** unrestricted trading, automatic leverage changes, model-updated risk limits, API/cloud fallback, silent activation of a newly installed strategy/model.
- **Deliverables:** ≥60-day constrained report with ≥200 reconciled trade episodes, rolling edge/calibration/cost/drift and operational incident results, explicit proposed next envelope and rollback plan.
- **Acceptance:** all mechanical and statistical gates hold, risk budget breaches remain zero, current fee/capability/account data valid, native protection continuity demonstrated, no outstanding severe incident. If sample/edge is insufficient stay constrained or stop. CONSTRAINED_LIVE and APPROVED_ENVELOPE approvals expire after 30 calendar days, earlier on a material hash/capability change; larger limits require another approval.
- **Tests:** existing offline/concurrency/recovery suite and ongoing reconciled observations; daily stop/entry-disable drills without destructive real actions; new capability changes repeat relevant F3–F5 proofs.
- **Rollback/failure:** deterministic entry suspension on drift/risk fault, retain/reduce protected positions according to policy, step back to paper/shadow after reconciliation. No automated escalation based on PnL.

## F7 — Optional derivatives/short capability extension

- **Goal:** allow a separate, proven product capability only if net benefit and protection feasibility justify its complexity.
- **Why now:** never needed for first spot-long operation; analysis of perpetual markets is not authority to execute them.
- **Dependencies:** explicit sponsor scope; mature F6 record; verified account/jurisdiction/product eligibility and current venue rules. This is a separate capability program, not automatic F6 completion work.
- **Scope:** contract multiplier/settlement, inverse/linear distinctions, margin/liquidation/bankruptcy rules, funding settlements, reduce-only/native protection semantics, hedged/netted positions and outage stress; separate risk/cost/adapter profiles.
- **Out of scope:** reusing spot quantity/PnL formulas blindly, automatic leverage manipulation, transfers, ignoring liquidation/funding because LLM analysis is strong.
- **Deliverables:** typed derivative instruments and exact accounting, capability/permission dossier, separately approved risk envelope, repeated retrospective/paper/shadow/canary progression.
- **Acceptance:** every instrument unit/fee/funding/margin/protection rule is evidenced and tested; unknown liquidation distance blocks entry; spot proofs are not substituted for derivative proofs. Same minimum progression periods/counts and zero safety defects apply before bounded autonomy.
- **Tests:** inverse/linear settlement, funding gaps, liquidation/maintenance changes, price gaps, mark/index disagreement, partial hedge fill, reduce-only rejection and position-mode mismatch.
- **Rollback/failure:** disable derivative admission, retain native protection/reconcile and reduce within the approved policy; spot capability need not be disabled unless the account-level fault affects both.

## Gate records

Every promotion record contains mode, account/inventory scope, instrument/strategy list, code/schema/policy/model/capability/fee hashes, evaluation dataset IDs, limits, approver, timestamp, expiry and rollback procedure. A hash mismatch invalidates the record. A developer cannot self-approve real execution. User authorization supplies permission and account-specific input, not unresolved architecture. If the user never approves a gate, the product remains in its last permitted safe mode indefinitely.
