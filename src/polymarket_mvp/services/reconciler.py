from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List

from ..common import parse_iso8601, utc_now_iso
from ..db import (
    list_executions,
    position_for_execution,
    record_order_reconciliation,
    record_position,
    record_position_event,
    update_execution,
)
from ..poly_executor import _build_clob_client, _normalize_order_status


def cancel_stale_orders(conn) -> List[Dict[str, Any]]:
    """Auto-cancel submitted/live orders that exceed their order_live_ttl_seconds."""
    executions = list_executions(conn, statuses=["submitted", "live"], mode="real")
    if not executions:
        return []
    now = parse_iso8601(utc_now_iso())
    results: List[Dict[str, Any]] = []
    client = None
    for execution in executions:
        intent = execution.get("order_intent_json") or {}
        ttl = intent.get("order_live_ttl_seconds")
        posted_at = intent.get("order_posted_at")
        if not ttl or not posted_at:
            continue
        deadline = parse_iso8601(posted_at) + timedelta(seconds=int(ttl))
        if now < deadline:
            continue
        order_id = execution.get("txhash_or_order_id")
        if order_id:
            try:
                if client is None:
                    client = _build_clob_client()
                client.cancel(order_id)
            except Exception:
                pass
        update_execution(conn, int(execution["id"]), {
            "status": "failed",
            "error_message": f"auto_cancelled_ttl_{ttl}s",
        })
        position = position_for_execution(conn, int(execution["id"]))
        if position and position["status"] == "open_requested":
            record_position(conn, {**position, "status": "cancelled", "updated_at": utc_now_iso()})
            record_position_event(conn, {
                "position_id": position["id"],
                "event_type": "reconcile",
                "payload_json": {"reason": "stale_order_auto_cancelled", "ttl": ttl},
            })
        results.append({
            "execution_id": execution["id"],
            "order_id": order_id,
            "action": "auto_cancelled",
            "ttl": ttl,
        })
    return results


def reconcile_live_orders(conn) -> List[Dict[str, Any]]:
    executions = list_executions(conn, statuses=["submitted", "live"], mode="real")
    if not executions:
        return []
    client = _build_clob_client()
    results: List[Dict[str, Any]] = []
    for execution in executions:
        order_id = execution.get("txhash_or_order_id")
        if not order_id:
            continue
        try:
            snapshot = client.get_order(order_id)
            normalized_status = _normalize_order_status((snapshot or {}).get("status"))
            size_matched = (snapshot or {}).get("size_matched")
            price = (snapshot or {}).get("price")
            updated = update_execution(
                conn,
                int(execution["id"]),
                {
                    "status": normalized_status,
                    "avg_fill_price": price if normalized_status == "filled" else execution.get("avg_fill_price"),
                    "filled_size_usdc": execution.get("requested_size_usdc") if normalized_status == "filled" else execution.get("filled_size_usdc"),
                    "order_intent_json": {
                        **(execution.get("order_intent_json") or {}),
                        "order_status_snapshot": snapshot,
                    },
                },
            )
            results.append(
                record_order_reconciliation(
                    conn,
                    {
                        "execution_id": execution["id"],
                        "external_order_id": order_id,
                        "observed_status": normalized_status,
                        "observed_fill_qty": size_matched,
                        "observed_fill_price": price,
                        "reconciliation_result": "updated",
                        "payload_json": snapshot or {},
                    },
                )
            )
            if updated.get("status") == "failed":
                results[-1]["reconciliation_result"] = "failed"
        except Exception as exc:
            results.append(
                record_order_reconciliation(
                    conn,
                    {
                        "execution_id": execution["id"],
                        "external_order_id": order_id,
                        "observed_status": None,
                        "observed_fill_qty": None,
                        "observed_fill_price": None,
                        "reconciliation_result": "error",
                        "payload_json": {"error": str(exc)},
                    },
                )
            )
    return results
