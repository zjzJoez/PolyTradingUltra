from __future__ import annotations

from typing import Dict

from ..common import get_env_int, parse_iso8601, utc_now_iso
from ..db import market_resolution, market_snapshot, record_exit_recommendation


def evaluate_position(conn, position: Dict) -> Dict:
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


def run_exit_agent(conn, position: Dict) -> Dict:
    recommendation = evaluate_position(conn, position)
    return record_exit_recommendation(conn, recommendation)
