from __future__ import annotations

from typing import Any, Dict, Mapping

from ..common import parse_iso8601, utc_now_iso
from ..db import list_strategy_authorizations
from .event_cluster_service import market_type_for


def _is_active_window(valid_from: str, valid_until: str, now_iso: str) -> bool:
    now = parse_iso8601(now_iso)
    return parse_iso8601(valid_from) <= now <= parse_iso8601(valid_until)


def evaluate_authorization(conn, record: Mapping[str, Any]) -> Dict[str, Any]:
    now_iso = utc_now_iso()
    market = record.get("market") or {}
    proposal = record["proposal_json"]
    topic = record.get("topic")
    strategy_name = record.get("strategy_name")
    event_cluster_id = record.get("event_cluster_id")
    market_type = market_type_for(market) if market else None
    result = {
        "authorization_status": "none",
        "matched_authorization": None,
        "reason": "no_match",
    }
    for auth in list_strategy_authorizations(conn, status="active"):
        if auth.get("strategy_name") and strategy_name and auth["strategy_name"] != strategy_name:
            continue
        if auth.get("scope_topic") and topic and auth["scope_topic"] != topic:
            continue
        if auth.get("scope_market_type") and market_type and auth["scope_market_type"] != market_type:
            continue
        if auth.get("scope_event_cluster_id") and event_cluster_id and int(auth["scope_event_cluster_id"]) != int(event_cluster_id):
            continue
        if not _is_active_window(auth["valid_from"], auth["valid_until"], now_iso):
            continue
        if float(proposal["recommended_size_usdc"]) > float(auth["max_order_usdc"]):
            continue
        if int(proposal["max_slippage_bps"]) > int(auth["max_slippage_bps"]):
            continue
        manual_cutoff = auth.get("requires_human_if_above_usdc")
        if auth.get("allow_auto_execute"):
            if manual_cutoff is not None and float(proposal["recommended_size_usdc"]) > float(manual_cutoff):
                result["authorization_status"] = "matched_manual_only"
                result["matched_authorization"] = auth
                result["reason"] = "manual_threshold_exceeded"
                return result
            result["authorization_status"] = "matched_auto_execute"
            result["matched_authorization"] = auth
            result["reason"] = "auto_authorized"
            return result
        result["authorization_status"] = "matched_manual_only"
        result["matched_authorization"] = auth
        result["reason"] = "matched_manual_only"
        return result
    return result
