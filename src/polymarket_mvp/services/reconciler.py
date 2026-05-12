from __future__ import annotations

import json
import os
import sys
import time as _time
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


def _parse_resolution_prices(raw_outcomes: Any, raw_prices: Any) -> tuple[list[str], list[float]] | None:
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
        return None
    outcomes = [str(name) for name in raw_outcomes]
    prices: list[float] = []
    for price_value in raw_prices:
        try:
            prices.append(float(price_value))
        except (TypeError, ValueError):
            return None
    return outcomes, prices


def _resolved_outcome_label(outcomes: list[str], prices: list[float]) -> str | None:
    for name, price in zip(outcomes, prices):
        if price >= 0.99:
            return name
    if len(prices) == 2 and all(abs(price - 0.5) <= 0.001 for price in prices):
        return "50-50"
    return None


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
            for _cancel_attempt in range(2):
                try:
                    if client is None:
                        client = _build_clob_client()
                    client.cancel(order_id)
                    break
                except Exception:
                    if _cancel_attempt == 0:
                        _time.sleep(1)
                    # second attempt failed — proceed anyway, reconciler will catch it
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
        conn.commit()
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
    """Check open positions AND shadow executions against Gamma API.

    Detects resolved markets and writes to market_resolutions.
    Shadow executions never create rows in the positions table, so without
    the extra query below, conviction-era shadow PnL is invisible (Gate 4
    always shows 0 resolved positions).

    Returns a list of newly resolved markets detected this pass.
    """
    gamma_base = (os.getenv("POLYMARKET_GAMMA_API") or "https://gamma-api.polymarket.com").rstrip("/")
    positions = list_positions(conn, statuses=["open", "partially_filled", "closing"])
    # Also include markets from shadow executions that haven't been resolved yet.
    shadow_rows = conn.execute(
        """
        SELECT DISTINCT p.market_id
        FROM shadow_executions se
        JOIN proposals p ON p.proposal_id = se.proposal_id
        LEFT JOIN market_resolutions mr ON mr.market_id = p.market_id
        WHERE mr.market_id IS NULL
        """
    ).fetchall()
    shadow_market_ids = {str(row["market_id"]) for row in shadow_rows}
    position_market_ids = {str(pos["market_id"]) for pos in positions}
    market_ids = list(position_market_ids | shadow_market_ids)
    if not market_ids:
        return []
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
        parsed = _parse_resolution_prices(data.get("outcomes"), data.get("outcomePrices"))
        if parsed is None:
            continue
        outcomes, prices = parsed
        resolved_outcome = _resolved_outcome_label(outcomes, prices)
        if resolved_outcome is None:
            continue
        upsert_market_resolution(conn, market_id, resolved_outcome, data)
        resolved.append({"market_id": market_id, "resolved_outcome": resolved_outcome})
        conn.commit()
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
            # Compute the actual fill in USDC from size_matched × price. The
            # CLOB reports size_matched whenever any fill has happened, even
            # if the order subsequently went terminal (LIVE → INVALID, or
            # CANCELED with partial fill). Before this fix we only recorded
            # fills when normalized_status == "filled", which silently
            # discarded every partial-fill-then-terminal case. That was the
            # core of position 1593's 20-day freeze.
            try:
                observed_fill_usdc = float(size_matched) * float(price) if size_matched and price else None
            except (TypeError, ValueError):
                observed_fill_usdc = None
            if normalized_status == "filled":
                # CLOB doesn't always send size_matched on FILLED; fall back
                # to the requested size, which by definition matches a full
                # fill.
                fill_usdc_to_record = observed_fill_usdc or execution.get("requested_size_usdc")
                price_to_record = price or execution.get("avg_fill_price")
            elif observed_fill_usdc and observed_fill_usdc > 0:
                # Terminal-but-partially-filled (e.g. INVALID after 10 of
                # 12.63 shares matched). Record the partial fill so the
                # position state machine downstream sees we hold tokens.
                fill_usdc_to_record = observed_fill_usdc
                price_to_record = price
            else:
                fill_usdc_to_record = execution.get("filled_size_usdc")
                price_to_record = execution.get("avg_fill_price")
            updated = update_execution(
                conn,
                int(execution["id"]),
                {
                    "status": normalized_status,
                    "avg_fill_price": price_to_record,
                    "filled_size_usdc": fill_usdc_to_record,
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
            conn.commit()
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
            conn.commit()
    return results


def assert_position_consistency(conn) -> List[Dict[str, Any]]:
    """Find and auto-repair positions that violate state invariants.

    Catches:
    - open/open_requested positions whose execution is failed
    - open/open_requested positions on resolved markets
    """
    now = utc_now_iso()
    repaired: List[Dict[str, Any]] = []

    # 1. Positions whose execution already failed
    rows = conn.execute(
        """
        SELECT p.id AS position_id, p.status AS pos_status, p.execution_id, e.status AS exec_status
        FROM positions p
        JOIN executions e ON e.id = p.execution_id
        WHERE p.status IN ('open', 'open_requested')
          AND e.status = 'failed'
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE positions SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (now, row["position_id"]),
        )
        record_position_event(conn, {
            "position_id": row["position_id"],
            "event_type": "reconcile",
            "payload_json": {"reason": "execution_failed_position_stale", "exec_status": row["exec_status"]},
        })
        repaired.append({"position_id": row["position_id"], "reason": "execution_failed"})

    # 2. Positions on resolved markets
    rows2 = conn.execute(
        """
        SELECT p.id AS position_id, p.status AS pos_status, p.market_id, mr.resolved_outcome
        FROM positions p
        JOIN market_resolutions mr ON mr.market_id = p.market_id
        WHERE p.status IN ('open', 'open_requested')
        """
    ).fetchall()
    for row in rows2:
        conn.execute(
            "UPDATE positions SET status = 'resolved', updated_at = ? WHERE id = ?",
            (now, row["position_id"]),
        )
        record_position_event(conn, {
            "position_id": row["position_id"],
            "event_type": "resolve",
            "payload_json": {"reason": "market_resolved_position_stale", "resolved_outcome": row["resolved_outcome"]},
        })
        repaired.append({"position_id": row["position_id"], "reason": "market_resolved"})

    if repaired:
        print(f"[reconciler] assert_position_consistency: repaired {len(repaired)} positions", file=sys.stderr)
    return repaired
