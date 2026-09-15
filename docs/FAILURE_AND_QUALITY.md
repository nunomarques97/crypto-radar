# Failure ownership and implementation quality

Status: ACCEPTED DESIGN. Analysis controls are planned for R1–R6; private/order/position controls are future-only and require the gates in FUTURE_TRADING_ROADMAP. This is an engineering failure review, not certification of a trading system.

## Architectural quality contract

New critical modules use Python 3.12+, frozen/explicit domain models, enums and exhaustive typed outcomes. Monetary boundaries reject float/bool/nonfinite input at runtime, carry currency/instrument/contract units, specify rounding direction and use Decimal with a documented precision context. Float arrays remain appropriate for statistics. Persist exact monetary values as canonical decimal strings or proven fixed-scale integers, not SQLite REAL. Do not retroactively imply exactness for legacy values.

New package boundaries, introduced only as needed:

- `radar_v08/domain/`: identities, evidence/decision/cost/order/account/position types and structured errors; no I/O, no UI, no adapter imports.
- `radar_v08/workflow/`: pure policy/state reducers and orchestration services depending on domain and injected ports; no direct HTTP, filesystem, wall-clock reads or LLM SDK.
- `radar_v08/adapters/`: clock, persistence, public/private exchange, local model, notifications; side effects isolated and bounded. Wiring stays in CLI/service startup.
- `radar_v08/execution/`: future deterministic decision/risk/planning/reconciliation/position services over domain and ports; absent/disabled until separately scoped. These are engines/services, not LLM agents.
- Existing `anomaly`, `l2_features`, `structure`, `setups`, `microstructure`, UI and SQLite code remain where they are; integrate by reviewed seams rather than a directory rewrite.

Enforce import contracts for the new boundaries and mypy strict on new critical packages. Add the required development tools in a separate locked-dependency task; do not claim checks exist before they are installed/configured. No blanket `Any`, ignored typing errors, silent exceptions, unchecked dict-based money contracts or mutable global configuration in critical new code. A caught error becomes a typed failure with context and a safe transition, not an empty success. Keep schema version separate from deployment/code version.

Transactions are explicit and short. Never hold a database transaction across network or model inference. Persist intent/reservation before side effects; persist acknowledgments afterward. Every update names expected state/version and must detect concurrent loss of ownership. SQLite busy/locked receives bounded retry (50/100/200 ms); on exhaustion stop new admissions, record a local health fault without claiming the failed write succeeded, and preserve existing protective orders. Public export failure is retryable independently of domain commit.

Test behavior with examples, adversarial fixtures, seeded state-machine/property tests and crash-injection around each side-effect boundary. Important invariants include asset/unit/side identity, net=gross−costs, no negative cash due to rounding, no oversizing, exact partial-fill accumulation, cancel/fill race handling, no expired/replayed order, and impossible cross-layer imports. Parameter-perturbation tests demonstrate that every configured safety limit actually changes behavior. No test uses real state directories, authenticated exchange clients or live notifications.

Critical-change acceptance includes targeted checks, complete existing suite, new strict typing/lint/import checks, reviewer trace from acceptance criterion to test, and documented failure/rollback. Dependency upgrades, schema changes, permissions and execution capability are separate reviewed work. No critical implementation is accepted on test-count growth alone.

## Ownership and recovery matrix

| Failure / detection owner | Safe state | Recovery and proof |
|---|---|---|
| Stale or future-dated market observation — integrity validator | Reject required evidence; remove optional venue | Fresh validated version; age/clock fixture and no-call assertion |
| Wrong symbol/quote/contract/side — domain boundary + adapter | Reject/quarantine; no dispatch/order | Explicit mapping version; mixed BTC/XBT, DOGE/XDG, quote and contract regression fixtures |
| NaN/negative volume/crossed or corrupted book/OHLC — validator | DATA_INVALID, no score/size | New source observation; corrupt fixture cannot be replaced with zero |
| Source timestamp unavailable — integrity profile | UNKNOWN/RECEIPT_ONLY; no claimed exchange freshness | Explicit capability-specific receipt/consistency policy; execution fails if contract unproved |
| REST/WS disagreement or missed sequence — reconciler | No new exposure; keep native protections | Snapshot plus deduplicated deltas to known watermark; repeat to agreement; never “latest packet wins” blindly |
| Exchange error disguised as empty response — public adapter | Typed rejected/unavailable state | Retry only transient error, obey bounds; fixture distinguishes empty-valid and failure |
| All models agree on bad premise — validator/controller | Deterministic veto wins | Models cannot override integrity IDs; shared-premise poison fixtures |
| Malformed model output/unknown evidence IDs — worker boundary | Reject result; one bounded repair if eligible | Otherwise FAILED/ABSTAIN, no production claim; do not parse free-text fallback |
| Unsupported factual claim with valid-looking citation — evaluation/reviewer | Suspend offending profile on material violation | Gold support evaluation and corrected frozen profile; ID existence alone insufficient |
| Agent disagreement — synthesis/decision policy | Explicit disagreement/abstain or configured deterministic-only arm | No majority-vote confidence boost; rules in EXECUTION_ARCHITECTURE |
| Missing fee/FX/funding/exit liquidity — cost engine | COST_INCOMPLETE; no positive net-edge or order | Verified cost profile/coverage; no unknown-to-zero conversion |
| GPU load delay/OOM/model crash/Ollama down — worker supervisor | Optional path disabled/aborted; collector continues | One worker restart after memory release; repeated two failures in 10 minutes disable profile until operator review; no cloud fallback |
| Queue overload/starvation — scheduler | Explicit drop/supersede/ABORT_STALE | Bounded ranking/fair slot policy; no hidden unbounded backlog |
| Clock jump/skew — clock adapter | Pause freshness-sensitive admissions | Synchronization re-established, new snapshot; monotonic budget unaffected |
| DB lock/disk pressure — store/supervisor | Stop new admissions/exposure; preserve protection | Bounded retry, verified storage restoration and reconciliation; never write pretend success only to UI |
| Duplicate event/call — store unique identity + controller | Return existing identity, no double reservation | Two-connection test; late lease fenced |
| Crash after transition before JSONL/notification — outbox | Durable domain state survives; unsent outbox pending | Replay with stable delivery ID; document possible duplicate remote delivery |
| Crash before order submit — execution ledger | Prepared intent remains UNSENT/RECONCILE_REQUIRED based on durable boundary | Query exchange identity before retry; prove no existing order or refuse |
| Timeout/crash after submit but before acknowledgment — execution adapter/reconciler | SUBMISSION_UNKNOWN; reserve worst-case risk; no blind resend | Query client ID, open/closed orders and fills; ambiguous absence is not proof of rejection; unresolved means halt and operator investigation |
| Duplicate order/replayed intent — execution ledger + exchange adapter | Refuse already-issued/expired identity | Persistent unique client ID, request hash and single account executor; venue idempotency never assumed |
| Partial fill/cancel race — order reducer | Account for every fill, protect filled quantity; residual intent remains bounded | Dedup trade IDs, reconcile cumulative fills; cancel acknowledgment cannot erase a fill |
| Rejected order/post-only would cross — planner/reconciler | No assumed fill; release only proven unfilled reservation | Replan only with fresh evidence/risk/cost gate and new linked intent; bounded one replan, no automatic market fallback |
| Protective order absent/rejected/undersized — Position Manager | Protection incident; no new entries; guarded reduce/exit if provably possible | Reconcile and restore mandatory native protection or explicitly reduce exposure; alert; do not wait for an LLM |
| Internet/exchange outage with open position — native protections + supervisor | No new orders; maintain known native orders; alert locally | Reconcile before actions resume; stops cannot guarantee fill during venue outage/gaps |
| Application/UI crash — Windows trading service + native protections | UI loss cannot stop protection; new admission blocked during restart | Service auto-restart, durable ledger, reconciled account/order/position snapshot before ACTIVE |
| Orphan/manual/external position — account reconciler | Quarantine account/instrument; block new risk; keep existing native protection | Adopt only through explicit operator-approved recovery plan with quantity/protection verified; never silently liquidate unrelated holdings |
| Stale account/margin/unknown liability — account/risk engine | No entry/size increase; risk-reducing work requires provable position identity | Current reconciled state and reservations; unknown account is not zero exposure |
| Margin/liquidation danger — deterministic risk/position services | Future derivatives disabled until separate proof; then emergency reduction under profile | Fresh venue rules/account data, protective-order semantics and adversarial contract tests; LLM cannot authorize leverage |
| Daily loss/drawdown/kill switch — risk supervisor | No new exposure; deterministic stop/reduce policy remains active | Explicit reset/promotion policy, reconciled state, cause record; no automatic midnight reset of a serious incident |
| Secret leak/permission change — private adapter/security owner | Disable private connector; block new exposure; retain external protection | Revoke/rotate outside prompts, verify least privilege and reconcile before reactivation |
| Local database loss/tampering — recovery owner | Start in RECOVERY_LOCKED, no new submission | Restore verified backup, reconcile exchange as financial truth; unmatched records stay quarantined |
| Upgrade/config change during active orders — service/controller | Drain new admissions; pin old plans/policies for open positions | Compatibility migration, safe protection continuity and restart rehearsal; no policy reinterpretation in place |

## Additional boundaries often missed

Live execution, if later authorized, runs as a dedicated local Windows service independent of PyWebView and Ollama, with a single executor per account and an OS ownership lock plus durable generation/lease. Preventing duplicate local executors does not prevent an external app/manual trade: require an explicitly designated account/subaccount inventory scope, reconcile external changes and quarantine ambiguity. Never treat all holdings in a person's account as bot-owned.

New-entry kill switch, cancel-entry policy and emergency reduction are separate controls. A kill switch must not accidentally cancel protective orders while leaving risk open. Notification delivery is not a risk-control acknowledgment; lack of ntfy service never changes trading permissions. Account/execution APIs expose no credentials or raw secrets to UI/model contexts. Redact errors at the adapter before persistence/logging.

Exchange-native protective orders are a prerequisite, not a guarantee of maximum loss: gaps, outage, illiquidity and venue failure remain possible. No local architecture can guarantee execution while the exchange is unavailable. The chosen response is limited exposure, native protection, independent supervision and explicit refusal where capability is insufficient—not a claim that AI can manage the outage.
