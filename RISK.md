# Risk and execution boundary

Status: accepted product constraints; discrepancies in the current legacy schema are listed below. No real execution phase is authorized.

## Authority

LLMs may classify, challenge, summarize, rank, explain, and recommend one of a finite set of analysis paths. They receive immutable evidence; their output is untrusted advisory data. Confidence is not probability until calibrated against outcomes.

Only deterministic code may calculate or enforce position size, maximum leverage/exposure, risk budgets, liquidation thresholds, stop-distance risk, portfolio constraints, and account protection. A model's wording can never authorize those values or override a veto. Future human approval must bind a concrete proposal and evidence version; approval for analysis is not approval to trade.

For the present product there is **no account model, position-sizing service, execution engine, or trading authorization**. Do not construct them merely because the roadmap mentions risk. Start with a deterministic analysis-admissibility verdict (allow analysis / insufficient evidence / reject / stale), independent of a model recommendation. It must not be named an execution approval.

## Hard boundaries

- Public read-only Kraken endpoints only; no private API keys, transfers, order creation/cancellation, or leverage changes.
- No new cloud LLM calls, paid API dependency, or silent fallback. Codex used to develop the software is separate from the software's runtime inference.
- An unavailable quote, fee, depth, FX conversion, or account value remains unavailable. A partial cost total cannot be reported as a full net edge.
- Exact Decimal parsing belongs at new monetary boundaries; currency, instrument, contract type, units, and rounding policy travel with values. Existing float indicators may remain for statistical/ranking calculations. Do not cosmetically convert already-rounded SQLite REAL history into claimed exact money.
- Freshness gates run before expensive inference and before publication. A late result cannot be published as current because the model completed successfully.
- Free text, market symbols, and model content never become commands, SQL, tool names, or control-flow transitions without a typed, allowlisted deterministic interpreter.

## Current defects that must not propagate

`analysis_schema.py` currently permits `leverage`, `margin`, `notional`, `max_loss`, `stop`, and `net_rr` from Claude. These are legacy advisory outputs, not validated risk calculations. Retain historical rows as historical records; the future local analyst schema must not carry executable risk authority. Do not reuse this schema unchanged.

`tradeability.build_cost_preview()` labels fees UNCALIBRATED but adds missing spread/slippage as zero to the subtotal. It uses the maximum estimated buy/sell slippage, not a correctly specified two-leg cashflow calculation. Market selection currently favors futures when present; that does not establish execution suitability. No expected-profit claim is justified by this preview.

Sextant's `engine/risk` is a placeholder. Its risk verdict types and preflight tests are useful references, not a production risk engine or evidence that real account protection exists.

## Future risk gate, if separately authorized

Require explicit account/portfolio source, reconciliation timestamp, known equity and liabilities, instrument-specific margin rules, currency conversion provenance, deterministic sizing and exposure tests, and human approval. Unknown or stale account state fails closed. Until then use only synthetic/manual analysis scenarios clearly labeled as such; never invent account capital.

## Future design, explicitly separated from authority to execute

The user subsequently requested a complete trading end-state design. It is defined in `docs/EXECUTION_ARCHITECTURE.md`, with F0–F7 gates in `docs/FUTURE_TRADING_ROADMAP.md`. This closes the architecture; it does not change the current absence of account/private/order capabilities or authorize implementing/activating them now. The first possible live scope is cash-funded spot-long only under a concrete approved envelope and proven native protection.

Risk sizing consumes deterministic strategy stop/exit rules, exact account state/reservations, cost/FX/liquidity and instrument constraints. Advisory direction may influence a declared strategy only through the Trade Decision Engine policy. Missing mandatory data or an unresolved material objection can prevent entry; consensus cannot waive a veto. Stops and other hard protection continue without models, UI or Ollama. Native protection is mandatory but cannot guarantee a fill or loss bound during venue outage/gaps.
