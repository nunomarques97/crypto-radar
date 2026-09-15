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

Approved 2026-09-14 (Sponsor picked "match the reference screenshot closely" over keeping the room fully flat). This section overrides "no gradients/glassmorphism" **only** for `#agent-room`'s `.room-floor` background and desk panels — every other panel in the app, and every other rule on this page, is unchanged.

**Allowed only inside the room**: a dark radial/linear gradient background (`rgba(91,157,246,...)` glow at two corners over the base `--bg`/`--panel` navy — see `ui/web/style.css`'s `.room-floor`), a light `backdrop-filter: blur()` glass tint on the desk panels. **Still never allowed, even here**: a fabricated progress percentage, a fabricated "thinking..." chat bubble, or any number/label that doesn't come from `get_state().agents`.

**Signature element**: each agent is a small robot **character** standing at its own workstation (SVG figure + desk + nameplate), built once in `ui/web/agent_room.js` and reused for every agent — adding an `AgentDefinition` to `AGENT_REGISTRY` produces one more workstation with no other code change. Characters stay geometric/restrained (rounded-rect head+torso, no cutesy face), matching the "professional, not childish" rule even though the room background now glows.

**State → pose** (`agent_room.js poseFor()`): `NOT_CONFIGURED`→dashed/dim disabled figure · `IDLE`/`ONLINE`/`OFFLINE`/`UNKNOWN`/`WAITING`→**sleeping** (lying down, closed eyes, floating "Z" marks — every flavour of "nothing to do" collapses to this one honest pose, since the backend has no signal for "awake-idle" vs "asleep") · `PROCESSING`→**working** (upright, arms at a floating console, chest-core + antenna pulse — the single allowed animation, reused from the existing `pulse` keyframe) · `COMPLETED`→calm figure with a small check mark · `DEGRADED`/`RATE_LIMITED`/`QUOTA_EXHAUSTED`/`AUTH_ERROR`→**error** (drooped head, warning tint). `RECEIVING` is a supported pose/class with no real trigger yet (Phase 1 prepares it; nothing produces it today).

**Connections (verified 2026-09-14)**: explicit directional edges come from AgentDefinition.connects_to and ui.agents.build_connections(). Registry order does not imply a relationship. Qwen → Red Team is topology only. app.js feeds real events to processCommunications(), which deduplicates by ID and triggers pulse/arrival reactions. Production input is currently empty because collect_real_agent_communications() returns []. The TEST MODE demo invokes animation directly without creating production communication or entering its dedup history.

**Do (room-specific)**: keep the glow subtle enough that text stays readable at both 1440 and a narrow ~760px window; keep every desk/pill color tied to `pillClass(status)`, the same table used everywhere else in the app.

**Don't (room-specific)**: no character facial expressions beyond the restrained eye/eyebrow shapes already defined; no more than the one existing pulse animation per state (no separate "extra" glow on top of it); no fabricated per-agent progress bar or freeform status text.

## Operational truth and takeover corrections

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
