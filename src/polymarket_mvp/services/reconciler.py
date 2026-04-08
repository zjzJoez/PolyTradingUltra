from __future__ import annotations

import json
import os
from datetime import timedelta
from typing import Any, Dict, List

import requests

from ..common import parse_iso8601, utc_now_iso
from ..db import (
    list_executions,
    list_positions,
    position_for_execution,
    record_order_reconciliation,
    record_position,
    record_position_event,
    update_execution,
    upsert_market_resolution,
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
        # After real execution the intent is nested under "request": {"request": {...}, "response": ..., "preflight": ...}
        request = intent.get("request") or intent
        ttl = request.get("order_live_ttl_seconds")
        posted_at = request.get("order_posted_at")
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


def cancel_orphaned_positions(conn) -> int:
    """Cancel open_requested positions whose execution already failed (e.g. API errors).

    This cleans up positions stuck in open_requested when the underlying execution
    never actually reached the exchange.
    """
    count = conn.execute(
        """
        UPDATE positions SET status = 'cancelled', updated_at = ?
        WHERE status = 'open_requested'
          AND execution_id IN (
              SELECT id FROM executions WHERE status = 'failed'
          )
        """,
        (utc_now_iso(),),
    ).rowcount
    return count


def check_and_backfill_resolutions(conn) -> List[Dict[str, Any]]:
    """Check open positions against Gamma API — detect resolved markets and write to market_resolutions.

    Returns a list of newly resolved markets detected this pass.
    """
    gamma_base = (os.getenv("POLYMARKET_GAMMA_API") or "https://gamma-api.polymarket.com").rstrip("/")
    positions = list_positions(conn, statuses=["open", "partially_filled", "closing"])
    if not positions:
        return []
    # De-duplicate market IDs
    market_ids = list({str(pos["market_id"]) for pos in positions})
    resolved: List[Dict[str, Any]] = []
    for market_id in market_ids:
        # Skip if already recorded
        existing = conn.execute("SELECT market_id FROM market_resolutions WHERE market_id = ?", (market_id,)).fetchone()
        if existing:
            continue
        try:
            resp = requests.get(f"{gamma_base}/markets/{market_id}", timeout=10)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            continue
        if not data.get("closed"):
            continue
        # Parse outcomes + prices to detect winner
        raw_outcomes = data.get("outcomes")
        raw_prices = data.get("outcomePrices")
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
        if not raw_outcomes or not raw_prices or len(raw_outcomes) != len(raw_prices):
            continue
        # Find the outcome that resolved to price ~1.0
        winning_outcome = None
        for name, price_str in zip(raw_outcomes, raw_prices):
            try:
                price = float(price_str)
            except (TypeError, ValueError):
                continue
            if price >= 0.99:
                winning_outcome = str(name)
                break
        if winning_outcome is None:
            continue
        upsert_market_resolution(conn, market_id, winning_outcome, data)
        resolved.append({"market_id": market_id, "resolved_outcome": winning_outcome})
    return resolved


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
