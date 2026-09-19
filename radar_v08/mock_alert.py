"""`python radar.py --mode mock-alert` - a fully synthetic, end-to-end dry
run of the alert pipeline, for validating Windows notification + ntfy +
prompt builder + clipboard + popup WITHOUT touching Kraken, Qwen, or Claude.

MOCK EVENT -> EVENT QUEUE -> WINDOWS NOTIFICATION -> NTFY -> PROMPT BUILDER
-> CLIPBOARD -> POPUP

The event is inserted through the exact same `events.create_event_if_new` /
SQLite `events` table real alerts use ("mesma estrutura usada pelos eventos
reais"), then run through the exact same `notifications.notify_for_event`
real PROCESSED events use - no separate/duplicated notification code path.
It is unambiguously tagged as synthetic:

- event_id starts with "MOCK-" (never generated for a real alert)
- `type` is "MOCK_TEST_EVENT", `reason` embeds "CRYPTO_RADAR_MOCK_TEST
  test_event=true"
- context_json carries "test_event": true and "source": "CRYPTO_RADAR_MOCK_TEST"
  (so build_prompt_text, unchanged, surfaces both in the copied prompt)

It never calls Kraken, Ollama/Qwen, or the Claude Bridge: this module never
imports kraken_spot/kraken_futures/qwen/claude_bridge. Because the mock event
is inserted directly as PROCESSED (never PENDING/DEFERRED), it is invisible
to every real-pipeline query (`find_open_event_by_dedup`, `find_actionable_
events`) - the real Claude Bridge can never pick it up, retry it, or bill a
model call for it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .context_builder import build_event_context
from .events import create_event_if_new
from .store import SnapshotStore
from .terminal import safe_print

MOCK_MARKER = "CRYPTO_RADAR_MOCK_TEST"

MOCK_ASSET = "PEPE"
MOCK_SPOT_PAIR = "PEPE/USD"
MOCK_FUTURES_SYMBOL = "PF_PEPEUSD"
MOCK_MARKET = "FUTURES"
MOCK_DIRECTION = "LONG"
MOCK_SETUP_TYPE = "BREAKOUT"
MOCK_ANOMALY_SCORE = 91.0
MOCK_OPPORTUNITY_SCORE = 82.0
MOCK_TRADEABILITY_SCORE = 88.0
MOCK_CONFIDENCE = "HIGH"
MOCK_MODEL_DEMAND = "FABLE"


@dataclass
class _MockL1Features:
    return_15m: float = 3.4
    return_1h: float = 5.1
    volume_intensity_15m: float = 2.8
    trades_intensity_15m: float = 2.1
    relative_btc_z: float = 1.9
    futures_oi_delta_1h: float = 0.12
    futures_oi_delta_15m: float = 0.05


class _MockOpportunity:
    score = MOCK_OPPORTUNITY_SCORE
    breakdown = {
        "momentum": 0.78, "momentum_coherence": 0.7, "acceleration": 0.6,
        "volume_confirmed": 0.82, "breakout_distance": 0.65, "freshness": 0.9,
        "relative_strength": 0.6, "derivatives_coherence": 0.7, "squeeze_release": 0.5,
        "cost_efficiency": 0.75,
    }
    derivatives_coherence = "COHERENT"


@dataclass
class _MockL2Features:
    return_1h_atr: float = 2.4
    breakout_state: str = "BREAKOUT_UP"
    flags: list = field(default_factory=lambda: ["volume_confirmed", "breakout_fresh"])


class _MockSetup:
    notes = ["breakout: 24h range cleared with 1.5x volume confirmation (mock)"]


class _MockL2Result:
    opportunity = _MockOpportunity()
    l2_features = _MockL2Features()
    setup = _MockSetup()


class _MockTradeability:
    score = MOCK_TRADEABILITY_SCORE
    state = "TRADEABLE"
    breakdown = {
        "spread": 0.9, "depth": 0.85, "slippage": 0.88, "book_imbalance_penalty": 0.05,
        "activity": 0.8, "futures_available": 1.0, "futures_quality": 0.82, "freshness": 0.95,
    }


class _MockL3Result:
    tradeability = _MockTradeability()
    cost_preview = {
        "spot": {"spread_bps": 8.0, "slippage_bps": 3.0, "total_cost_bps": 37.0, "slippage_source": "order_book"},
        "futures": {
            "spread_bps": 3.5, "slippage_bps": 1.5, "total_cost_bps": 10.0,
            "slippage_source": "order_book", "funding_raw": 0.00015, "funding_semantics": "RAW_UNVERIFIED",
        },
    }


class _MockQwenReview:
    setup_type = MOCK_SETUP_TYPE
    direction = MOCK_DIRECTION
    market = MOCK_MARKET
    veto = False
    call_sonnet = True
    call_fable = True
    confidence = "HIGH"
    reason = "Momentum and volume confirm the breakout; derivatives coherent with spot move. (mock)"
    data_quality_notes: list[str] = []


class _MockRouterResult:
    decision = MOCK_MODEL_DEMAND
    model_demand_score = 91.0
    confidence = "HIGH"
    confirmations = ["momentum_2atr_coherent", "taker_imbalance", "oi_confirms"]
    reasons = ["opportunity>=70 with 3 confirmations (mock)"]


def _mock_event_id(now: datetime) -> str:
    return f"MOCK-{now:%Y%m%d}-{uuid.uuid4().hex[:6]}"


def build_mock_context() -> dict[str, Any]:
    """The same shape a real event's context_json has (via
    context_builder.build_event_context) plus two explicit test markers, so
    prompt_builder.build_prompt_text - completely unchanged - surfaces
    `"test_event": true` in the copied prompt's JSON data block.
    """
    context = build_event_context(
        asset=MOCK_ASSET, spot_pair=MOCK_SPOT_PAIR, futures_symbol=MOCK_FUTURES_SYMBOL, market=MOCK_MARKET,
        current_price=0.0000091, setup_type=MOCK_SETUP_TYPE, direction=MOCK_DIRECTION,
        anomaly_score=MOCK_ANOMALY_SCORE,
        l1_features=_MockL1Features(), l2_result=_MockL2Result(), l3_result=_MockL3Result(),
        futures_snapshot={
            "open_interest": 18_400_000.0, "funding_rate_raw": 0.00015, "funding_semantics": "RAW_UNVERIFIED",
            "basis": 0.0021, "last": 0.0000091, "suspended": False, "post_only": False,
        },
        qwen_review=_MockQwenReview(), router_result=_MockRouterResult(),
        flags=["BREAKOUT_WITH_VOLUME", MOCK_MARKER],
    )
    context["test_event"] = True
    context["source"] = MOCK_MARKER
    return context


def create_mock_event(store: SnapshotStore, now: datetime | None = None) -> str:
    """Inserts one fully synthetic, already-PROCESSED event through the same
    events.create_event_if_new/SQLite `events` table real alerts use, so the
    rest of the pipeline (notifications, ntfy, prompt builder) is exercised
    end to end on real persistence rather than an in-memory fake.
    """
    now = now or datetime.now(timezone.utc)
    event_id, _created = create_event_if_new(
        store,
        ts=now.isoformat(),
        type_="MOCK_TEST_EVENT",
        asset=MOCK_ASSET,
        setup_type=MOCK_SETUP_TYPE,
        direction=MOCK_DIRECTION,
        market=MOCK_MARKET,
        anomaly_score=MOCK_ANOMALY_SCORE,
        opportunity_score=MOCK_OPPORTUNITY_SCORE,
        tradeability_score=MOCK_TRADEABILITY_SCORE,
        confidence=MOCK_CONFIDENCE,
        model_demand=MOCK_MODEL_DEMAND,
        reason=f"{MOCK_MARKER} test_event=true - synthetic dry run, never a real alert",
        status="PROCESSED",
        context=build_mock_context(),
        event_id=_mock_event_id(now),
    )
    return event_id


def _fmt(value: Any) -> str:
    return f"{value:.0f}" if isinstance(value, (int, float)) else str(value)


def format_mock_alert_report(event_id: str, notify_result: dict[str, Any]) -> str:
    bar = "=" * 40
    windows_line = "SENT ✅" if notify_result["windows_sent"] else "FAILED"
    ntfy_line = notify_result["ntfy_result"]
    if ntfy_line == "SUCCESS":
        ntfy_line = "SENT ✅"
    prompt_line = "COPIED ✅" if notify_result["prompt_copied"] else "FAILED"
    popup_line = "OPENED ✅" if notify_result["popup_opened"] else "SKIPPED/FAILED"

    return "\n".join([
        bar,
        "CRYPTO RADAR — MOCK TEST",
        bar,
        "",
        f"Event: {event_id}",
        f"Asset: {MOCK_ASSET}",
        f"Setup: {MOCK_SETUP_TYPE} {MOCK_DIRECTION}",
        f"Opportunity: {_fmt(MOCK_OPPORTUNITY_SCORE)}",
        f"Tradeability: {_fmt(MOCK_TRADEABILITY_SCORE)}",
        f"Model: {MOCK_MODEL_DEMAND}",
        "",
        f"Windows notification: {windows_line}",
        f"NTFY: {ntfy_line}",
        f"Prompt: {prompt_line}",
        f"Popup: {popup_line}",
        "",
        "Nenhum acesso à Kraken.",
        "Nenhuma chamada Qwen.",
        "Nenhuma chamada Claude.",
        "Nenhuma operação executada.",
        "",
        bar,
    ])


def run_mock_alert(store: SnapshotStore) -> dict[str, Any]:
    """Runs the full MOCK EVENT -> EVENT QUEUE -> WINDOWS NOTIFICATION -> NTFY
    -> PROMPT BUILDER -> CLIPBOARD -> POPUP chain and prints the terminal
    report. Returns {"event_id": ..., "notify_result": {...}} for tests/CLI
    exit-code decisions.
    """
    now = datetime.now(timezone.utc)
    event_id = create_mock_event(store, now=now)

    # Marks the same way a real MEDIUM/HIGH title/body pair would (task
    # section "NOTIFICAÇÃO WINDOWS"/"NTFY") - notify_for_event is the exact
    # function claude_bridge.run_bridge_cycle's notify_fn calls for a real
    # PROCESSED event, so this is a genuine dry run of that path, not a copy.
    payload = {
        "event_id": event_id, "asset": MOCK_ASSET, "model": MOCK_MODEL_DEMAND,
        "setup_type": MOCK_SETUP_TYPE, "direction": MOCK_DIRECTION,
        "opportunity_score": MOCK_OPPORTUNITY_SCORE, "tradeability_score": MOCK_TRADEABILITY_SCORE,
        "recommendation": None, "test_event": True,
    }
    # local import: keeps this module import-order-independent
    from . import notifications

    notify_result = notifications.notify_for_event(payload, store=store)
    store.mark_event_notified(event_id, datetime.now(timezone.utc).isoformat())

    safe_print(format_mock_alert_report(event_id, notify_result))
    return {"event_id": event_id, "notify_result": notify_result}
