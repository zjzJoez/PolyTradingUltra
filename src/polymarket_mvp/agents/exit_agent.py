from __future__ import annotations

from typing import Any, Dict, List

from ..common import get_env_int, parse_iso8601, utc_now_iso
from ..db import market_contexts, market_resolution, market_snapshot, record_exit_recommendation


def evaluate_position(conn, position: Dict) -> Dict:
    """Deterministic exit evaluation — no LLM required."""
    market = market_snapshot(conn, str(position["market_id"])) or {}
    resolution = market_resolution(conn, str(position["market_id"]))
    if resolution is not None:
        return {
            "position_id": position["id"],
            "recommendation": "close",
            "target_reduce_pct": 1.0,
            "reasoning": "Market already resolved; position should be closed and reviewed.",
            "confidence_score": 0.99,
            "payload_json": {"resolution": resolution},
        }
    end_date = market.get("end_date")
    if end_date:
        minutes_to_expiry = int((parse_iso8601(end_date) - parse_iso8601(utc_now_iso())).total_seconds() / 60)
        threshold = get_env_int("POLY_EXIT_EXPIRY_MINUTES", 30)
        if minutes_to_expiry <= threshold:
            return {
                "position_id": position["id"],
                "recommendation": "close",
                "target_reduce_pct": 1.0,
                "reasoning": f"Market is within {threshold} minutes of expiry.",
                "confidence_score": 0.8,
                "payload_json": {"minutes_to_expiry": minutes_to_expiry},
            }
    return {
        "position_id": position["id"],
        "recommendation": "hold",
        "target_reduce_pct": None,
        "reasoning": "No deterministic exit trigger fired.",
        "confidence_score": 0.55,
        "payload_json": {},
    }


def evaluate_position_with_llm(conn, position: Dict) -> Dict:
    """Ask OpenClaw for exit decision. Falls back to deterministic if unavailable."""
    from ..services.openclaw_adapter import is_enabled, maybe_generate_exit_proposals

    if not is_enabled():
        return evaluate_position(conn, position)

    # Deterministic rules always take priority
    deterministic = evaluate_position(conn, position)
    if deterministic["recommendation"] != "hold":
        return deterministic

    market = market_snapshot(conn, str(position["market_id"])) or {}
    contexts = market_contexts(conn, str(position["market_id"]))
    prompt_payload = {
        "position": {
            "id": position["id"],
            "market_id": position["market_id"],
            "outcome": position["outcome"],
            "entry_price": position.get("entry_price"),
            "size_usdc": position.get("size_usdc"),
            "filled_qty": position.get("filled_qty"),
            "unrealized_pnl": position.get("unrealized_pnl"),
            "entry_time": position.get("entry_time"),
        },
        "market": {
            "question": market.get("question"),
            "end_date": market.get("end_date"),
            "liquidity_usdc": market.get("liquidity_usdc"),
            "outcomes": market.get("outcomes_json") or market.get("outcomes", []),
        },
        "contexts": [
            {"title": c.get("title"), "text": (c.get("display_text") or "")[:500]}
            for c in (contexts or [])[:5]
        ],
    }
    try:
        result = maybe_generate_exit_proposals(prompt_payload)
    except Exception:
        return deterministic
    if result and isinstance(result, list) and len(result) > 0:
        item = result[0]
        recommendation = item.get("recommendation", "hold")
        if recommendation not in ("hold", "reduce", "close", "cancel"):
            recommendation = "hold"
        return {
            "position_id": position["id"],
            "recommendation": recommendation,
            "target_reduce_pct": item.get("target_reduce_pct"),
            "reasoning": item.get("reasoning", "OpenClaw exit decision"),
            "confidence_score": float(item.get("confidence_score", 0.6)),
            "payload_json": {"source": "openclaw", "raw": item},
        }
    return deterministic


def run_exit_agent(conn, position: Dict, *, use_llm: bool = False) -> Dict:
    if use_llm:
        recommendation = evaluate_position_with_llm(conn, position)
    else:
        recommendation = evaluate_position(conn, position)
    return record_exit_recommendation(conn, recommendation)
