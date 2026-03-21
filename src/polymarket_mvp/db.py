from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from .common import (
    get_db_path,
    json_dumps_compact,
    normalize_proposal,
    proposal_id_for,
    row_to_dict,
    rows_to_dicts,
    schema_path,
    utc_now_iso,
)


def connect_db(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | None = None) -> Path:
    db_path = path or get_db_path()
    with connect_db(db_path) as conn:
        conn.executescript(schema_path().read_text(encoding="utf-8"))
        conn.commit()
    return db_path


def upsert_market_snapshot(conn: sqlite3.Connection, market: Mapping[str, Any]) -> None:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO market_snapshots (
          market_id, question, slug, market_url, condition_id, active, closed, accepting_orders,
          end_date, seconds_to_expiry, days_to_expiry, liquidity_usdc, volume_usdc, volume_24h_usdc,
          outcomes_json, market_json, last_scanned_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market_id) DO UPDATE SET
          question=excluded.question,
          slug=excluded.slug,
          market_url=excluded.market_url,
          condition_id=excluded.condition_id,
          active=excluded.active,
          closed=excluded.closed,
          accepting_orders=excluded.accepting_orders,
          end_date=excluded.end_date,
          seconds_to_expiry=excluded.seconds_to_expiry,
          days_to_expiry=excluded.days_to_expiry,
          liquidity_usdc=excluded.liquidity_usdc,
          volume_usdc=excluded.volume_usdc,
          volume_24h_usdc=excluded.volume_24h_usdc,
          outcomes_json=excluded.outcomes_json,
          market_json=excluded.market_json,
          last_scanned_at=excluded.last_scanned_at
        """,
        (
            str(market["market_id"]),
            market.get("question"),
            market.get("slug"),
            market.get("market_url"),
            market.get("condition_id"),
            1 if market.get("active") else 0,
            1 if market.get("closed") else 0,
            1 if market.get("accepting_orders") else 0,
            market.get("end_date"),
            market.get("seconds_to_expiry"),
            market.get("days_to_expiry"),
            market.get("liquidity_usdc"),
            market.get("volume_usdc"),
            market.get("volume_24h_usdc"),
            json_dumps_compact(market.get("outcomes", [])),
            json.dumps(dict(market), sort_keys=False),
            now,
        ),
    )


def replace_market_contexts(conn: sqlite3.Connection, market_id: str, contexts: Iterable[Mapping[str, Any]]) -> None:
    conn.execute("DELETE FROM market_contexts WHERE market_id = ?", (market_id,))
    now = utc_now_iso()
    for context in contexts:
        conn.execute(
            """
            INSERT INTO market_contexts (
              market_id, source_type, source_id, title, published_at, url, raw_text,
              display_text, importance_weight, normalized_payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market_id,
                context["source_type"],
                context.get("source_id"),
                context.get("title"),
                context.get("published_at"),
                context.get("url"),
                context["raw_text"],
                context["display_text"],
                float(context.get("importance_weight", 1.0)),
                json.dumps(dict(context), sort_keys=False),
                now,
            ),
        )


def market_snapshot(conn: sqlite3.Connection, market_id: str) -> Dict[str, Any] | None:
    row = conn.execute("SELECT * FROM market_snapshots WHERE market_id = ?", (market_id,)).fetchone()
    item = row_to_dict(row)
    if item is None:
        return None
    item["outcomes"] = json.loads(item.pop("outcomes_json"))
    item["market_json"] = json.loads(item["market_json"])
    return item


def market_contexts(conn: sqlite3.Connection, market_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT source_type, source_id, title, published_at, url, raw_text, display_text,
               importance_weight, normalized_payload_json, created_at
        FROM market_contexts
        WHERE market_id = ?
        ORDER BY CASE source_type
                   WHEN 'perplexity' THEN 0
                   WHEN 'cryptopanic' THEN 1
                   ELSE 2
                 END,
                 COALESCE(published_at, '') DESC,
                 importance_weight DESC,
                 id ASC
        """,
        (market_id,),
    ).fetchall()
    items = rows_to_dicts(rows)
    for item in items:
        item["normalized_payload_json"] = json.loads(item["normalized_payload_json"])
    return items


def upsert_proposal(
    conn: sqlite3.Connection,
    proposal: Mapping[str, Any],
    *,
    decision_engine: str,
    status: str,
    context_payload: Mapping[str, Any],
) -> Dict[str, Any]:
    normalized = normalize_proposal(proposal)
    proposal_id = proposal_id_for(normalized)
    now = utc_now_iso()
    record = {
        "proposal_id": proposal_id,
        "market_id": normalized["market_id"],
        "outcome": normalized["outcome"],
        "confidence_score": normalized["confidence_score"],
        "recommended_size_usdc": normalized["recommended_size_usdc"],
        "reasoning": normalized["reasoning"],
        "decision_engine": decision_engine,
        "status": status,
        "max_slippage_bps": normalized["max_slippage_bps"],
        "proposal_json": json.dumps(normalized, sort_keys=False),
        "context_payload_json": json.dumps(context_payload, sort_keys=False),
        "created_at": now,
        "updated_at": now,
    }
    conn.execute(
        """
        INSERT INTO proposals (
          proposal_id, market_id, outcome, confidence_score, recommended_size_usdc, reasoning,
          decision_engine, status, max_slippage_bps, proposal_json, context_payload_json,
          created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(proposal_id) DO UPDATE SET
          market_id=excluded.market_id,
          outcome=excluded.outcome,
          confidence_score=excluded.confidence_score,
          recommended_size_usdc=excluded.recommended_size_usdc,
          reasoning=excluded.reasoning,
          decision_engine=excluded.decision_engine,
          status=excluded.status,
          max_slippage_bps=excluded.max_slippage_bps,
          proposal_json=excluded.proposal_json,
          context_payload_json=excluded.context_payload_json,
          updated_at=excluded.updated_at
        """,
        tuple(record.values()),
    )
    return record


def replace_proposal_contexts(conn: sqlite3.Connection, proposal_id: str, contexts: Iterable[Mapping[str, Any]]) -> None:
    conn.execute("DELETE FROM proposal_contexts WHERE proposal_id = ?", (proposal_id,))
    now = utc_now_iso()
    for context in contexts:
        conn.execute(
            """
            INSERT INTO proposal_contexts (
              proposal_id, source_type, source_id, title, published_at, url, raw_text,
              display_text, importance_weight, normalized_payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                context["source_type"],
                context.get("source_id"),
                context.get("title"),
                context.get("published_at"),
                context.get("url"),
                context["raw_text"],
                context["display_text"],
                float(context.get("importance_weight", 1.0)),
                json.dumps(context.get("normalized_payload_json", context), sort_keys=False),
                now,
            ),
        )


def proposal_record(conn: sqlite3.Connection, proposal_id: str) -> Dict[str, Any] | None:
    row = conn.execute("SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
    item = row_to_dict(row)
    if item is None:
        return None
    item["proposal_json"] = json.loads(item["proposal_json"])
    item["context_payload_json"] = json.loads(item["context_payload_json"])
    item["market"] = market_snapshot(conn, item["market_id"])
    item["contexts"] = proposal_contexts(conn, proposal_id)
    approval = conn.execute("SELECT * FROM approvals WHERE proposal_id = ?", (proposal_id,)).fetchone()
    item["approval"] = row_to_dict(approval)
    if item["approval"] and item["approval"].get("raw_callback_json"):
        item["approval"]["raw_callback_json"] = json.loads(item["approval"]["raw_callback_json"])
    return item


def proposal_contexts(conn: sqlite3.Connection, proposal_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT source_type, source_id, title, published_at, url, raw_text, display_text,
               importance_weight, normalized_payload_json, created_at
        FROM proposal_contexts
        WHERE proposal_id = ?
        ORDER BY CASE source_type
                   WHEN 'perplexity' THEN 0
                   WHEN 'cryptopanic' THEN 1
                   ELSE 2
                 END,
                 COALESCE(published_at, '') DESC,
                 importance_weight DESC,
                 id ASC
        """,
        (proposal_id,),
    ).fetchall()
    items = rows_to_dicts(rows)
    for item in items:
        item["normalized_payload_json"] = json.loads(item["normalized_payload_json"])
    return items


def list_proposals(conn: sqlite3.Connection, proposal_ids: Iterable[str]) -> List[Dict[str, Any]]:
    return [proposal_record(conn, proposal_id) for proposal_id in proposal_ids if proposal_record(conn, proposal_id) is not None]


def update_proposal_status(conn: sqlite3.Connection, proposal_id: str, status: str) -> None:
    conn.execute(
        "UPDATE proposals SET status = ?, updated_at = ? WHERE proposal_id = ?",
        (status, utc_now_iso(), proposal_id),
    )


def record_approval(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    decision: str,
    decided_at: str,
    telegram_user_id: str | None,
    telegram_username: str | None,
    callback_query_id: str,
    telegram_message_id: str | None,
    raw_callback_json: Mapping[str, Any],
) -> Dict[str, Any]:
    conn.execute(
        """
        INSERT INTO approvals (
          proposal_id, decision, decided_at, telegram_user_id, telegram_username,
          callback_query_id, telegram_message_id, raw_callback_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(proposal_id) DO UPDATE SET
          decision=excluded.decision,
          decided_at=excluded.decided_at,
          telegram_user_id=excluded.telegram_user_id,
          telegram_username=excluded.telegram_username,
          callback_query_id=excluded.callback_query_id,
          telegram_message_id=excluded.telegram_message_id,
          raw_callback_json=excluded.raw_callback_json
        """,
        (
            proposal_id,
            decision,
            decided_at,
            telegram_user_id,
            telegram_username,
            callback_query_id,
            telegram_message_id,
            json.dumps(raw_callback_json, sort_keys=False),
        ),
    )
    update_proposal_status(conn, proposal_id, decision)
    row = conn.execute("SELECT * FROM approvals WHERE proposal_id = ?", (proposal_id,)).fetchone()
    return row_to_dict(row) or {}


def approval_by_callback(conn: sqlite3.Connection, callback_query_id: str) -> Dict[str, Any] | None:
    row = conn.execute("SELECT * FROM approvals WHERE callback_query_id = ?", (callback_query_id,)).fetchone()
    return row_to_dict(row)


def decision_status_for(conn: sqlite3.Connection, proposal_ids: Iterable[str]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for proposal_id in proposal_ids:
        record = proposal_record(conn, proposal_id)
        if record is None:
            results.append({"proposal_id": proposal_id, "status": "missing"})
        else:
            results.append(
                {
                    "proposal_id": proposal_id,
                    "status": record["status"],
                    "proposal": record["proposal_json"],
                    "approval": record["approval"],
                }
            )
    return results


def record_execution(conn: sqlite3.Connection, execution: Mapping[str, Any]) -> Dict[str, Any]:
    payload = dict(execution)
    conn.execute(
        """
        INSERT INTO executions (
          proposal_id, mode, client_order_id, order_intent_json, requested_price, requested_size_usdc,
          max_slippage_bps, observed_worst_price, slippage_check_status, status, filled_size_usdc,
          avg_fill_price, txhash_or_order_id, slippage_bps, error_message, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["proposal_id"],
            payload["mode"],
            payload.get("client_order_id"),
            json.dumps(payload["order_intent_json"], sort_keys=False),
            payload.get("requested_price"),
            payload["requested_size_usdc"],
            payload["max_slippage_bps"],
            payload.get("observed_worst_price"),
            payload["slippage_check_status"],
            payload["status"],
            payload.get("filled_size_usdc"),
            payload.get("avg_fill_price"),
            payload.get("txhash_or_order_id"),
            payload.get("slippage_bps"),
            payload.get("error_message"),
            payload["created_at"],
            payload["updated_at"],
        ),
    )
    if payload["status"] in {"filled", "submitted", "live"}:
        update_proposal_status(conn, payload["proposal_id"], "executed")
    elif payload["status"] == "failed":
        current = conn.execute(
            "SELECT status FROM proposals WHERE proposal_id = ?",
            (payload["proposal_id"],),
        ).fetchone()
        current_status = current["status"] if current else None
        if payload.get("error_message") not in {"proposal_not_approved"} and current_status not in {"executed"}:
            update_proposal_status(conn, payload["proposal_id"], "failed")
    row = conn.execute("SELECT * FROM executions WHERE rowid = last_insert_rowid()").fetchone()
    result = row_to_dict(row) or {}
    if result.get("order_intent_json"):
        result["order_intent_json"] = json.loads(result["order_intent_json"])
    return result


def latest_execution(conn: sqlite3.Connection, proposal_id: str, mode: str | None = None) -> Dict[str, Any] | None:
    if mode:
        row = conn.execute(
            "SELECT * FROM executions WHERE proposal_id = ? AND mode = ? ORDER BY id DESC LIMIT 1",
            (proposal_id, mode),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM executions WHERE proposal_id = ? ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
    item = row_to_dict(row)
    if item and item.get("order_intent_json"):
        item["order_intent_json"] = json.loads(item["order_intent_json"])
    return item


def upsert_market_resolution(conn: sqlite3.Connection, market_id: str, resolved_outcome: str, payload: Mapping[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO market_resolutions (market_id, resolved_outcome, resolved_at, source_payload_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(market_id) DO UPDATE SET
          resolved_outcome=excluded.resolved_outcome,
          resolved_at=excluded.resolved_at,
          source_payload_json=excluded.source_payload_json
        """,
        (
            market_id,
            resolved_outcome,
            utc_now_iso(),
            json.dumps(payload, sort_keys=False),
        ),
    )
