# DESIGN.md — Crypto Radar Control Room

Direction: **Signal Room** — dark, calm, observability-style control room for the Crypto Radar backend. Locked 2026-09-14. Mock: `docs/design/mocks/signal-room.html`.

Mood: calm, precise, trustworthy. Never flashy. A state is a fact, not a celebration.

## Color roles

Single dark theme (no light mode needed — this is a desktop control room, not a web artifact with viewer-controlled theming).

| Role | Value | Use |
|---|---|---|
| `--bg` | `#0B0E13` | App background |
| `--panel` | `#12161D` | Card/panel background, topbar, sidebar |
| `--panel-hi` | `#161B23` | Hover/active surface, nav active state |
| `--border` | `#1E242D` | All hairlines, table rows, card borders |
| `--text` | `#E6E9EE` | Primary text |
| `--muted` | `#8B95A5` | Labels, secondary text |
| `--muted-dim` | `#5A6577` | Tertiary text, footnotes, disabled labels |
| `--ok` | `#34D399` | RUNNING, OK, SENT, done step |
| `--warn` | `#F5B94D` | STARTING/STOPPING, PENDING, AUTH ERROR, deferred |
| `--err` | `#F0616B` | STOPPED-on-error, FAILED, ERROR |
| `--info` | `#5B9DF6` | PROCESSING, current pipeline step, in-flight |
| `--idle` | `#4B5563` | IDLE, NOT CONFIGURED, OFFLINE, DISABLED |

Rule: color always maps to a real backend state (see the app's status-mapping table). Never pick a color to make something look more alive than the data says it is.

## Type

- UI/labels/nav: **Inter** (system-ui fallback stack: `-apple-system, Segoe UI, system-ui, sans-serif`).
- Data — IDs, timestamps, scores, logs, prompts: **JetBrains Mono** (`ui-monospace, Consolas, monospace` fallback).
- Base size 13px, line-height 1.4. Panel headers: 11px, uppercase, letter-spacing .08em, `--muted`, weight 600.

## Spacing scale

4 / 8 / 12 / 16 / 24 / 32 / 48 (px). Panel padding 16px. Gap between panels 16px.

## Radii (max 3)

- 6px — buttons, small controls, badges-as-rect
- 10px — panels/cards
- 999px — pills/status badges

## Motion

One signature moment per screen: a subtle pulse (`box-shadow` breathing, 1.8s ease-in-out) on the status dot of whichever pipeline node/step is **currently active** — never on idle/done/offline nodes. No other animation. No page-transition effects, no hover-lift, no confetti-on-success.

## Signature element

The **agent pipeline rail**: a vertical list of agent nodes, each with a status dot, connector line to the next node, role label, and last-activity/counts line. Same visual language reused for the per-alert **lifecycle stepper** (Detected → Qwen → Router → NTFY → Prompt → Claude) as a horizontal row of dots. This is the element that must stay extensible — adding an agent or a lifecycle step is adding one more dot/node, not redesigning the panel.

## Component rules

- **Status pill**: dot + label, background is the role color at 10% opacity, text and dot at full color. Six roles only: ok / warn / err / info / idle (+ neutral for pure counts).
- **Demand vs Calls**: always shown as two adjacent rows, "calls" row visually distinguished (subtle warm background tint) when demand > 0 and calls = 0, so a stalled bridge is visible without reading numbers carefully.
- **Tables**: hairline row borders (`--border`), no zebra striping, no box-shadow. Mono font for all numeric/ID columns, sans for text columns (setup names, asset symbols use mono since they're identifiers).
- **Buttons**: flat, 1px border, no gradients, no shadows. `start` variant = ok-colored border+text; `stop` = err-colored border+text; default = neutral panel-hi background.
- **Secrets**: never rendered, not even masked inline next to a label — masked forms (`ntfy.masked_topic()`) only appear if the user explicitly opens a details view, never on the main dashboard.

## Do / Don't

**Do**: keep every color tied to a real, sourced backend state; keep panels flat and single-level; keep the pipeline rail as the one reusable "flow" visual; let empty/idle states look calm (gray, not sad-face or broken-looking).

**Don't**: no charts, candles, sparklines, or P&L anywhere. No gradients or glassmorphism **outside the Agent Control Room** (see below — that page is a scoped exception, not a precedent). No emoji as icons (use the dot/pill system). No pure-black hacker-terminal green-on-black look. No card-inside-card nesting. No inventing a status value that doesn't exist in the backend (see the app's state-source mapping — every pill must cite where its value came from).

## Agent Control Room — scoped exception

Approved 2026-09-14 ("match the reference screenshot closely" was chosen over keeping the room fully flat). This section overrides "no gradients/glassmorphism" **only** for `#agent-room`'s `.room-floor` background and desk panels — every other panel in the app, and every other rule on this page, is unchanged.

**Allowed only inside the room**: a dark radial/linear gradient background (`rgba(91,157,246,...)` glow at two corners over the base `--bg`/`--panel` navy — see `ui/web/style.css`'s `.room-floor`), a light `backdrop-filter: blur()` glass tint on the desk panels. **Still never allowed, even here**: a fabricated progress percentage, a fabricated "thinking..." chat bubble, or any number/label that doesn't come from `get_state().agents`.

**Signature element**: each agent is a small robot **character** standing at its own workstation (SVG figure + desk + nameplate), built once in `ui/web/agent_room.js` and reused for every agent — adding an `AgentDefinition` to `AGENT_REGISTRY` produces one more workstation with no other code change. Characters stay geometric/restrained (rounded-rect head+torso, no cutesy face), matching the "professional, not childish" rule even though the room background now glows.

**State → pose** (`agent_room.js poseFor()`): `NOT_CONFIGURED`→dashed/dim disabled figure · `IDLE`/`ONLINE`/`OFFLINE`/`UNKNOWN`/`WAITING`→**sleeping** (lying down, closed eyes, floating "Z" marks — every flavour of "nothing to do" collapses to this one honest pose, since the backend has no signal for "awake-idle" vs "asleep") · `PROCESSING`→**working** (upright, arms at a floating console, chest-core + antenna pulse — the single allowed animation, reused from the existing `pulse` keyframe) · `COMPLETED`→calm figure with a small check mark · `DEGRADED`/`RATE_LIMITED`/`QUOTA_EXHAUSTED`/`AUTH_ERROR`→**error** (drooped head, warning tint). `RECEIVING` is a supported pose/class with no real trigger yet (Phase 1 prepares it; nothing produces it today).

**Connections (verified 2026-09-14)**: explicit directional edges come from AgentDefinition.connects_to and ui.agents.build_connections(). Registry order does not imply a relationship. Qwen → Red Team is topology only. app.js feeds real events to processCommunications(), which deduplicates by ID and triggers pulse/arrival reactions. Production input is currently empty because collect_real_agent_communications() returns []. The TEST MODE demo invokes animation directly without creating production communication or entering its dedup history.

**Do (room-specific)**: keep the glow subtle enough that text stays readable at both 1440 and a narrow ~760px window; keep every desk/pill color tied to `pillClass(status)`, the same table used everywhere else in the app.

**Don't (room-specific)**: no character facial expressions beyond the restrained eye/eyebrow shapes already defined; no more than the one existing pulse animation per state (no separate "extra" glow on top of it); no fabricated per-agent progress bar or freeform status text.

## Operational truth and audit corrections

This document governs visuals; ARCHITECTURE.md governs runtime roles. Current Sonnet/Orchestrator and Fable 5.1 labels are legacy registry entries, not the target architecture. Red Team remains NOT_CONFIGURED. Communication and receiving/waking reactions are scoped motion exceptions to the original single-pulse rule, triggered only by real events or explicit simulation.

| Display | Current source | Limit |
|---|---|---|
| Qwen IDLE/COMPLETED | Latest output/candidates in ui/agents.py | No live invocation telemetry; COMPLETED need not mean a successful Qwen review |
| Legacy model PROCESSING/health | Bridge health, latest event and analysis rows | PENDING may appear PROCESSING; target separates queued/running |
| Red Team NOT_CONFIGURED | Registry kind | No implementation |
| Connection | Explicit topology | Relationship, not activity |
| Production pulse | agent_communications | Correctly empty until actual persisted handoffs |
| System OK | Cached output/current store | Old cache is not current health; explicit age is required |

TEST MODE target: off on every load, never persisted, visual-only state/communication, no model calls, SQLite writes, event history or notifications. Current defect: the hidden Mocks tab still exposes Api.run_mock_alert() against the real store and run_notify_test() for ntfy. Toggle/demo isolation does not isolate those actions. R1 separates operational diagnostics from simulation and guards the API; hiding controls is insufficient.

Preserve ui/__init__.py's RADAR_COPY_PROMPT_POPUP_ENABLED default before config import and the fresh-process duplicate-window regression. It uses setdefault: an externally supplied value 1 is a caveat, not a proven safe frozen configuration. A tightening requires regression coverage.

## Evolution toward a living operations floor

Keep PyWebView, local assets and SVG workers. Add real queued/loading/started/finished/stale telemetry and replay fixtures first, then enrich station geometry, spatial layers and postures within the existing renderer. A worker never displays invented progress or thoughts. Working requires actual running state; waking is a brief event reaction.

Canvas/WebGL is only a measured alternative behind the same state/event adapter, not a current rewrite. Add reduced-motion behavior and reconnect/old-event handling. Movement corresponds to a defined real event or labeled replay/simulation. Production truth takes priority if an animation must be dropped.

## AI Game (paper game) — scoped direction

Approved 2026-09-29. The shipped screen is the reference: `ui/web/paper_game.js`, `ui/web/paper_office.js` and `ui/web/paper_office_engine.js`. Desktop only (pywebview); verify at 1440 wide and at a narrow ~960px window, no phone layout. `scripts/paper_game_screens.py` rebuilds the sample-data preview and captures the Game tab screens with headless Edge into a local folder (`--out-dir`); the design mocks and the captured PNG screens are not published.

**Design freedom (decision 2026-09-29).** On this screen the "no charts / no P&L", "no gradients" and "one animation per screen" rules above do not apply: charts, P&L, gradients, glows and several simultaneous animations are allowed. The rest of the app keeps the rules above. What still applies here, without exception, is the honesty rule: every number, state, movement and speech bubble comes from a real backend record, and the paper wallet is always labelled as pretend money.

**Audience and job.** For someone who does not know crypto: see at a glance what the AI agents are doing, which coin they picked and why in one plain sentence, how the pretend wallet is doing, and why each closed play won or lost. All copy in English, plain words, no jargon; numbers use English formatting (1,000.00, 0.55%); the glossary panel explains the few terms that remain.

**Layout (top to bottom).** Header with radar status pill → **the isometric office** (full width) with the cast legend under it → two columns: **the AI's wallet** (big balance, gain/loss since start, balance chart with a dashed start line, win/loss tally of the last plays, the cost sentence) and **the current play** (coin, direction pill, plain-language why, entry/now/stake, stop and target, time bar with the time left, the 3 decision steps) → **what happened** feed, one sentence per closed play with its close reason and the € result → **words you will see** glossary.

**Tokens.** The Signal Room palette above plus agent colours: Scout `#5B9DF6`, Analyst `#A78BFA`, Strategist `#F5B94D`, Boss `#F0616B`, Treasurer `#34D399`. Skin `#E8D5B7`. Office floor `#1A2029`, walls `#202734` / `#171D26`, grid lines `#222A35`. Inter for text, JetBrains Mono for money, prices and times.

**Signature element: the isometric office.** Five agents, one per real pipeline role: Scout (radar cycle), Analyst (Qwen review), Strategist (Sonnet call), Boss (Fable call), Treasurer (paper wallet). Each has a desk; there is a meeting table, a wall board and a wallet screen on the left wall.
- **Realism and movement:** agents walk along paths between their desk and the meeting table (no teleporting), with a walking cycle (legs and arms alternate), turn to face where they walk, sit and type at their desk while working (hands move, screen flickers), stand and gesture while speaking, and idle naturally (small shifts, looking around). The Boss sleeps on the sofa with floating "Z" marks until a real router decision for Fable wakes him, then walks to the table. Smooth easing, 60 fps, no jitter; `prefers-reduced-motion` turns walking into instant moves and stops idle motion.
- **What triggers movement (never invented):** a radar cycle makes the Scout work at his desk; an alert makes the Scout walk to the table and speak; a Qwen review makes the Analyst join; a router decision that an alert deserves Sonnet or Fable makes the Strategist or the Boss join (the Claude API stays off for paper trading, `CLAUDE_BRIDGE_DISPATCH_ENABLED = False`, so no analysis exists; their lines say only what the router decided, e.g. "This one deserves a closer look", and never show or imply an analysis); opening or closing a paper play makes the Treasurer update the wallet screen. With nothing happening, agents idle or sleep. Speech bubble text is generated from those records by templates, one active bubble at a time, queued, readable for at least 4 s.
- **The wall board:** a proper trading screen on the back wall, drawn on the wall plane: coin symbol and name, direction arrow, entry and current price, a small live price line since entry, the gain or loss so far before costs, and a countdown ring to the close. Green or red only when the play is actually up or down. When no play is open it shows "Looking for a play".
- **Stop, target, time left and close reason (EX-1 paper exit rule, 2026-09-30).** A play opened under the EX-1 paper policy carries a stop, a target and a 24-hour limit frozen when it opened; they are shown only from those records, never estimated. The wall board lists `stop` and `target` beside the entry/now rows (mono, backend digits; a value too wide for its slot is squeezed, never cut) and draws them as two faint level lines around the price line (dotted for the stop, dashed for the target, each with a tiny tag); the countdown ring runs to the 24-hour limit in hours ("23h", then minutes) with the note "time left". The current-play panel adds "Stop (loss limit)" and "Target (profit goal)" to the entry/now/stake facts and says "At most 23 h 12 min left (until HH:MM)."; the third decision step describes the rule (first price that reaches the loss limit or the profit goal, or after 24 hours at the latest). The "what happened" feed puts a small muted chip before the sentence with the recorded reason ("Hit the stop", "Hit the target", "Closed at the 24-hour limit"), and the sentence gives the level, the observed exit price and the costs. With no play open, the board's second line names the last play and its result and a third line its reason. The Treasurer's closing line names the reason ("I closed the SOL play at the stop: lost €0.45."). Prices on the board, panel and sentences are shown at the precision of the play's own entry quote. A legacy play (opened before the policy, fixed hold) shows none of these: no stop/target rows or lines, no chip, the old "Closes in … (at HH:MM)" text and "to close" on the ring. Missing values are absent, never 0.

**Pilot shadow panel (2026-09-30).** A full-width panel inside the Game tab, after "What happened" and before the glossary, titled "Pilot shadow" with the pill "Pretend money - no orders - LONG only" and a one-paragraph plain explanation. It is filled by `ui/web/pilot_shadow.js` from `Api.get_pilot_state()` (`ui/pilot_reader.py`, read-only `mode=ro`), polled only while the Game tab is visible. Under the explanation, one row of state pills: "Kill switch on" and each active lock in `--err`, otherwise a calm "No lock, kill switch off". Then two independent columns (one column below 1180 px), each block a flat `--panel-hi` card with a small uppercase heading: left **Account** (equity big in mono, change since the start, assigned, closed results, cash, day start (UTC), best value, % below it), **Open position** (coin, pair, "Bought, betting it goes up", quantity, bought at, stop, target, size, planned worst loss, time left) and **Locks and kill switch** (the switch with when, why and who; each lock with when it tripped, the equity against its reference and the limit, and either the review that cleared it or how to clear it); right **Limits** (a table: every EX-1 limit in words from its recorded %, its € amount at the current equity, what is in use and what is left; a negative room in `--err`), **Last sizing** (the four quantities each limit allows, the smallest marked "sets the size" in `--info`, the lot rounding, steps down, final quantity, the smallest-order and smallest-value checks in green/red, planned worst loss, size and fees) and **Why it did not trade** (counts by NO_TRADE reason, each with a short label and a one-line English explanation, then the most recent decisions). No control buttons: the kill switch and lock reviews stay in `scripts/pilot_control.py`. Every value is a backend string; a value not recorded reads "not recorded yet" or "—", never 0. Backend strings are inserted as text only. A lock or kill switch change is announced once through a polite live region without moving focus; a failed poll keeps the last reading with a visible "Could not refresh" note. Before the pilot has records, one calm empty line says why. Its open, locked and empty states at 1440 and 960 wide are captured by `scripts/paper_game_screens.py --only pilot-shadow`.

**Trend paper panel (2026-10-03).** A full-width panel inside the Game tab, after "Pilot shadow" and before the glossary, titled "Trend paper (research)" with the fixed pill "Paper only — research, not qualified, no real orders", a one-paragraph plain explanation and the fixed pre-tax note; both texts are in the page, so they show with or without a reading. It is filled by `ui/web/trend_paper.js` from `Api.get_trend_paper_state()` (`ui/trend_reader.py`, which only reads the trend paper ledger and never touches the network), polled every 15 s only while the Game tab is visible and TEST MODE is off. Under a one-line summary (days booked, first and last day, when the last record was written), one flat `--panel-hi` card per currency, EUR then USDT, each with a hairline table: a row group per strategy (ENS, ENS_VT, btc_trend5, btc_trend5_vt) headed by its name, what it holds and the buy-and-hold it is compared with, then one row per fee per leg (0.1%, 0.4%) with equity (and "as of" its last booked day), return since 2026-10-04, the difference from buy-and-hold in percentage points (the buy-and-hold return under it), max drawdown, trades, fees and the exposure after the last fill with its target, per coin. Inter for text, JetBrains Mono for every amount, percentage and date; the currency is named in the column headings. Return and the difference from buy-and-hold use `--ok`/`--err` only from the sign of the backend's string; drawdown is not coloured. Every value is a backend string inserted as text; a value not recorded reads "—", never 0. Before the first paper day one calm line says when the first fill is; a refused ledger shows an `--err` "Ledger refused" pill, the reason and the error code with no figure; a failed poll keeps the last reading with a visible "Could not refresh" note, and a reading older than one already applied is dropped. The only control is "Catch up now", which runs the existing paper catch-up (public market data, paper books only): it stays focusable and is marked `aria-disabled` while a request is pending, its result is shown under it and announced once in a polite live region without moving focus, then the panel reads the ledger again; it is never called in TEST MODE. When the backend counts skipped paper days (a public candle missing for good), one calm muted line under the summary gives their number and each day (mono) with the backend's reason, and is absent otherwise; a "Waiting for data" catch-up answer (a due day the public candles do not cover yet) reads as a warning, not a success, and shows no figure. Its populated, empty, skipped and refused states at 1440 and 960 wide are captured from synthetic ledgers by `scripts/paper_game_screens.py --only trend-paper`.

**Wallet value, fees and FX labels (2026-09-30).** Reporting only; the layout above is unchanged. The wallet's big number is labelled "Balance after closed plays". Under it, a bordered block titled "Value now, if every open play closed", with the `as_of` time and a pill, shows six backend amounts from `wallet.valuation`: "Total value now" (`total_equity`, lead), "Free cash (not in a play)" (`free_cash`), "Put into open plays" (`open_cost_basis`), "Open plays if closed now" (`liquidation_value`), "Result of closed plays" (`realized_pnl`) and "Open plays after all costs" (`open_net_pnl`). The pilot Account block shows the same breakdown from its own `valuation` ("Total value now, if the position closed", "Assigned at the start", "Free cash (not in a position)", "Put into the open position", "Open position if sold now", "Result of closed positions", "Open position after all costs"), keeps the recorded "Value the limits and locks use" separately, and carries the pill "PAPER" with the sentence that it is not SHADOW_LIVE and no real balance is read. The current play adds "If closed now" and "After all costs".
- **Accounting identity**: `total_equity = free_cash + liquidation_value = realized_balance + open_net_pnl`. Open positions are valued as if closed now at the price that could actually be sold at (bought back at for a paper SHORT), after the assumed commission on both legs; the entry consideration and each fee are counted once. The UI never recomputes these amounts.
- **Freshness rule**: a position is valued only on a valid price of exactly its own pair, recorded since it opened and at most 10 minutes old. Otherwise its value and the dependent totals show "—" with an `--warn` "unknown: price too old" (or "no price yet", "price not usable", "no usable price") note, the pill reads "price missing or too old", and a list names each unpriced pair with its reason. Free cash, the amount put in and the result of closed plays stay visible. Never a zero.
- **Fee provenance**: each open play, open pilot position and history item shows its stored commission per buy and per sell, e.g. "Assumed commission of 0.26% on the buy and again on the sell (ASSUMED, account tier unverified)." Stored rates are never replaced by today's public fee.
- **FX label**: a paper play on a pair not priced in the wallet currency says "Hypothetical simulation: this pair is priced in USD; its price moves are scaled onto the EUR stake with no exchange rate (FX excluded). This is not EUR inventory."
- Screens: the open, empty, stale and history wallet states and the stale pilot state, at 1440 and 960 wide, are captured from synthetic fixtures by `scripts/paper_game_screens.py`. The read-only records audit is `python scripts/audit_paper.py --db PATH [--format json|text]` (see ARCHITECTURE.md); it is a command, not a screen.

**Do / don't (this screen).** Do keep the honesty rule, the "sample data" banner only in mocks, and money always in mono with the currency. Don't show a number the backend did not produce, don't celebrate wins with confetti, don't hide the cost of each play.
