from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List

import requests

from .common import dump_json, get_env_bool, get_env_float, get_env_int, read_proposals, resolve_token_id, utc_now_iso
from .db import connect_db, init_db, proposal_record, proposal_id_for, update_proposal_status


def _clob_host() -> str:
    return (os.getenv("POLY_CLOB_HOST") or "https://clob.polymarket.com").rstrip("/")


def _selected_outcome_has_live_price(record: Dict[str, Any]) -> bool:
    market = record.get("market")
    proposal = record["proposal_json"]
    if market is None:
        return False
    token_id = resolve_token_id(market["market_json"], proposal["outcome"])
    if not token_id:
        return False
    response = requests.get(
        f"{_clob_host()}/price",
        params={"token_id": token_id, "side": "BUY"},
        timeout=10,
    )
    if response.status_code == 404:
        return False
    response.raise_for_status()
    payload = response.json()
    raw_price = payload.get("price") if isinstance(payload, dict) else payload
    try:
        return float(raw_price) >= 0.0
    except (TypeError, ValueError):
        return False


def evaluate_proposal(record: Dict[str, Any]) -> Dict[str, Any]:
    max_order_usdc = get_env_float("POLY_RISK_MAX_ORDER_USDC", 10.0)
    min_confidence = get_env_float("POLY_RISK_MIN_CONFIDENCE", 0.6)
    max_slippage_bps = get_env_int("POLY_RISK_MAX_SLIPPAGE_BPS", 500)
    available_balance = get_env_float("POLYMARKET_AVAILABLE_BALANCE_U", 100.0)
    require_executable_market = get_env_bool("POLY_RISK_REQUIRE_EXECUTABLE_MARKET", True)
    reasons: List[str] = []
    market = record.get("market")
    proposal = record["proposal_json"]
    if market is None:
        reasons.append("market_missing_from_db")
    else:
        if not market.get("active") or market.get("closed") or not market.get("accepting_orders"):
            reasons.append("market_not_tradeable")
    if proposal["recommended_size_usdc"] > max_order_usdc:
        reasons.append("size_above_risk_limit")
    if proposal["confidence_score"] < min_confidence:
        reasons.append("confidence_below_threshold")
    if proposal["max_slippage_bps"] > max_slippage_bps:
        reasons.append("slippage_above_risk_limit")
    if proposal["recommended_size_usdc"] > available_balance:
        reasons.append("insufficient_balance")
    if require_executable_market and market is not None and not _selected_outcome_has_live_price(record):
        reasons.append("selected_outcome_has_no_live_price")
    approved = not reasons
    return {
        "proposal_id": record["proposal_id"],
        "approved_for_approval_gate": approved,
        "next_status": "pending_approval" if approved else "risk_blocked",
        "risk_summary": {
            "max_order_usdc": max_order_usdc,
            "min_confidence": min_confidence,
            "max_slippage_bps": max_slippage_bps,
            "available_balance_usdc": available_balance,
            "require_executable_market": require_executable_market,
            "reasons": reasons,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply hard risk checks to proposal records.")
    parser.add_argument("--proposal-file", required=True, help="Proposal JSON file.")
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    proposals = read_proposals(args.proposal_file)
    results = []
    with connect_db() as conn:
        for proposal in proposals:
            proposal_id = proposal_id_for(proposal)
            record = proposal_record(conn, proposal_id)
            if record is None:
                raise RuntimeError(f"Proposal {proposal_id} not found in database. Generate proposals before running risk-engine.")
            result = evaluate_proposal(record)
            update_proposal_status(conn, proposal_id, result["next_status"])
            results.append(result)
        conn.commit()
    payload = {
        "generated_at": utc_now_iso(),
        "results": results,
        "approved_count": sum(1 for item in results if item["approved_for_approval_gate"]),
        "blocked_count": sum(1 for item in results if not item["approved_for_approval_gate"]),
    }
    print(dump_json(payload, path=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
