from __future__ import annotations

from typing import Any, Dict, Mapping

from ..common import parse_iso8601, utc_now_iso
from ..db import list_strategy_authorizations
from .event_cluster_service import market_type_for


def _is_active_window(valid_from: str, valid_until: str, now_iso: str) -> bool:
    now = parse_iso8601(now_iso)
    return parse_iso8601(valid_from) <= now <= parse_iso8601(valid_until)


def _daily_realized_loss(conn, strategy_name: str | None) -> float:
    """Sum of negative realized PnL for today's resolved positions (returns positive number)."""
    if not strategy_name:
        return 0.0
    today = parse_iso8601(utc_now_iso()).date().isoformat()
    row = conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN p.realized_pnl < 0 THEN p.realized_pnl ELSE 0 END), 0)
        FROM positions p
        WHERE p.strategy_name = ? AND substr(p.updated_at, 1, 10) = ? AND p.status = 'resolved'
        """,
        (strategy_name, today),
    ).fetchone()
    return abs(float(row[0]))


def _strategy_open_positions(conn, strategy_name: str | None) -> int:
    """Count open positions for a strategy."""
    if not strategy_name:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*) FROM positions
        WHERE strategy_name = ? AND status IN ('open', 'open_requested', 'partially_filled')
        """,
        (strategy_name,),
    ).fetchone()
    return int(row[0])


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
        # Enforce max_daily_loss_usdc (schema field, previously unchecked)
        max_daily_loss = float(auth.get("max_daily_loss_usdc") or 0)
        if max_daily_loss > 0:
            daily_loss = _daily_realized_loss(conn, auth.get("strategy_name") or strategy_name)
            if daily_loss >= max_daily_loss:
                result["authorization_status"] = "daily_loss_limit_reached"
                result["matched_authorization"] = auth
                result["reason"] = f"daily_loss={daily_loss:.2f} >= max={max_daily_loss:.2f}"
                return result
        # Enforce max_open_positions (schema field, previously unchecked)
        max_open = int(auth.get("max_open_positions") or 0)
        if max_open > 0:
            open_count = _strategy_open_positions(conn, auth.get("strategy_name") or strategy_name)
            if open_count >= max_open:
                result["authorization_status"] = "position_limit_reached"
                result["matched_authorization"] = auth
                result["reason"] = f"open_positions={open_count} >= max={max_open}"
                return result
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


# proposals.authorization_status CHECK enum. evaluate_authorization() can
# return additional values like 'daily_loss_limit_reached' /
# 'position_limit_reached' to communicate *why* authorization was denied —
# those are not persistable to the column directly. Callers must route
# limit-reached outcomes to status='risk_blocked' (with the reason saved to
# proposals.risk_block_reasons_json) and persist authorization_status='none'.
PERSISTABLE_AUTHORIZATION_STATUSES = frozenset({"none", "matched_manual_only", "matched_auto_execute"})


def safe_authorization_status(raw: Any) -> str:
    """Coerce an evaluate_authorization() outcome to a value that satisfies
    the proposals.authorization_status CHECK constraint. Anything outside
    the canonical 3-value enum (e.g. limit-reached signals) is mapped to
    'none' — the underlying reason should already be captured in
    risk_block_reasons_json by _persist_risk_decision()."""
    value = str(raw) if raw is not None else "none"
    return value if value in PERSISTABLE_AUTHORIZATION_STATUSES else "none"
