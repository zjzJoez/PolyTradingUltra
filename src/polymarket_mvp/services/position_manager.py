from __future__ import annotations

import json
from typing import Any, Dict

from ..common import market_reference_price, utc_now_iso
from ..db import (
    list_positions,
    market_resolution,
    market_snapshot,
    position_for_execution,
    proposal_record,
    record_position,
    record_position_event,
)


def _position_status_from_execution_status(status: str) -> str | None:
    if status in {"submitted", "live"}:
        return "open_requested"
    if status == "filled":
        return "open"
    return None


def _is_cancelled_execution_status(status: str) -> bool:
    normalized = str(status or "").strip().lower()
    return normalized == "failed" or normalized.startswith("canceled") or normalized.startswith("cancelled")


def _resolution_payout(resolution: Dict[str, Any], outcome: str) -> float | None:
    payload = resolution.get("source_payload_json") or {}
    raw_outcomes = payload.get("outcomes")
    raw_prices = payload.get("outcomePrices")
    if isinstance(raw_outcomes, str):
        try:
            raw_outcomes = json.loads(raw_outcomes)
        except Exception:
            raw_outcomes = None
    if isinstance(raw_prices, str):
        try:
            raw_prices = json.loads(raw_prices)
        except Exception:
            raw_prices = None
    if isinstance(raw_outcomes, list) and isinstance(raw_prices, list) and len(raw_outcomes) == len(raw_prices):
        for name, price_value in zip(raw_outcomes, raw_prices):
            if str(name) != str(outcome):
                continue
            try:
                return float(price_value)
            except (TypeError, ValueError):
                return None
    resolved_outcome = resolution.get("resolved_outcome")
    if resolved_outcome is None:
        return None
    return 1.0 if str(resolved_outcome) == str(outcome) else 0.0


def sync_position_for_execution(conn, execution_id: int) -> Dict[str, Any] | None:
    row = conn.execute("SELECT * FROM executions WHERE id = ?", (execution_id,)).fetchone()
    if row is None:
        return None
    execution = dict(row)
    execution_status = str(execution.get("status") or "")
    status = _position_status_from_execution_status(execution_status)
    record = proposal_record(conn, str(execution["proposal_id"]))
    if record is None:
        return None
    existing = position_for_execution(conn, execution_id)
    if existing is not None and existing.get("status") == "resolved":
        return existing
    if status is None:
        if existing is not None and _is_cancelled_execution_status(execution_status):
            payload = {
                **existing,
                "status": "cancelled",
                "updated_at": utc_now_iso(),
            }
            position = record_position(conn, payload)
            record_position_event(
                conn,
                {
                    "position_id": position["id"],
                    "event_type": "reconcile",
                    "payload_json": {
                        "execution_id": execution_id,
                        "execution_status": execution_status,
                        "mode": execution.get("mode"),
                        "error_message": execution.get("error_message"),
                    },
                },
            )
            return position
        return None
    entry_price = execution.get("avg_fill_price") or execution.get("requested_price")
    size_usdc = float(execution.get("filled_size_usdc") or execution.get("requested_size_usdc") or 0.0)
    filled_qty = None
    if entry_price:
        filled_qty = round(size_usdc / float(entry_price), 6)
    payload = {
        "proposal_id": record["proposal_id"],
        "execution_id": execution_id,
        "market_id": record["market_id"],
        "event_cluster_id": record.get("event_cluster_id"),
        "outcome": record["proposal_json"]["outcome"],
        "entry_price": entry_price,
        "size_usdc": size_usdc,
        "filled_qty": filled_qty,
        "status": status,
        "entry_time": execution.get("created_at") or utc_now_iso(),
        "last_mark_price": execution.get("avg_fill_price") or execution.get("requested_price"),
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "strategy_name": record.get("strategy_name"),
        "is_shadow": False,
        "mode": execution.get("mode") or "real",
        "created_at": existing.get("created_at") if existing else utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    position = record_position(conn, payload)
    record_position_event(
        conn,
        {
            "position_id": position["id"],
            "event_type": "open" if not existing else "reconcile",
            "payload_json": {
                "execution_id": execution_id,
                "execution_status": execution.get("status"),
                "mode": execution.get("mode"),
            },
        },
    )
    return position


def sync_all_positions(conn) -> list[Dict[str, Any]]:
    rows = conn.execute("SELECT id FROM executions ORDER BY id ASC").fetchall()
    synced = []
    for row in rows:
        position = sync_position_for_execution(conn, int(row["id"]))
        if position is not None:
            synced.append(position)
    return synced


def update_position_marks(conn) -> list[Dict[str, Any]]:
    """Recompute unrealized/realized P&L for all non-terminal positions.

    - If the market has resolved: computes realized_pnl, sets status='resolved'.
    - Otherwise: computes unrealized_pnl from current mark in market_snapshots.
    - Positions with missing entry_price/filled_qty are skipped silently.
    """
    active_statuses = ["open", "partially_filled", "closing"]
    positions = list_positions(conn, statuses=active_statuses)
    updated = []

    for pos in positions:
        market_id = pos["market_id"]
        outcome = pos["outcome"]
        entry_price = pos.get("entry_price")
        filled_qty = pos.get("filled_qty")

        if entry_price is None or filled_qty is None:
            continue
        entry_price = float(entry_price)
        filled_qty = float(filled_qty)

        # Resolution check first (takes priority over live mark)
        resolution = market_resolution(conn, market_id)
        if resolution is not None:
            resolved_outcome = resolution["resolved_outcome"]
            payout = _resolution_payout(resolution, outcome)
            if payout is None:
                continue
            realized_pnl = round((payout - entry_price) * filled_qty, 6)
            mark = payout
            payload = {
                **pos,
                "status": "resolved",
                "last_mark_price": mark,
                "unrealized_pnl": 0.0,
                "realized_pnl": realized_pnl,
                "updated_at": utc_now_iso(),
            }
            record_position(conn, payload)
            record_position_event(
                conn,
                {
                    "position_id": pos["id"],
                    "event_type": "resolve",
                    "payload_json": {
                        "resolved_outcome": resolved_outcome,
                        "realized_pnl": realized_pnl,
                    },
                },
            )
            updated.append(payload)
            continue

        # Live position: compute unrealized P&L from current snapshot
        snap = market_snapshot(conn, market_id)
        if snap is None:
            continue
        mark_price = market_reference_price(snap, outcome)
        if mark_price is None:
            continue
        mark_price = float(mark_price)
        unrealized_pnl = round((mark_price - entry_price) * filled_qty, 6)
        payload = {
            **pos,
            "last_mark_price": mark_price,
            "unrealized_pnl": unrealized_pnl,
            "updated_at": utc_now_iso(),
        }
        record_position(conn, payload)
        updated.append(payload)

    return updated
