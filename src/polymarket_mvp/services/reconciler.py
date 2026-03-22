from __future__ import annotations

from typing import Any, Dict, List

from ..db import list_executions, record_order_reconciliation, update_execution
from ..poly_executor import _build_clob_client, _normalize_order_status


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
