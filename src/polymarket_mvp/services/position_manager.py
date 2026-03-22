from __future__ import annotations

from typing import Any, Dict

from ..common import utc_now_iso
from ..db import position_for_execution, proposal_record, record_position, record_position_event


def _position_status_from_execution_status(status: str) -> str | None:
    if status in {"submitted", "live"}:
        return "open_requested"
    if status == "filled":
        return "open"
    return None


def sync_position_for_execution(conn, execution_id: int) -> Dict[str, Any] | None:
    row = conn.execute("SELECT * FROM executions WHERE id = ?", (execution_id,)).fetchone()
    if row is None:
        return None
    execution = dict(row)
    status = _position_status_from_execution_status(str(execution.get("status") or ""))
    if status is None:
        return None
    record = proposal_record(conn, str(execution["proposal_id"]))
    if record is None:
        return None
    existing = position_for_execution(conn, execution_id)
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
