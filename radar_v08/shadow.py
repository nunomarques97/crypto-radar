"""Shadow mode: run v0.7 and v0.8 (L0+L1 only) side by side, without letting
either affect the other. v0.8 never replaces v0.7 in this phase.

v0.7 is run WITHOUT its Qwen call (out of scope for this phase, and it would
require a local Ollama server that may not be running); its own candidate
scoring pipeline is used as-is, untouched, imported straight from the
top-level radar.py so the "real v0.7" is what's being compared against.
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

from . import config
from .heartbeat import run_heartbeat
from .output import write_json


def _import_v07():
    """Import the top-level v0.7 radar.py module by file path, so this works
    regardless of whether the repo root is on sys.path.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    return importlib.import_module("radar")


def run_v07_shadow() -> dict[str, Any]:
    v07 = _import_v07()
    started = time.perf_counter()

    candidates = v07.build_spot_candidates()

    futures = []
    futures_status = "OK"
    try:
        futures = v07.futures_tickers()
    except Exception:  # noqa: BLE001 - v0.7's own network call, degrade like v0.7 does
        futures_status = "UNAVAILABLE"

    v07.enrich_futures(candidates, futures)
    for c in candidates:
        v07.score_candidate(c)

    candidates.sort(key=lambda c: c.score, reverse=True)
    top = candidates[: v07.TOP_CANDIDATES]

    elapsed_ms = (time.perf_counter() - started) * 1000

    return {
        "schema_version": "0.7-shadow",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "SHADOW_V07",
        "qwen_skipped": True,
        "data_quality": {
            "spot_ticker": "OK",
            "futures_ticker": futures_status,
            "funding_semantics": "RAW_UNVERIFIED",
            "credentials_used": False,
        },
        "candidate_count": len(candidates),
        "top_candidates": [
            {
                "asset": c.base,
                "spot_pair": c.display,
                "futures_symbol": c.futures_symbol,
                "score": round(c.score, 2),
                "signals": c.signals or [],
            }
            for c in top
        ],
        "latency_ms": elapsed_ms,
    }


def compare(v07_output: dict[str, Any], v08_output: dict[str, Any]) -> dict[str, Any]:
    v07_assets = {c["asset"] for c in v07_output["top_candidates"]}
    v08_assets = {c["asset"] for c in v08_output["candidates"]}

    v07_rank = {c["asset"]: i for i, c in enumerate(v07_output["top_candidates"])}
    v08_rank = {c["asset"]: i for i, c in enumerate(v08_output["candidates"])}

    common = v07_assets & v08_assets
    only_v07 = sorted(v07_assets - v08_assets)
    only_v08 = sorted(v08_assets - v07_assets)

    rank_diff = {
        asset: {"v07_rank": v07_rank[asset], "v08_rank": v08_rank[asset]}
        for asset in sorted(common)
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "v07": {
            "candidate_count": v07_output["candidate_count"],
            "shortlist_size": len(v07_output["top_candidates"]),
            "latency_ms": v07_output["latency_ms"],
        },
        "v08": {
            "assets_eligible": v08_output["universe"]["assets_eligible"],
            "assets_tradeable": v08_output["universe"]["assets_tradeable"],
            "shortlist_size": len(v08_output["candidates"]),
            "warmup": v08_output["warmup"],
        },
        "coverage": {
            "common_assets": sorted(common),
            "only_in_v07_shortlist": only_v07,
            "only_in_v08_shortlist": only_v08,
            "overlap_ratio": (len(common) / len(v07_assets)) if v07_assets else None,
        },
        "rank_comparison": rank_diff,
    }


def run_shadow() -> dict[str, Any]:
    v07_output = run_v07_shadow()
    write_json(v07_output, config.OUTPUT_V07_PATH)

    v08_output = run_heartbeat(mode="SHADOW")
    write_json(v08_output, config.OUTPUT_V08_PATH)

    comparison = compare(v07_output, v08_output)
    write_json(comparison, config.SHADOW_COMPARISON_PATH)

    return {"v07": v07_output, "v08": v08_output, "comparison": comparison}
