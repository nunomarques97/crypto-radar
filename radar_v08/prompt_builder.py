"""Builds the "COPIAR PROMPT" text for one PROCESSED event.

RADAR -> QWEN -> DEMAND ROUTER -> EVENT -> WINDOWS NOTIFICATION -> COPIAR PROMPT

Reuses `context_builder.build_model_context` - the exact same reconstruction
the Claude Bridge itself uses - so the prompt is built from PERSISTED event
data only (SQLite `events` row + its `context_json`), never from live,
possibly-stale in-memory radar state. Every value that does not exist is the
literal string "UNAVAILABLE" (never fabricated) - see context_builder.py.

This module is pure formatting: no network call, no SQLite write, no Kraken/
Qwen/Claude access. It is safe to call for any persisted event, including a
synthetic test row that never went through the real pipeline.
"""

from __future__ import annotations

import json
from typing import Any

from .context_builder import UNAVAILABLE, build_model_context

_SEP = "=" * 50

# Defense in depth: this radar never touches Kraken private endpoints or any
# credential (see security.py), so no key matching these markers should ever
# exist in a persisted event's context - but a prompt is fed straight into a
# new Claude conversation, so redact defensively rather than trust that.
_SENSITIVE_KEY_MARKERS = (
    "api_key", "apikey", "secret", "token", "password", "credential", "cookie", "authorization",
)


def _field(value: Any) -> Any:
    return UNAVAILABLE if value in (None, "") else value


def _row_get(event_row: Any, key: str) -> Any:
    try:
        return event_row[key]
    except (KeyError, IndexError):
        return None


# build_model_context reconstructs a fixed, curated set of fields for the
# real Claude Bridge analysis payload - it deliberately drops any OTHER key
# that might be sitting in a persisted context_json. A synthetic/mock event
# (mock_alert.py) adds exactly these two extra markers so a prompt can prove
# it came from a test, not a real alert; nothing else is passed through, so
# a real event's context_json (which never has them) is completely unaffected.
_PASSTHROUGH_CONTEXT_KEYS = ("test_event", "source")


def _load_raw_context_json(event_row: Any) -> dict[str, Any]:
    raw = _row_get(event_row, "context_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _fmt_score(value: Any) -> Any:
    return f"{value:.2f}" if isinstance(value, (int, float)) else UNAVAILABLE


def _scrub_sensitive(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            key: ("REDACTED" if any(marker in str(key).lower() for marker in _SENSITIVE_KEY_MARKERS) else _scrub_sensitive(value))
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub_sensitive(item) for item in obj]
    return obj


def build_prompt_context(event_row: Any) -> dict[str, Any]:
    """The full 'DADOS DO RADAR' data block for one event: everything
    `build_model_context` reconstructs from `context_json` (L1/L2/L3 features,
    Qwen review, router reasoning, cost preview, futures/funding/OI), plus the
    columns that live directly on the events row and are not duplicated
    inside context_json (timestamp, reason, status).
    """
    context = build_model_context(event_row)
    raw_persisted = _load_raw_context_json(event_row)
    for key in _PASSTHROUGH_CONTEXT_KEYS:
        if key in raw_persisted:
            context[key] = raw_persisted[key]
    context["timestamp"] = _field(_row_get(event_row, "ts"))
    context["reason"] = _field(_row_get(event_row, "reason"))
    context["status"] = _field(_row_get(event_row, "status"))
    context["confidence"] = _field(_row_get(event_row, "confidence"))
    context["model_demand"] = _field(_row_get(event_row, "model_demand"))
    return _scrub_sensitive(context)


def build_prompt_text(event_row: Any) -> str:
    """One complete, ready-to-paste prompt for a NEW Claude conversation in
    the `crypto` Project, built entirely from this event's persisted data.
    Never includes credentials/secrets/tokens/cookies - see `_scrub_sensitive`
    and the security guards in security.py that keep them out of the radar
    in the first place.
    """
    context = build_prompt_context(event_row)

    event_id = _field(_row_get(event_row, "event_id"))
    timestamp = context["timestamp"]
    asset = _field(_row_get(event_row, "asset"))
    market = _field(_row_get(event_row, "market"))
    direction = _field(_row_get(event_row, "direction"))
    setup_type = _field(_row_get(event_row, "setup_type"))
    anomaly_score = _fmt_score(_row_get(event_row, "anomaly_score"))
    opportunity_score = _fmt_score(_row_get(event_row, "opportunity_score"))
    tradeability_score = _fmt_score(_row_get(event_row, "tradeability_score"))
    confidence = context["confidence"]
    model_demand = context["model_demand"]

    data_json = json.dumps(context, indent=2, ensure_ascii=False, default=str)

    return f"""{_SEP}
CRYPTO RADAR — ALERTA
{_SEP}

O Crypto Radar detetou uma oportunidade que merece análise.

Event ID: {event_id}
Timestamp: {timestamp}

Asset: {asset}
Market: {market}
Direction: {direction}
Setup: {setup_type}

Anomaly Score: {anomaly_score}
Opportunity Score: {opportunity_score}
Tradeability Score: {tradeability_score}
Confidence: {confidence}
Model Demand: {model_demand}

{_SEP}
DADOS DO RADAR
{_SEP}

{data_json}

{_SEP}
INSTRUÇÕES PARA CLAUDE
{_SEP}

Analisa este alerta usando o sistema de trading do Project `crypto`.

Antes da decisão:

1. Carrega a Skill `crypto-trading-system`.
2. Lê `TRADING_STATE.md`.
3. Lê o histórico relevante de `TRADING_HISTORY.md`.
4. Consulta o estado LIVE da Kraken.
5. Reconcilia o alerta do Crypto Radar com o estado atual da Kraken.
6. Não assumas que os dados do radar continuam atuais.
7. O radar é um detetor, não é a fonte final de verdade para preços, posições, ordens ou capital.

Depois faz uma análise completa da oportunidade.

Quero:

- dizer se a oportunidade continua válida agora;
- validar ou rejeitar o setup;
- melhor instrumento: Spot ou Futures;
- direction;
- entry;
- entry range;
- stop;
- TP1;
- TP2;
- leverage;
- margin;
- notional;
- maximum loss em EUR;
- maximum loss %;
- expected profit;
- fees;
- funding;
- liquidation;
- net R:R;
- capital status;
- principais riscos;
- invalidação;
- alternativas;
- comparação com posições existentes;
- decisão final.

Seguir integralmente a Skill `crypto-trading-system`.

Não executar nenhuma ordem.
Não cancelar ordens.
Não alterar leverage.
Não transferir fundos.

Apresenta uma decisão clara no final.

{_SEP}
END CRYPTO RADAR ALERT
{_SEP}
"""
