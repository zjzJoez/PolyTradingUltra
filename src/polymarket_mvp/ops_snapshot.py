from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .common import debug_events_path, get_env_int, parse_iso8601, utc_now_iso
from .db import (
    latest_execution,
    list_executions,
    list_kill_switches,
    list_positions,
    proposal_record,
)


DEFAULT_LIMITS = {
    "recent_decisions": 25,
    "recent_failures": 25,
    "recent_events": 50,
}

LOOP_CADENCES = {
    "scan": lambda: get_env_int("POLY_SCAN_INTERVAL_SECONDS", 30),
    "context": lambda: get_env_int("POLY_CONTEXT_INTERVAL_SECONDS", 60),
    "propose": lambda: get_env_int("POLY_DECISION_INTERVAL_SECONDS", 30),
    "expiry": lambda: 5,
    "execute": lambda: 10,
    "reconcile": lambda: get_env_int("POLY_RECONCILE_INTERVAL_SECONDS", 10),
    "exit": lambda: get_env_int("POLY_EXIT_INTERVAL_SECONDS", 30),
    "review": lambda: 60,
}


def _seconds_between(later_iso: str, earlier_iso: str) -> float | None:
    if not later_iso or not earlier_iso:
        return None
    try:
        return (parse_iso8601(later_iso) - parse_iso8601(earlier_iso)).total_seconds()
    except Exception:
        return None


def _seconds_until(now_iso: str, future_iso: str | None) -> int | None:
    if not future_iso:
        return None
    delta = _seconds_between(future_iso, now_iso)
    if delta is None:
        return None
    return int(delta)


def _seconds_since(now_iso: str, past_iso: str | None) -> int | None:
    if not past_iso:
        return None
    delta = _seconds_between(now_iso, past_iso)
    if delta is None:
        return None
    return int(delta)


def _tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception:
        return []
    items: list[dict[str, Any]] = []
    for raw in lines[-limit:]:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            items.append(parsed)
    return list(reversed(items))


def _normalize_failure_category(message: str | None, *, source: str) -> str:
    text = (message or "").lower()
    if "slippage_exceeded" in text:
        return "slippage_exceeded"
    if "allowance" in text or "spender" in text:
        return "allowance_missing"
    if "insufficient collateral balance" in text or "insufficient_balance" in text or "not enough balance" in text:
        return "insufficient_balance"
    if "gamma_clob_price_divergence_exceeded" in text:
        return "gamma_clob_divergence"
    if source == "telegram":
        return "telegram_error"
    if source == "reconcile":
        return "order_reconcile_error"
    if source == "autopilot":
        return "autopilot_loop_error"
    if source == "risk":
        return "risk_blocked"
    return f"{source}_error"


def load_recent_ops_events(*, limit: int = DEFAULT_LIMITS["recent_events"]) -> list[dict[str, Any]]:
    return _tail_jsonl(debug_events_path("approvals"), limit)


def _build_heartbeat_section(conn, now_iso: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT h1.*
        FROM autopilot_heartbeats h1
        JOIN (
          SELECT loop_name, MAX(id) AS max_id
          FROM autopilot_heartbeats
          GROUP BY loop_name
        ) latest
          ON latest.loop_name = h1.loop_name
         AND latest.max_id = h1.id
        ORDER BY h1.loop_name ASC
        """
    ).fetchall()
    heartbeats: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []
    for row in rows:
        cadence = LOOP_CADENCES.get(row["loop_name"], lambda: 30)()
        age_seconds = _seconds_since(now_iso, row["started_at"])
        duration_seconds = _seconds_between(row["finished_at"], row["started_at"]) if row["finished_at"] else None
        health = "green"
        if age_seconds is None or age_seconds > cadence * 3:
            health = "red"
        elif row["error_message"] or age_seconds > int(cadence * 1.5):
            health = "yellow"
        item = {
            "loop": row["loop_name"],
            "cadence_seconds": cadence,
            "last_started": row["started_at"],
            "last_finished": row["finished_at"],
            "age_seconds": age_seconds,
            "duration_seconds": duration_seconds,
            "items_processed": row["items_processed"],
            "last_error_text": row["error_message"],
            "health": health,
        }
        heartbeats.append(item)
        if health != "green":
            attention.append(
                {
                    "kind": "heartbeat",
                    "severity": "high" if health == "red" else "medium",
                    "title": f"{row['loop_name']} loop unhealthy",
                    "detail": row["error_message"] or f"Last heartbeat is {age_seconds}s old",
                }
            )
    return heartbeats, attention


def _build_pending_approvals(conn, now_iso: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = conn.execute(
        "SELECT proposal_id FROM proposals WHERE status = 'pending_approval' ORDER BY approval_expires_at ASC, updated_at ASC"
    ).fetchall()
    items: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []
    for row in rows:
        record = proposal_record(conn, str(row["proposal_id"]))
        if record is None:
            continue
        market = record.get("market") or {}
        expires_in = _seconds_until(now_iso, record.get("approval_expires_at"))
        item = {
            "proposal_id": record["proposal_id"],
            "proposal_kind": record.get("proposal_kind"),
            "market_id": record["market_id"],
            "market": market.get("question") or record["market_id"],
            "market_url": market.get("market_url"),
            "outcome": record["outcome"],
            "size_usdc": record["recommended_size_usdc"],
            "confidence_score": record["confidence_score"],
            "approval_requested_at": record.get("approval_requested_at"),
            "approval_expires_at": record.get("approval_expires_at"),
            "seconds_remaining": expires_in,
            "telegram_message_id": record.get("telegram_message_id"),
            "telegram_chat_id": record.get("telegram_chat_id"),
        }
        items.append(item)
        if expires_in is not None and expires_in <= 60:
            attention.append(
                {
                    "kind": "approval",
                    "severity": "high" if expires_in <= 30 else "medium",
                    "title": f"Approval expiring for {record['proposal_id']}",
                    "detail": f"{expires_in}s remaining for {item['market']} {item['outcome']}",
                }
            )
    return items, attention


def _build_live_orders(conn, now_iso: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    executions = list_executions(conn, statuses=["submitted", "live"], mode="real")
    items: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []
    for execution in executions:
        proposal = proposal_record(conn, execution["proposal_id"])
        market = (proposal or {}).get("market") or {}
        intent = execution.get("order_intent_json") or {}
        posted_at = intent.get("order_posted_at") or execution.get("created_at")
        ttl_seconds = intent.get("order_live_ttl_seconds") or (proposal or {}).get("order_live_ttl_seconds")
        age_seconds = _seconds_since(now_iso, posted_at)
        ttl_remaining = None
        if isinstance(ttl_seconds, (int, float)) and age_seconds is not None:
            ttl_remaining = int(float(ttl_seconds) - age_seconds)
        item = {
            "execution_id": execution["id"],
            "proposal_id": execution["proposal_id"],
            "proposal_kind": (proposal or {}).get("proposal_kind"),
            "market_id": execution.get("market_id") or (proposal or {}).get("market_id"),
            "market": market.get("question") or execution["proposal_id"],
            "market_url": market.get("market_url"),
            "outcome": (proposal or {}).get("outcome"),
            "order_id": execution.get("txhash_or_order_id"),
            "requested_price": execution.get("requested_price"),
            "requested_size_usdc": execution.get("requested_size_usdc"),
            "status": execution.get("status"),
            "order_live_ttl_seconds": ttl_seconds,
            "order_posted_at": posted_at,
            "age_seconds": age_seconds,
            "seconds_remaining": ttl_remaining,
        }
        items.append(item)
        if ttl_remaining is not None and ttl_remaining <= 60:
            attention.append(
                {
                    "kind": "live_order",
                    "severity": "high" if ttl_remaining <= 30 else "medium",
                    "title": f"Live order nearing stale cancel for {execution['proposal_id']}",
                    "detail": f"{ttl_remaining}s remaining on order {item['order_id']}",
                }
            )
    return items, attention


def _build_open_positions(conn) -> list[dict[str, Any]]:
    positions = list_positions(conn, statuses=["open_requested", "open", "partially_filled"])
    items: list[dict[str, Any]] = []
    for position in positions:
        market_row = conn.execute(
            "SELECT question, market_url FROM market_snapshots WHERE market_id = ?",
            (position["market_id"],),
        ).fetchone()
        items.append(
            {
                "id": position["id"],
                "proposal_id": position["proposal_id"],
                "execution_id": position["execution_id"],
                "market_id": position["market_id"],
                "market": (market_row["question"] if market_row else None) or position["market_id"],
                "market_url": market_row["market_url"] if market_row else None,
                "outcome": position["outcome"],
                "size_usdc": position["size_usdc"],
                "status": position["status"],
                "entry_price": position.get("entry_price"),
                "last_mark_price": position.get("last_mark_price"),
                "unrealized_pnl": position.get("unrealized_pnl"),
                "realized_pnl": position.get("realized_pnl"),
                "updated_at": position.get("updated_at"),
            }
        )
    return items


def _build_recent_decisions(conn, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT proposal_id FROM proposals ORDER BY updated_at DESC, proposal_id DESC LIMIT ?", (limit,)).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        record = proposal_record(conn, str(row["proposal_id"]))
        if record is None:
            continue
        market = record.get("market") or {}
        latest = latest_execution(conn, record["proposal_id"])
        reason = None
        if latest and latest.get("error_message"):
            reason = latest["error_message"]
        elif record.get("status") == "expired":
            reason = "Approval expired before operator response"
        elif record.get("status") == "pending_approval":
            reason = "Awaiting Telegram approval"
        elif record.get("status") == "risk_blocked":
            reason = "Blocked by risk engine"
        elif record.get("approval"):
            reason = f"Telegram decision: {record['approval']['decision']}"
        items.append(
            {
                "proposal_id": record["proposal_id"],
                "proposal_kind": record.get("proposal_kind"),
                "decision_engine": record.get("decision_engine"),
                "status": record.get("status"),
                "market_id": record["market_id"],
                "market": market.get("question") or record["market_id"],
                "market_url": market.get("market_url"),
                "outcome": record.get("outcome"),
                "size_usdc": record.get("recommended_size_usdc"),
                "confidence_score": record.get("confidence_score"),
                "approval_expires_at": record.get("approval_expires_at"),
                "reason": reason,
                "updated_at": record.get("updated_at"),
            }
        )
    return items


def _recent_risk_blocks(conn, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT proposal_id FROM proposals WHERE status = 'risk_blocked' ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        record = proposal_record(conn, str(row["proposal_id"]))
        if record is None:
            continue
        market = record.get("market") or {}
        message = "Proposal blocked by risk engine"
        items.append(
            {
                "kind": "risk",
                "category": _normalize_failure_category(message, source="risk"),
                "proposal_id": record["proposal_id"],
                "market": market.get("question") or record["market_id"],
                "outcome": record.get("outcome"),
                "status": record.get("status"),
                "message": message,
                "timestamp": record.get("updated_at"),
            }
        )
    return items


def _recent_execution_failures(conn, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM executions WHERE status = 'failed' ORDER BY updated_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        execution = dict(row)
        record = proposal_record(conn, execution["proposal_id"])
        market = (record or {}).get("market") or {}
        message = execution.get("error_message") or "Execution failed"
        items.append(
            {
                "kind": "execution",
                "category": _normalize_failure_category(message, source="execution"),
                "proposal_id": execution["proposal_id"],
                "execution_id": execution["id"],
                "market": market.get("question") or execution["proposal_id"],
                "status": execution.get("status"),
                "message": message,
                "timestamp": execution.get("updated_at"),
            }
        )
    return items


def _recent_reconcile_failures(conn, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT r.*, e.proposal_id
        FROM order_reconciliations r
        JOIN executions e ON e.id = r.execution_id
        WHERE r.reconciliation_result = 'error'
        ORDER BY r.created_at DESC, r.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        record = proposal_record(conn, row["proposal_id"])
        market = (record or {}).get("market") or {}
        message = payload.get("error") or "Order reconciliation error"
        items.append(
            {
                "kind": "reconcile",
                "category": _normalize_failure_category(message, source="reconcile"),
                "proposal_id": row["proposal_id"],
                "execution_id": row["execution_id"],
                "market": market.get("question") or row["proposal_id"],
                "status": row["reconciliation_result"],
                "message": message,
                "timestamp": row["created_at"],
            }
        )
    return items


def _recent_heartbeat_failures(conn, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM autopilot_heartbeats WHERE error_message IS NOT NULL AND trim(error_message) <> '' ORDER BY started_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        message = row["error_message"] or "Autopilot loop error"
        items.append(
            {
                "kind": "autopilot",
                "category": _normalize_failure_category(message, source="autopilot"),
                "loop": row["loop_name"],
                "status": "error",
                "message": message,
                "timestamp": row["started_at"],
            }
        )
    return items


def _recent_telegram_failures(limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for event in load_recent_ops_events(limit=limit * 3):
        event_type = str(event.get("type") or "")
        if event_type not in {"telegram_followup_failed", "auto_execute_failed"}:
            continue
        message = event.get("error") or event_type
        items.append(
            {
                "kind": "telegram",
                "category": _normalize_failure_category(message, source="telegram"),
                "proposal_id": event.get("proposal_id"),
                "status": event_type,
                "message": message,
                "timestamp": event.get("timestamp"),
            }
        )
        if len(items) >= limit:
            break
    return items


def _build_recent_failures(conn, limit: int) -> list[dict[str, Any]]:
    combined = (
        _recent_execution_failures(conn, limit)
        + _recent_risk_blocks(conn, limit)
        + _recent_reconcile_failures(conn, limit)
        + _recent_heartbeat_failures(conn, limit)
        + _recent_telegram_failures(limit)
    )
    combined.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
    return combined[:limit]


def build_ops_snapshot(conn, *, limits: Mapping[str, int] | None = None) -> dict[str, Any]:
    effective_limits = dict(DEFAULT_LIMITS)
    if limits:
        effective_limits.update({key: int(value) for key, value in limits.items()})
    now_iso = utc_now_iso()

    system_health, health_attention = _build_heartbeat_section(conn, now_iso)
    pending_approvals, approval_attention = _build_pending_approvals(conn, now_iso)
    live_orders, live_attention = _build_live_orders(conn, now_iso)
    open_positions = _build_open_positions(conn)
    recent_decisions = _build_recent_decisions(conn, effective_limits["recent_decisions"])
    recent_failures = _build_recent_failures(conn, effective_limits["recent_failures"])
    recent_events = load_recent_ops_events(limit=effective_limits["recent_events"])

    return {
        "timestamp": now_iso,
        "system_health": system_health,
        "needs_attention": health_attention + approval_attention + live_attention + recent_failures[:5],
        "pending_approvals": pending_approvals,
        "pending_count": len(pending_approvals),
        "live_orders": live_orders,
        "live_order_count": len(live_orders),
        "open_positions": open_positions,
        "open_position_count": len(open_positions),
        "recent_decisions": recent_decisions,
        "recent_failures": recent_failures,
        "recent_events": recent_events,
        "control_state": {
            "kill_switches": list_kill_switches(conn, active_only=True),
            "loop_intervals_seconds": {loop: resolver() for loop, resolver in LOOP_CADENCES.items()},
            "openclaw_agent_id": os.getenv("OPENCLAW_AGENT_ID") or "",
            "tg_auto_execute_mode": (os.getenv("TG_AUTO_EXECUTE_MODE") or "real").strip().lower(),
        },
    }
