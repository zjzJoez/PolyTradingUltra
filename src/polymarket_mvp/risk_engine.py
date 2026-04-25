from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List

import requests

from .common import blocked_market_reason, dump_json, get_env_bool, get_env_float, get_env_int, market_reference_price, price_is_tradable, read_proposals, resolve_token_id, tradable_price_bounds, utc_now_iso
from .db import connect_db, init_db, proposal_record, proposal_id_for, update_proposal_workflow_fields
from .services.authorization_service import evaluate_authorization
from .services.event_cluster_service import MARKET_CLASS_CONFIG, classify_market_class
from .services.portfolio_risk_service import evaluate_portfolio_risk


def _clob_host() -> str:
    return (os.getenv("POLY_CLOB_HOST") or "https://clob.polymarket.com").rstrip("/")


def _clob_buy_price(record: Dict[str, Any]) -> float | None:
    """Fetch real CLOB BUY price for the selected outcome. Returns None if unavailable."""
    market = record.get("market")
    proposal = record["proposal_json"]
    if market is None:
        return None
    token_id = resolve_token_id(market["market_json"], proposal["outcome"])
    if not token_id:
        return None
    try:
        response = requests.get(
            f"{_clob_host()}/price",
            params={"token_id": token_id, "side": "BUY"},
            timeout=10,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        raw_price = payload.get("price") if isinstance(payload, dict) else payload
        return float(raw_price)
    except (requests.RequestException, TypeError, ValueError):
        return None


def _selected_outcome_has_live_price(record: Dict[str, Any]) -> bool:
    market = record.get("market")
    proposal = record["proposal_json"]
    if market is None:
        return False
    snapshot_price = market_reference_price(market["market_json"], proposal["outcome"])
    snapshot_has_price = isinstance(snapshot_price, float) and 0.0 <= snapshot_price <= 1.0
    clob_price = _clob_buy_price(record)
    if clob_price is not None and clob_price >= 0.0:
        return True
    return snapshot_has_price


def _real_available_balance_usdc() -> float | None:
    """Best-effort real collateral balance lookup for risk gating.

    Falls back to env-configured balance when real execution dependencies or
    credentials are unavailable.
    """
    try:
        from .poly_executor import _build_clob_client, _coerce_float, _extract_balance_value
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams  # pyright: ignore[reportMissingImports]
    except Exception:
        return None
    try:
        signature_type_raw = os.getenv("POLY_CLOB_SIGNATURE_TYPE") or os.getenv("SIGNATURE_TYPE")
        if not signature_type_raw:
            return None
        signature_type = int(signature_type_raw)
        client = _build_clob_client()
        collateral = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=signature_type)
        )
        raw_collateral_balance = None
        if isinstance(collateral, dict):
            raw_collateral_balance = _coerce_float(collateral.get("balance"))
        if raw_collateral_balance is not None:
            return raw_collateral_balance / 1_000_000.0
        return _extract_balance_value(collateral)
    except Exception:
        return None


def evaluate_proposal(record: Dict[str, Any]) -> Dict[str, Any]:
    max_order_usdc = get_env_float("POLY_RISK_MAX_ORDER_USDC", 10.0)
    min_confidence = get_env_float("POLY_RISK_MIN_CONFIDENCE", 0.6)
    max_slippage_bps = get_env_int("POLY_RISK_MAX_SLIPPAGE_BPS", 500)
    configured_balance = get_env_float("POLYMARKET_AVAILABLE_BALANCE_U", 100.0)
    real_available_balance = _real_available_balance_usdc()
    available_balance = (
        min(configured_balance, real_available_balance)
        if real_available_balance is not None
        else configured_balance
    )
    require_executable_market = get_env_bool("POLY_RISK_REQUIRE_EXECUTABLE_MARKET", True)
    reasons: List[str] = []
    market = record.get("market")
    proposal = record["proposal_json"]
    has_conviction_tier = bool(record.get("conviction_tier"))
    if market is None:
        reasons.append("market_missing_from_db")
    else:
        blocked_reason = blocked_market_reason(market)
        if blocked_reason:
            reasons.append(blocked_reason)
        # Market-class segmentation: live_enabled gate always applies. The
        # per-class max_order_usdc cap is bypassed when a conviction tier
        # has been assigned — strategy/conviction.compute_tier_size() and
        # portfolio_risk_service already enforce the relevant absolute and
        # portfolio-relative size bounds.
        if get_env_bool("POLY_RISK_MARKET_CLASS_ENABLED", True):
            market_class = classify_market_class(market)
            class_config = MARKET_CLASS_CONFIG.get(market_class, MARKET_CLASS_CONFIG.get("other"))
            if class_config:
                if not class_config["live_enabled"]:
                    reasons.append(f"market_class_disabled[{market_class}]")
                elif (
                    not has_conviction_tier
                    and float(proposal["recommended_size_usdc"]) > float(class_config["max_order_usdc"])
                ):
                    reasons.append(f"market_class_size_exceeded[{market_class}:max={class_config['max_order_usdc']}]")
        if not market.get("active") or market.get("closed") or not market.get("accepting_orders"):
            reasons.append("market_not_tradeable")
    # Same logic for the global max_order_usdc gate: when conviction tier
    # is set, trust the sizer (extreme tier is $15 at $50 balance, $30 at
    # $100, etc.; the legacy POLY_RISK_MAX_ORDER_USDC=5 default would
    # block every extreme entry).
    if not has_conviction_tier and proposal["recommended_size_usdc"] > max_order_usdc:
        reasons.append("size_above_risk_limit")
    # When the conviction-tier sizer has already qualified this proposal,
    # absolute confidence is irrelevant — long-tail bets intentionally use
    # confidence values like 0.20 (a 0.07 edge over a 0.13 market price is
    # still a real edge). The conviction sizer enforces edge thresholds via
    # strategy/conviction.compute_tier(); double-gating on raw confidence
    # would block every long-tail entry.
    if not record.get("conviction_tier") and proposal["confidence_score"] < min_confidence:
        reasons.append("confidence_below_threshold")
    if proposal["max_slippage_bps"] > max_slippage_bps:
        reasons.append("slippage_above_risk_limit")
    if proposal["recommended_size_usdc"] > available_balance:
        reasons.append("insufficient_balance")
    if require_executable_market and market is not None and not _selected_outcome_has_live_price(record):
        reasons.append("selected_outcome_has_no_live_price")
    selected_snapshot_price = market_reference_price(market["market_json"], proposal["outcome"]) if market is not None else None
    # Polymarket enforces a minimum of 5 shares per order; reject if order size can't meet that
    poly_min_shares = get_env_float("POLY_MIN_SHARES_PER_ORDER", 5.0)
    if selected_snapshot_price and selected_snapshot_price > 0:
        estimated_shares = proposal["recommended_size_usdc"] / selected_snapshot_price
        if estimated_shares < poly_min_shares:
            reasons.append(f"shares_below_polymarket_minimum[need={poly_min_shares},got={estimated_shares:.2f}]")
    if market is not None and selected_snapshot_price is not None and not price_is_tradable(selected_snapshot_price):
        min_price, max_price = tradable_price_bounds()
        reasons.append(f"selected_outcome_price_outside_tradable_band[{min_price:.2f},{max_price:.2f}]")
    if require_executable_market and market is not None and not reasons:
        max_divergence_bps = get_env_int("POLY_RISK_MAX_GAMMA_CLOB_DIVERGENCE_BPS", 500)
        clob_price = _clob_buy_price(record)
        gamma_price = selected_snapshot_price
        if clob_price is not None and gamma_price is not None and gamma_price > 0:
            divergence_bps = abs(clob_price - gamma_price) / gamma_price * 10000
            if divergence_bps > max_divergence_bps:
                reasons.append("gamma_clob_price_divergence_exceeded")
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
            "configured_balance_usdc": configured_balance,
            "real_available_balance_usdc": real_available_balance,
            "require_executable_market": require_executable_market,
            "reasons": reasons,
        },
    }


def _persist_risk_decision(conn, proposal_id: str, reasons: List[str]) -> None:
    """Save risk block reasons to proposals table for post-hoc attribution."""
    if not reasons:
        return
    try:
        import json as _json
        conn.execute(
            "UPDATE proposals SET risk_block_reasons_json = ?, updated_at = ? WHERE proposal_id = ?",
            (_json.dumps(reasons), utc_now_iso(), proposal_id),
        )
    except Exception:
        pass  # Column may not exist yet (pre-migration)


def evaluate_full_record(conn, record: Dict[str, Any]) -> Dict[str, Any]:
    single_risk = evaluate_proposal(record)
    if not single_risk["approved_for_approval_gate"]:
        _persist_risk_decision(conn, record["proposal_id"], single_risk["risk_summary"]["reasons"])
        return {
            "proposal_id": record["proposal_id"],
            "approved_for_approval_gate": False,
            "next_status": "risk_blocked",
            "risk_summary": single_risk["risk_summary"],
            "portfolio_risk": None,
            "authorization": None,
        }
    portfolio_risk = evaluate_portfolio_risk(conn, record)
    if not portfolio_risk["approved"]:
        _persist_risk_decision(conn, record["proposal_id"], portfolio_risk["reasons"])
        return {
            "proposal_id": record["proposal_id"],
            "approved_for_approval_gate": False,
            "next_status": "risk_blocked",
            "risk_summary": single_risk["risk_summary"],
            "portfolio_risk": portfolio_risk,
            "authorization": None,
        }
    authorization = evaluate_authorization(conn, record)
    next_status = "authorized_for_execution" if authorization["authorization_status"] == "matched_auto_execute" else "pending_approval"
    return {
        "proposal_id": record["proposal_id"],
        "approved_for_approval_gate": True,
        "next_status": next_status,
        "risk_summary": single_risk["risk_summary"],
        "portfolio_risk": portfolio_risk,
        "authorization": authorization,
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
            result = evaluate_full_record(conn, record)
            update_proposal_workflow_fields(
                conn,
                proposal_id,
                authorization_status=(result.get("authorization") or {}).get("authorization_status", "none"),
                status=result["next_status"],
            )
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
