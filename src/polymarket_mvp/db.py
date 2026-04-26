from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from .common import (
    get_db_path,
    get_env_int,
    json_dumps_compact,
    normalize_proposal,
    proposal_id_for,
    row_to_dict,
    rows_to_dicts,
    schema_path,
    utc_now_iso,
)
from .migrations import apply_pending_migrations, ensure_schema_migrations, mark_all_migrations_applied


class ManagedSQLiteConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            return super().__exit__(exc_type, exc_val, exc_tb)
        finally:
            self.close()


def connect_db(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    busy_timeout_seconds = max(5, get_env_int("POLY_SQLITE_BUSY_TIMEOUT_SECONDS", 60))
    conn = sqlite3.connect(
        db_path,
        timeout=busy_timeout_seconds,
        isolation_level="IMMEDIATE",
        factory=ManagedSQLiteConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_seconds * 1000}")
    return conn


def _user_table_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        """
    ).fetchone()
    return int(row[0] if row else 0)


def init_db(path: Path | None = None) -> Path:
    db_path = path or get_db_path()
    with connect_db(db_path) as conn:
        fresh = _user_table_count(conn) == 0
        if fresh:
            conn.executescript(schema_path().read_text(encoding="utf-8"))
            ensure_schema_migrations(conn)
            mark_all_migrations_applied(conn)
        else:
            ensure_schema_migrations(conn)
            apply_pending_migrations(conn)
            conn.executescript(schema_path().read_text(encoding="utf-8"))
        conn.commit()
    return db_path


def _json_loads_if_present(value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


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


def market_snapshot(conn: sqlite3.Connection, market_id: str) -> Dict[str, Any] | None:
    row = conn.execute("SELECT * FROM market_snapshots WHERE market_id = ?", (market_id,)).fetchone()
    item = row_to_dict(row)
    if item is None:
        return None
    item["outcomes"] = json.loads(item.pop("outcomes_json"))
    item["market_json"] = json.loads(item["market_json"])
    return item


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


def market_contexts(conn: sqlite3.Connection, market_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, source_type, source_id, title, published_at, url, raw_text, display_text,
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
        item["normalized_payload_json"] = _json_loads_if_present(item.get("normalized_payload_json"))
    return items


def upsert_event_cluster(conn: sqlite3.Connection, cluster: Mapping[str, Any]) -> Dict[str, Any]:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO event_clusters (
          cluster_key, topic, title, description, status, canonical_start_time,
          canonical_end_time, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cluster_key) DO UPDATE SET
          topic=excluded.topic,
          title=excluded.title,
          description=excluded.description,
          status=excluded.status,
          canonical_start_time=excluded.canonical_start_time,
          canonical_end_time=excluded.canonical_end_time,
          updated_at=excluded.updated_at
        """,
        (
            cluster["cluster_key"],
            cluster["topic"],
            cluster["title"],
            cluster.get("description"),
            cluster.get("status", "active"),
            cluster.get("canonical_start_time"),
            cluster.get("canonical_end_time"),
            now,
            now,
        ),
    )
    row = conn.execute("SELECT * FROM event_clusters WHERE cluster_key = ?", (cluster["cluster_key"],)).fetchone()
    return row_to_dict(row) or {}


def replace_market_event_links(conn: sqlite3.Connection, market_id: str, links: Iterable[Mapping[str, Any]]) -> None:
    conn.execute("DELETE FROM market_event_links WHERE market_id = ?", (market_id,))
    now = utc_now_iso()
    for link in links:
        conn.execute(
            """
            INSERT INTO market_event_links (
              market_id, event_cluster_id, link_confidence, link_reason, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                market_id,
                int(link["event_cluster_id"]),
                float(link.get("link_confidence", 1.0)),
                link.get("link_reason"),
                now,
            ),
        )


def market_cluster_link(conn: sqlite3.Connection, market_id: str) -> Dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT mel.*, ec.cluster_key, ec.topic, ec.title, ec.status AS cluster_status
        FROM market_event_links mel
        JOIN event_clusters ec ON ec.id = mel.event_cluster_id
        WHERE mel.market_id = ?
        ORDER BY mel.link_confidence DESC, mel.id ASC
        LIMIT 1
        """,
        (market_id,),
    ).fetchone()
    return row_to_dict(row)


def upsert_research_memo(conn: sqlite3.Connection, memo: Mapping[str, Any]) -> Dict[str, Any]:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO research_memos (
          market_id, event_cluster_id, topic, source_bundle_hash, thesis,
          supporting_evidence_json, counter_evidence_json, uncertainty_notes,
          generated_by, memo_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market_id, source_bundle_hash) DO UPDATE SET
          event_cluster_id=excluded.event_cluster_id,
          topic=excluded.topic,
          thesis=excluded.thesis,
          supporting_evidence_json=excluded.supporting_evidence_json,
          counter_evidence_json=excluded.counter_evidence_json,
          uncertainty_notes=excluded.uncertainty_notes,
          generated_by=excluded.generated_by,
          memo_json=excluded.memo_json
        """,
        (
            memo["market_id"],
            memo.get("event_cluster_id"),
            memo["topic"],
            memo["source_bundle_hash"],
            memo["thesis"],
            json.dumps(memo.get("supporting_evidence", []), sort_keys=False),
            json.dumps(memo.get("counter_evidence", []), sort_keys=False),
            memo.get("uncertainty_notes", ""),
            memo["generated_by"],
            json.dumps(dict(memo), sort_keys=False),
            now,
        ),
    )
    row = conn.execute(
        """
        SELECT *
        FROM research_memos
        WHERE market_id = ? AND source_bundle_hash = ?
        """,
        (memo["market_id"], memo["source_bundle_hash"]),
    ).fetchone()
    item = row_to_dict(row) or {}
    if item:
        item["supporting_evidence_json"] = _json_loads_if_present(item.get("supporting_evidence_json"))
        item["counter_evidence_json"] = _json_loads_if_present(item.get("counter_evidence_json"))
        item["memo_json"] = _json_loads_if_present(item.get("memo_json"))
    return item


def latest_research_memo(conn: sqlite3.Connection, market_id: str) -> Dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM research_memos WHERE market_id = ? ORDER BY id DESC LIMIT 1",
        (market_id,),
    ).fetchone()
    item = row_to_dict(row)
    if item:
        item["supporting_evidence_json"] = _json_loads_if_present(item.get("supporting_evidence_json"))
        item["counter_evidence_json"] = _json_loads_if_present(item.get("counter_evidence_json"))
        item["memo_json"] = _json_loads_if_present(item.get("memo_json"))
    return item


def upsert_proposal(
    conn: sqlite3.Connection,
    proposal: Mapping[str, Any],
    *,
    decision_engine: str,
    status: str,
    context_payload: Mapping[str, Any],
    strategy_name: str | None = None,
    topic: str | None = None,
    event_cluster_id: int | None = None,
    source_memo_id: int | None = None,
    authorization_status: str = "none",
    supervisor_decision: str | None = None,
    priority_score: float | None = None,
    proposal_kind: str = "entry",
    target_position_id: int | None = None,
    approval_ttl_seconds: int | None = None,
    order_live_ttl_seconds: int | None = None,
    alpha_signal_id: str | None = None,
    alpha_fair_probability: float | None = None,
    alpha_market_probability: float | None = None,
    alpha_gross_edge_bps: float | None = None,
    alpha_net_edge_bps: float | None = None,
    alpha_model_version: str | None = None,
    alpha_mapping_confidence: float | None = None,
    llm_meta: Mapping[str, Any] | None = None,
    conviction_tier: str | None = None,
    catalyst_clarity: str | None = None,
    downside_risk: str | None = None,
    asymmetric_target_multiplier: float | None = None,
    thesis_catalyst_deadline: str | None = None,
) -> Dict[str, Any]:
    normalized = normalize_proposal(proposal)
    proposal_id = proposal_id_for(normalized)
    now = utc_now_iso()
    llm_meta_json = json.dumps(dict(llm_meta), sort_keys=False) if llm_meta else None
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
        "strategy_name": strategy_name,
        "topic": topic,
        "event_cluster_id": event_cluster_id,
        "source_memo_id": source_memo_id,
        "authorization_status": authorization_status,
        "supervisor_decision": supervisor_decision,
        "priority_score": priority_score,
        "proposal_kind": proposal_kind,
        "target_position_id": target_position_id,
        "approval_ttl_seconds": approval_ttl_seconds,
        "order_live_ttl_seconds": order_live_ttl_seconds,
        "alpha_signal_id": alpha_signal_id,
        "alpha_fair_probability": alpha_fair_probability,
        "alpha_market_probability": alpha_market_probability,
        "alpha_gross_edge_bps": alpha_gross_edge_bps,
        "alpha_net_edge_bps": alpha_net_edge_bps,
        "alpha_model_version": alpha_model_version,
        "alpha_mapping_confidence": alpha_mapping_confidence,
        "llm_meta_json": llm_meta_json,
        "conviction_tier": conviction_tier,
        "catalyst_clarity": catalyst_clarity,
        "downside_risk": downside_risk,
        "asymmetric_target_multiplier": asymmetric_target_multiplier,
        "thesis_catalyst_deadline": thesis_catalyst_deadline,
        "proposal_json": json.dumps(normalized, sort_keys=False),
        "context_payload_json": json.dumps(context_payload, sort_keys=False),
        "created_at": now,
        "updated_at": now,
    }
    conn.execute(
        """
        INSERT INTO proposals (
          proposal_id, market_id, outcome, confidence_score, recommended_size_usdc, reasoning,
          decision_engine, status, max_slippage_bps, strategy_name, topic, event_cluster_id,
          source_memo_id, authorization_status, supervisor_decision, priority_score,
          proposal_kind, target_position_id, approval_ttl_seconds, order_live_ttl_seconds,
          alpha_signal_id, alpha_fair_probability, alpha_market_probability,
          alpha_gross_edge_bps, alpha_net_edge_bps, alpha_model_version, alpha_mapping_confidence,
          llm_meta_json, conviction_tier, catalyst_clarity, downside_risk,
          asymmetric_target_multiplier, thesis_catalyst_deadline,
          proposal_json, context_payload_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(proposal_id) DO UPDATE SET
          market_id=excluded.market_id,
          outcome=excluded.outcome,
          confidence_score=excluded.confidence_score,
          recommended_size_usdc=excluded.recommended_size_usdc,
          reasoning=excluded.reasoning,
          decision_engine=excluded.decision_engine,
          status=CASE WHEN proposals.status = 'proposed' THEN excluded.status ELSE proposals.status END,
          max_slippage_bps=excluded.max_slippage_bps,
          strategy_name=excluded.strategy_name,
          topic=excluded.topic,
          event_cluster_id=excluded.event_cluster_id,
          source_memo_id=excluded.source_memo_id,
          authorization_status=excluded.authorization_status,
          supervisor_decision=excluded.supervisor_decision,
          priority_score=excluded.priority_score,
          proposal_kind=excluded.proposal_kind,
          target_position_id=excluded.target_position_id,
          approval_ttl_seconds=excluded.approval_ttl_seconds,
          order_live_ttl_seconds=excluded.order_live_ttl_seconds,
          alpha_signal_id=excluded.alpha_signal_id,
          alpha_fair_probability=excluded.alpha_fair_probability,
          alpha_market_probability=excluded.alpha_market_probability,
          alpha_gross_edge_bps=excluded.alpha_gross_edge_bps,
          alpha_net_edge_bps=excluded.alpha_net_edge_bps,
          alpha_model_version=excluded.alpha_model_version,
          alpha_mapping_confidence=excluded.alpha_mapping_confidence,
          llm_meta_json=COALESCE(excluded.llm_meta_json, proposals.llm_meta_json),
          conviction_tier=COALESCE(excluded.conviction_tier, proposals.conviction_tier),
          catalyst_clarity=COALESCE(excluded.catalyst_clarity, proposals.catalyst_clarity),
          downside_risk=COALESCE(excluded.downside_risk, proposals.downside_risk),
          asymmetric_target_multiplier=COALESCE(excluded.asymmetric_target_multiplier, proposals.asymmetric_target_multiplier),
          thesis_catalyst_deadline=COALESCE(excluded.thesis_catalyst_deadline, proposals.thesis_catalyst_deadline),
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


def proposal_contexts(conn: sqlite3.Connection, proposal_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, source_type, source_id, title, published_at, url, raw_text, display_text,
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
        item["normalized_payload_json"] = _json_loads_if_present(item.get("normalized_payload_json"))
    return items


def recent_proposals_for_market(
    conn: sqlite3.Connection,
    market_id: str,
    *,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Recent proposals for a market with fill data (for LLM self-calibration).

    Joins the most recent execution per proposal so PolyProposer can see
    whether prior calls filled and at what price.
    """
    rows = conn.execute(
        """
        SELECT p.proposal_id, p.outcome, p.confidence_score, p.status,
               p.recommended_size_usdc, p.decision_engine, p.created_at,
               e.avg_fill_price AS fill_price, e.filled_size_usdc AS fill_size_usdc,
               e.status AS execution_status
        FROM proposals p
        LEFT JOIN (
            SELECT proposal_id, avg_fill_price, filled_size_usdc, status,
                   ROW_NUMBER() OVER (PARTITION BY proposal_id ORDER BY id DESC) AS rn
            FROM executions
        ) e ON e.proposal_id = p.proposal_id AND e.rn = 1
        WHERE p.market_id = ?
        ORDER BY p.created_at DESC
        LIMIT ?
        """,
        (market_id, int(limit)),
    ).fetchall()
    return rows_to_dicts(rows)


def proposal_record(conn: sqlite3.Connection, proposal_id: str) -> Dict[str, Any] | None:
    row = conn.execute("SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
    item = row_to_dict(row)
    if item is None:
        return None
    item["proposal_json"] = _json_loads_if_present(item.get("proposal_json"))
    item["context_payload_json"] = _json_loads_if_present(item.get("context_payload_json"))
    item["market"] = market_snapshot(conn, item["market_id"])
    item["contexts"] = proposal_contexts(conn, proposal_id)
    item["cluster"] = None
    if item.get("event_cluster_id"):
        cluster_row = conn.execute("SELECT * FROM event_clusters WHERE id = ?", (item["event_cluster_id"],)).fetchone()
        item["cluster"] = row_to_dict(cluster_row)
    memo_row = None
    if item.get("source_memo_id"):
        memo_row = conn.execute("SELECT * FROM research_memos WHERE id = ?", (item["source_memo_id"],)).fetchone()
    item["research_memo"] = row_to_dict(memo_row)
    if item["research_memo"]:
        item["research_memo"]["memo_json"] = _json_loads_if_present(item["research_memo"].get("memo_json"))
    approval = conn.execute("SELECT * FROM approvals WHERE proposal_id = ?", (proposal_id,)).fetchone()
    item["approval"] = row_to_dict(approval)
    if item["approval"] and item["approval"].get("raw_callback_json"):
        item["approval"]["raw_callback_json"] = _json_loads_if_present(item["approval"]["raw_callback_json"])
    return item


def list_proposals(conn: sqlite3.Connection, proposal_ids: Iterable[str]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for proposal_id in proposal_ids:
        record = proposal_record(conn, proposal_id)
        if record is not None:
            items.append(record)
    return items


def list_proposals_by_status(conn: sqlite3.Connection, statuses: Sequence[str], *, limit: int | None = None) -> List[Dict[str, Any]]:
    if not statuses:
        return []
    placeholders = ", ".join("?" for _ in statuses)
    sql = f"SELECT proposal_id FROM proposals WHERE status IN ({placeholders}) ORDER BY updated_at ASC, proposal_id ASC"
    params: list[Any] = list(statuses)
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [proposal_record(conn, str(row["proposal_id"])) for row in rows if proposal_record(conn, str(row["proposal_id"])) is not None]


LEGAL_PROPOSAL_TRANSITIONS = {
    "proposed":                    {"risk_blocked", "pending_approval"},
    "risk_blocked":                {"proposed"},
    "pending_approval":            {"approved", "rejected", "expired", "authorized_for_execution"},
    "approved":                    {"authorized_for_execution", "executed", "failed"},
    "authorized_for_execution":    {"executed", "failed"},
    "executed":                    set(),
    "failed":                      {"proposed"},
    "expired":                     set(),
    "rejected":                    set(),
    "cancelled":                   set(),
}


def update_proposal_status(conn: sqlite3.Connection, proposal_id: str, status: str) -> None:
    current = conn.execute("SELECT status FROM proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
    if current:
        current_status = current["status"]
        legal = LEGAL_PROPOSAL_TRANSITIONS.get(current_status, set())
        if status != current_status and status not in legal:
            import sys
            print(
                f"[db] WARNING: illegal proposal transition {current_status} -> {status} for {proposal_id}",
                file=sys.stderr,
            )
    conn.execute(
        "UPDATE proposals SET status = ?, updated_at = ? WHERE proposal_id = ?",
        (status, utc_now_iso(), proposal_id),
    )


def update_proposal_workflow_fields(
    conn: sqlite3.Connection,
    proposal_id: str,
    *,
    strategy_name: str | None = None,
    topic: str | None = None,
    event_cluster_id: int | None = None,
    source_memo_id: int | None = None,
    authorization_status: str | None = None,
    supervisor_decision: str | None = None,
    priority_score: float | None = None,
    status: str | None = None,
    approval_requested_at: str | None = None,
    approval_expires_at: str | None = None,
    telegram_message_id: str | None = None,
    telegram_chat_id: str | None = None,
) -> None:
    current = proposal_record(conn, proposal_id)
    if current is None:
        raise KeyError(f"Unknown proposal_id: {proposal_id}")
    conn.execute(
        """
        UPDATE proposals
        SET strategy_name = ?,
            topic = ?,
            event_cluster_id = ?,
            source_memo_id = ?,
            authorization_status = ?,
            supervisor_decision = ?,
            priority_score = ?,
            status = ?,
            approval_requested_at = ?,
            approval_expires_at = ?,
            telegram_message_id = ?,
            telegram_chat_id = ?,
            updated_at = ?
        WHERE proposal_id = ?
        """,
        (
            strategy_name if strategy_name is not None else current.get("strategy_name"),
            topic if topic is not None else current.get("topic"),
            event_cluster_id if event_cluster_id is not None else current.get("event_cluster_id"),
            source_memo_id if source_memo_id is not None else current.get("source_memo_id"),
            authorization_status if authorization_status is not None else current.get("authorization_status"),
            supervisor_decision if supervisor_decision is not None else current.get("supervisor_decision"),
            priority_score if priority_score is not None else current.get("priority_score"),
            status if status is not None else current.get("status"),
            approval_requested_at if approval_requested_at is not None else current.get("approval_requested_at"),
            approval_expires_at if approval_expires_at is not None else current.get("approval_expires_at"),
            telegram_message_id if telegram_message_id is not None else current.get("telegram_message_id"),
            telegram_chat_id if telegram_chat_id is not None else current.get("telegram_chat_id"),
            utc_now_iso(),
            proposal_id,
        ),
    )


def record_approval(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    decision: str,
    decided_at: str,
    callback_query_id: str,
    raw_callback_json: Mapping[str, Any],
    telegram_user_id: str | None = None,
    telegram_username: str | None = None,
    telegram_message_id: str | None = None,
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
                    "authorization_status": record.get("authorization_status"),
                }
            )
    return results


def create_strategy_authorization(conn: sqlite3.Connection, authorization: Mapping[str, Any]) -> Dict[str, Any]:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO strategy_authorizations (
          strategy_name, scope_topic, scope_market_type, scope_event_cluster_id,
          max_order_usdc, max_daily_gross_usdc, max_open_positions, max_daily_loss_usdc,
          max_slippage_bps, allow_auto_execute, requires_human_if_above_usdc,
          valid_from, valid_until, status, created_by, created_at, revoked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            authorization["strategy_name"],
            authorization.get("scope_topic"),
            authorization.get("scope_market_type"),
            authorization.get("scope_event_cluster_id"),
            float(authorization["max_order_usdc"]),
            float(authorization["max_daily_gross_usdc"]),
            int(authorization["max_open_positions"]),
            float(authorization["max_daily_loss_usdc"]),
            int(authorization["max_slippage_bps"]),
            1 if authorization.get("allow_auto_execute") else 0,
            authorization.get("requires_human_if_above_usdc"),
            authorization["valid_from"],
            authorization["valid_until"],
            authorization.get("status", "active"),
            authorization.get("created_by"),
            now,
            authorization.get("revoked_at"),
        ),
    )
    row = conn.execute("SELECT * FROM strategy_authorizations WHERE id = last_insert_rowid()").fetchone()
    return row_to_dict(row) or {}


def list_strategy_authorizations(conn: sqlite3.Connection, *, status: str | None = None) -> List[Dict[str, Any]]:
    if status:
        rows = conn.execute(
            "SELECT * FROM strategy_authorizations WHERE status = ? ORDER BY created_at DESC, id DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM strategy_authorizations ORDER BY created_at DESC, id DESC").fetchall()
    return rows_to_dicts(rows)


def record_shadow_execution(conn: sqlite3.Connection, payload: Mapping[str, Any]) -> Dict[str, Any]:
    conn.execute(
        """
        INSERT INTO shadow_executions (
          proposal_id, simulated_fill_price, simulated_size, simulated_notional,
          simulated_status, context_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["proposal_id"],
            payload.get("simulated_fill_price"),
            payload.get("simulated_size"),
            payload.get("simulated_notional"),
            payload["simulated_status"],
            json.dumps(payload.get("context_json", {}), sort_keys=False),
            payload.get("created_at", utc_now_iso()),
        ),
    )
    row = conn.execute("SELECT * FROM shadow_executions WHERE id = last_insert_rowid()").fetchone()
    item = row_to_dict(row) or {}
    if item.get("context_json"):
        item["context_json"] = _json_loads_if_present(item["context_json"])
    return item


def list_shadow_executions(conn: sqlite3.Connection, proposal_id: str | None = None) -> List[Dict[str, Any]]:
    if proposal_id:
        rows = conn.execute(
            "SELECT * FROM shadow_executions WHERE proposal_id = ? ORDER BY id DESC",
            (proposal_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM shadow_executions ORDER BY id DESC").fetchall()
    items = rows_to_dicts(rows)
    for item in items:
        item["context_json"] = _json_loads_if_present(item.get("context_json"))
    return items


def _sync_position_for_execution(conn: sqlite3.Connection, execution_id: int) -> None:
    try:
        from .services.position_manager import sync_position_for_execution
    except Exception:
        return
    sync_position_for_execution(conn, execution_id)


def record_execution(conn: sqlite3.Connection, execution: Mapping[str, Any]) -> Dict[str, Any]:
    payload = dict(execution)
    conn.execute(
        """
        INSERT INTO executions (
          proposal_id, mode, client_order_id, order_intent_json, requested_price, requested_size_usdc,
          max_slippage_bps, observed_worst_price, slippage_check_status, status, filled_size_usdc,
          avg_fill_price, txhash_or_order_id, slippage_bps, error_message, error_category,
          submitted_at, filled_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            payload.get("error_category"),
            payload.get("submitted_at"),
            payload.get("filled_at"),
            payload["created_at"],
            payload["updated_at"],
        ),
    )
    if payload["status"] in {"filled", "submitted", "live"}:
        update_proposal_status(conn, payload["proposal_id"], "executed")
    elif payload["status"] == "failed":
        current = conn.execute("SELECT status FROM proposals WHERE proposal_id = ?", (payload["proposal_id"],)).fetchone()
        current_status = current["status"] if current else None
        if payload.get("error_message") not in {"proposal_not_approved"} and current_status not in {"executed"}:
            update_proposal_status(conn, payload["proposal_id"], "failed")
    row = conn.execute("SELECT * FROM executions WHERE rowid = last_insert_rowid()").fetchone()
    result = row_to_dict(row) or {}
    if result.get("order_intent_json"):
        result["order_intent_json"] = _json_loads_if_present(result["order_intent_json"])
    # Position sync is decoupled: commit execution first, then attempt sync.
    # If sync fails, the reconciler will heal the orphaned execution.
    if result.get("id") and result.get("status") in {"filled", "submitted", "live"}:
        try:
            conn.commit()
        except Exception:
            pass
        try:
            _sync_position_for_execution(conn, int(result["id"]))
        except Exception:
            import sys
            print(f"[db] WARNING: position sync failed for execution {result['id']}, reconciler will heal", file=sys.stderr)
    # Record execution event for audit trail
    if result.get("id"):
        try:
            record_execution_event(conn, {
                "execution_id": int(result["id"]),
                "from_status": None,
                "to_status": result.get("status", "unknown"),
                "trigger": "record_execution",
                "payload_json": {"mode": payload.get("mode"), "error_category": payload.get("error_category")},
            })
        except Exception:
            pass
    return result


def record_execution_event(conn: sqlite3.Connection, payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Append-only audit trail for execution state transitions."""
    conn.execute(
        """
        INSERT INTO execution_events (execution_id, from_status, to_status, trigger, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            payload["execution_id"],
            payload.get("from_status"),
            payload["to_status"],
            payload.get("trigger"),
            json.dumps(payload.get("payload_json", {}), sort_keys=False),
            payload.get("created_at", utc_now_iso()),
        ),
    )
    row = conn.execute("SELECT * FROM execution_events WHERE id = last_insert_rowid()").fetchone()
    item = row_to_dict(row) or {}
    if item.get("payload_json"):
        item["payload_json"] = _json_loads_if_present(item["payload_json"])
    return item


def update_execution(conn: sqlite3.Connection, execution_id: int, fields: Mapping[str, Any]) -> Dict[str, Any]:
    if not fields:
        row = conn.execute("SELECT * FROM executions WHERE id = ?", (execution_id,)).fetchone()
        item = row_to_dict(row) or {}
        if item.get("order_intent_json"):
            item["order_intent_json"] = _json_loads_if_present(item["order_intent_json"])
        return item
    assignments: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        assignments.append(f"{key} = ?")
        if key == "order_intent_json" and not isinstance(value, str):
            values.append(json.dumps(value, sort_keys=False))
        else:
            values.append(value)
    assignments.append("updated_at = ?")
    values.append(utc_now_iso())
    values.append(execution_id)
    conn.execute(f"UPDATE executions SET {', '.join(assignments)} WHERE id = ?", tuple(values))
    row = conn.execute("SELECT * FROM executions WHERE id = ?", (execution_id,)).fetchone()
    item = row_to_dict(row) or {}
    if item.get("order_intent_json"):
        item["order_intent_json"] = _json_loads_if_present(item["order_intent_json"])
    if item.get("status") in {"filled", "submitted", "live"}:
        _sync_position_for_execution(conn, execution_id)
    return item


def has_active_execution(conn: sqlite3.Connection, proposal_id: str) -> bool:
    """Return True if the proposal already has a submitted/live/filled execution."""
    row = conn.execute(
        "SELECT COUNT(*) FROM executions WHERE proposal_id = ? AND status IN ('submitted', 'live', 'filled')",
        (proposal_id,),
    ).fetchone()
    return int(row[0]) > 0


def has_active_market_outcome_exposure(
    conn: sqlite3.Connection, market_id: str, outcome: str, exclude_proposal_id: str | None = None
) -> bool:
    """Return True if another entry proposal for the same market_id+outcome has an active execution."""
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM executions e
        JOIN proposals p ON p.proposal_id = e.proposal_id
        LEFT JOIN market_resolutions mr ON mr.market_id = p.market_id
        WHERE p.proposal_kind = 'entry'
          AND p.market_id = ?
          AND p.outcome = ?
          AND e.status IN ('submitted', 'live', 'filled')
          AND mr.market_id IS NULL
          AND p.proposal_id != COALESCE(?, '')
        """,
        (market_id, outcome, exclude_proposal_id),
    ).fetchone()
    return int(row[0]) > 0


def latest_execution(conn: sqlite3.Connection, proposal_id: str, mode: str | None = None) -> Dict[str, Any] | None:
    if mode:
        row = conn.execute(
            "SELECT * FROM executions WHERE proposal_id = ? AND mode = ? ORDER BY id DESC LIMIT 1",
            (proposal_id, mode),
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM executions WHERE proposal_id = ? ORDER BY id DESC LIMIT 1", (proposal_id,)).fetchone()
    item = row_to_dict(row)
    if item and item.get("order_intent_json"):
        item["order_intent_json"] = _json_loads_if_present(item["order_intent_json"])
    return item


def list_executions(
    conn: sqlite3.Connection,
    *,
    statuses: Sequence[str] | None = None,
    mode: str | None = None,
) -> List[Dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)
    if mode:
        clauses.append("mode = ?")
        params.append(mode)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM executions {where} ORDER BY id DESC", tuple(params)).fetchall()
    items = rows_to_dicts(rows)
    for item in items:
        item["order_intent_json"] = _json_loads_if_present(item.get("order_intent_json"))
    return items


def record_position(conn: sqlite3.Connection, payload: Mapping[str, Any]) -> Dict[str, Any]:
    conn.execute(
        """
        INSERT INTO positions (
          proposal_id, execution_id, market_id, event_cluster_id, outcome, entry_price,
          size_usdc, filled_qty, status, entry_time, last_mark_price, unrealized_pnl,
          realized_pnl, strategy_name, is_shadow, mode, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(execution_id, is_shadow) DO UPDATE SET
          market_id=excluded.market_id,
          event_cluster_id=excluded.event_cluster_id,
          outcome=excluded.outcome,
          entry_price=excluded.entry_price,
          size_usdc=excluded.size_usdc,
          filled_qty=excluded.filled_qty,
          status=excluded.status,
          entry_time=excluded.entry_time,
          last_mark_price=excluded.last_mark_price,
          unrealized_pnl=excluded.unrealized_pnl,
          realized_pnl=excluded.realized_pnl,
          strategy_name=excluded.strategy_name,
          mode=excluded.mode,
          updated_at=excluded.updated_at
        """,
        (
            payload["proposal_id"],
            payload["execution_id"],
            payload["market_id"],
            payload.get("event_cluster_id"),
            payload["outcome"],
            payload.get("entry_price"),
            payload["size_usdc"],
            payload.get("filled_qty"),
            payload["status"],
            payload["entry_time"],
            payload.get("last_mark_price"),
            payload.get("unrealized_pnl"),
            payload.get("realized_pnl"),
            payload.get("strategy_name"),
            1 if payload.get("is_shadow") else 0,
            payload.get("mode", "real"),
            payload["created_at"],
            payload["updated_at"],
        ),
    )
    row = conn.execute(
        "SELECT * FROM positions WHERE execution_id = ? AND is_shadow = ?",
        (payload["execution_id"], 1 if payload.get("is_shadow") else 0),
    ).fetchone()
    return row_to_dict(row) or {}


def record_position_event(conn: sqlite3.Connection, payload: Mapping[str, Any]) -> Dict[str, Any]:
    conn.execute(
        """
        INSERT INTO position_events (position_id, event_type, payload_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            payload["position_id"],
            payload["event_type"],
            json.dumps(payload.get("payload_json", {}), sort_keys=False),
            payload.get("created_at", utc_now_iso()),
        ),
    )
    row = conn.execute("SELECT * FROM position_events WHERE id = last_insert_rowid()").fetchone()
    item = row_to_dict(row) or {}
    if item.get("payload_json"):
        item["payload_json"] = _json_loads_if_present(item["payload_json"])
    return item


def list_positions(conn: sqlite3.Connection, *, statuses: Sequence[str] | None = None, is_shadow: bool | None = None) -> List[Dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)
    if is_shadow is not None:
        clauses.append("is_shadow = ?")
        params.append(1 if is_shadow else 0)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM positions {where} ORDER BY id DESC", tuple(params)).fetchall()
    return rows_to_dicts(rows)


def position_for_execution(conn: sqlite3.Connection, execution_id: int, *, is_shadow: bool = False) -> Dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM positions WHERE execution_id = ? AND is_shadow = ?",
        (execution_id, 1 if is_shadow else 0),
    ).fetchone()
    return row_to_dict(row)


def record_order_reconciliation(conn: sqlite3.Connection, payload: Mapping[str, Any]) -> Dict[str, Any]:
    conn.execute(
        """
        INSERT INTO order_reconciliations (
          execution_id, external_order_id, observed_status, observed_fill_qty,
          observed_fill_price, reconciliation_result, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["execution_id"],
            payload.get("external_order_id"),
            payload.get("observed_status"),
            payload.get("observed_fill_qty"),
            payload.get("observed_fill_price"),
            payload["reconciliation_result"],
            json.dumps(payload.get("payload_json", {}), sort_keys=False),
            payload.get("created_at", utc_now_iso()),
        ),
    )
    row = conn.execute("SELECT * FROM order_reconciliations WHERE id = last_insert_rowid()").fetchone()
    item = row_to_dict(row) or {}
    if item.get("payload_json"):
        item["payload_json"] = _json_loads_if_present(item["payload_json"])
    return item


def set_kill_switch(
    conn: sqlite3.Connection,
    *,
    scope_type: str,
    scope_key: str,
    reason: str,
    created_by: str | None = None,
) -> Dict[str, Any]:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO kill_switches (
          scope_type, scope_key, status, reason, created_by, created_at, released_at
        ) VALUES (?, ?, 'active', ?, ?, ?, NULL)
        """,
        (scope_type, scope_key, reason, created_by, now),
    )
    row = conn.execute("SELECT * FROM kill_switches WHERE id = last_insert_rowid()").fetchone()
    return row_to_dict(row) or {}


def release_kill_switch(conn: sqlite3.Connection, kill_id: int) -> Dict[str, Any] | None:
    conn.execute(
        "UPDATE kill_switches SET status = 'released', released_at = ? WHERE id = ?",
        (utc_now_iso(), kill_id),
    )
    row = conn.execute("SELECT * FROM kill_switches WHERE id = ?", (kill_id,)).fetchone()
    return row_to_dict(row)


def list_kill_switches(conn: sqlite3.Connection, *, active_only: bool = False) -> List[Dict[str, Any]]:
    if active_only:
        rows = conn.execute("SELECT * FROM kill_switches WHERE status = 'active' ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM kill_switches ORDER BY id DESC").fetchall()
    return rows_to_dicts(rows)


def record_exit_recommendation(conn: sqlite3.Connection, payload: Mapping[str, Any]) -> Dict[str, Any]:
    conn.execute(
        """
        INSERT INTO exit_recommendations (
          position_id, recommendation, target_reduce_pct, reasoning,
          confidence_score, created_at, action_status, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["position_id"],
            payload["recommendation"],
            payload.get("target_reduce_pct"),
            payload["reasoning"],
            payload["confidence_score"],
            payload.get("created_at", utc_now_iso()),
            payload.get("action_status", "generated"),
            json.dumps(payload.get("payload_json", {}), sort_keys=False),
        ),
    )
    row = conn.execute("SELECT * FROM exit_recommendations WHERE id = last_insert_rowid()").fetchone()
    item = row_to_dict(row) or {}
    if item.get("payload_json"):
        item["payload_json"] = _json_loads_if_present(item["payload_json"])
    return item


def record_agent_review(conn: sqlite3.Connection, payload: Mapping[str, Any]) -> Dict[str, Any]:
    conn.execute(
        """
        INSERT INTO agent_reviews (
          position_id, proposal_id, event_cluster_id, review_type, summary,
          what_worked, what_failed, failure_bucket, next_action, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.get("position_id"),
            payload.get("proposal_id"),
            payload.get("event_cluster_id"),
            payload["review_type"],
            payload["summary"],
            json.dumps(payload.get("what_worked", []), sort_keys=False),
            json.dumps(payload.get("what_failed", []), sort_keys=False),
            payload["failure_bucket"],
            payload["next_action"],
            json.dumps(payload.get("payload_json", {}), sort_keys=False),
            payload.get("created_at", utc_now_iso()),
        ),
    )
    row = conn.execute("SELECT * FROM agent_reviews WHERE id = last_insert_rowid()").fetchone()
    item = row_to_dict(row) or {}
    if item.get("payload_json"):
        item["payload_json"] = _json_loads_if_present(item["payload_json"])
    if item.get("what_worked"):
        item["what_worked"] = _json_loads_if_present(item["what_worked"])
    if item.get("what_failed"):
        item["what_failed"] = _json_loads_if_present(item["what_failed"])
    return item


def list_expired_pending_proposals(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Return pending_approval proposals whose approval deadline has passed."""
    now = utc_now_iso()
    rows = conn.execute(
        """
        SELECT proposal_id FROM proposals
        WHERE status = 'pending_approval'
          AND approval_expires_at IS NOT NULL
          AND approval_expires_at < ?
        ORDER BY approval_expires_at ASC
        """,
        (now,),
    ).fetchall()
    return [proposal_record(conn, str(row["proposal_id"])) for row in rows if proposal_record(conn, str(row["proposal_id"])) is not None]


def record_heartbeat(
    conn: sqlite3.Connection,
    loop_name: str,
    started_at: str,
    finished_at: str | None,
    items_processed: int,
    error_message: str | None,
    payload: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    conn.execute(
        """
        INSERT INTO autopilot_heartbeats (
          loop_name, started_at, finished_at, items_processed, error_message, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            loop_name,
            started_at,
            finished_at,
            items_processed,
            error_message,
            json.dumps(payload or {}, sort_keys=False),
        ),
    )
    row = conn.execute("SELECT * FROM autopilot_heartbeats WHERE id = last_insert_rowid()").fetchone()
    item = row_to_dict(row) or {}
    if item.get("payload_json"):
        item["payload_json"] = _json_loads_if_present(item["payload_json"])
    return item


def list_reviews(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM agent_reviews ORDER BY id DESC").fetchall()
    items = rows_to_dicts(rows)
    for item in items:
        item["payload_json"] = _json_loads_if_present(item.get("payload_json"))
        item["what_worked"] = _json_loads_if_present(item.get("what_worked"))
        item["what_failed"] = _json_loads_if_present(item.get("what_failed"))
    return items


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


def market_resolution(conn: sqlite3.Connection, market_id: str) -> Dict[str, Any] | None:
    row = conn.execute("SELECT * FROM market_resolutions WHERE market_id = ?", (market_id,)).fetchone()
    item = row_to_dict(row)
    if item and item.get("source_payload_json"):
        item["source_payload_json"] = _json_loads_if_present(item["source_payload_json"])
    return item
