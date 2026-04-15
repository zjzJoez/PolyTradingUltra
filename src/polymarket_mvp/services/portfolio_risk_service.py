from __future__ import annotations

from typing import Any, Dict, Mapping

from ..common import get_env_float, parse_iso8601, utc_now_iso


def _active_exposure(conn, *, topic: str | None, event_cluster_id: int | None, strategy_name: str | None) -> Dict[str, float]:
    rows = conn.execute(
        """
        SELECT
          p.topic,
          p.event_cluster_id,
          p.strategy_name,
          e.requested_size_usdc,
          p.market_id
        FROM executions e
        JOIN proposals p ON p.proposal_id = e.proposal_id
        LEFT JOIN market_resolutions mr ON mr.market_id = p.market_id
        WHERE e.status IN ('submitted', 'live', 'filled')
          AND mr.market_id IS NULL
        """
    ).fetchall()
    topic_exposure = 0.0
    cluster_exposure = 0.0
    strategy_exposure = 0.0
    for row in rows:
        size = float(row["requested_size_usdc"] or 0.0)
        if topic and row["topic"] == topic:
            topic_exposure += size
        if event_cluster_id is not None and row["event_cluster_id"] == event_cluster_id:
            cluster_exposure += size
        if strategy_name and row["strategy_name"] == strategy_name:
            strategy_exposure += size
    return {
        "topic_exposure_usdc": topic_exposure,
        "cluster_exposure_usdc": cluster_exposure,
        "strategy_exposure_usdc": strategy_exposure,
    }


def _strategy_daily_gross(conn, strategy_name: str | None) -> float:
    if not strategy_name:
        return 0.0
    today = parse_iso8601(utc_now_iso()).date().isoformat()
    row = conn.execute(
        """
        SELECT COALESCE(SUM(e.requested_size_usdc), 0)
        FROM executions e
        JOIN proposals p ON p.proposal_id = e.proposal_id
        WHERE p.strategy_name = ?
          AND substr(e.created_at, 1, 10) = ?
          AND e.status NOT IN ('failed')
        """,
        (strategy_name, today),
    ).fetchone()
    return float(row[0] if row else 0.0)


def _active_market_outcome_exposure(conn, *, market_id: str | None, outcome: str | None) -> float:
    if not market_id or not outcome:
        return 0.0
    row = conn.execute(
        """
        SELECT COALESCE(SUM(e.requested_size_usdc), 0)
        FROM executions e
        JOIN proposals p ON p.proposal_id = e.proposal_id
        LEFT JOIN market_resolutions mr ON mr.market_id = p.market_id
        WHERE p.proposal_kind = 'entry'
          AND p.market_id = ?
          AND p.outcome = ?
          AND e.status IN ('submitted', 'live', 'filled')
          AND mr.market_id IS NULL
        """,
        (market_id, outcome),
    ).fetchone()
    return float(row[0] if row else 0.0)


def _pending_market_outcome_entries(conn, *, market_id: str | None, outcome: str | None, exclude_proposal_id: str | None) -> int:
    if not market_id or not outcome:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM proposals
        WHERE proposal_kind = 'entry'
          AND market_id = ?
          AND outcome = ?
          AND status IN ('proposed', 'pending_approval', 'approved', 'authorized_for_execution')
          AND proposal_id != COALESCE(?, '')
        """,
        (market_id, outcome, exclude_proposal_id),
    ).fetchone()
    return int(row[0] if row else 0)


def evaluate_portfolio_risk(conn, record: Mapping[str, Any]) -> Dict[str, Any]:
    proposal = record["proposal_json"]
    exposures = _active_exposure(
        conn,
        topic=record.get("topic"),
        event_cluster_id=record.get("event_cluster_id"),
        strategy_name=record.get("strategy_name"),
    )
    topic_limit = get_env_float("POLY_RISK_MAX_TOPIC_EXPOSURE_USDC", 25.0)
    cluster_limit = get_env_float("POLY_RISK_MAX_CLUSTER_EXPOSURE_USDC", 25.0)
    strategy_daily_limit = get_env_float("POLY_RISK_MAX_STRATEGY_DAILY_GROSS_USDC", 100.0)
    exact_market_outcome_exposure = _active_market_outcome_exposure(
        conn,
        market_id=record.get("market_id") or proposal.get("market_id"),
        outcome=proposal.get("outcome"),
    )
    pending_market_outcome_entries = _pending_market_outcome_entries(
        conn,
        market_id=record.get("market_id") or proposal.get("market_id"),
        outcome=proposal.get("outcome"),
        exclude_proposal_id=record.get("proposal_id"),
    )
    projected_topic = exposures["topic_exposure_usdc"] + float(proposal["recommended_size_usdc"])
    projected_cluster = exposures["cluster_exposure_usdc"] + float(proposal["recommended_size_usdc"])
    projected_strategy_daily = _strategy_daily_gross(conn, record.get("strategy_name")) + float(proposal["recommended_size_usdc"])
    reasons: list[str] = []
    if record.get("proposal_kind") != "exit" and exact_market_outcome_exposure > 0:
        reasons.append("market_outcome_exposure_exists")
    if record.get("proposal_kind") != "exit" and pending_market_outcome_entries > 0:
        reasons.append("market_outcome_entry_already_pending")
    if record.get("topic") and projected_topic > topic_limit:
        reasons.append("topic_exposure_limit_exceeded")
    if record.get("event_cluster_id") and projected_cluster > cluster_limit:
        reasons.append("cluster_exposure_limit_exceeded")
    if record.get("strategy_name") and projected_strategy_daily > strategy_daily_limit:
        reasons.append("strategy_daily_gross_limit_exceeded")
    return {
        "approved": not reasons,
        "reasons": reasons,
        "limits": {
            "topic_limit_usdc": topic_limit,
            "cluster_limit_usdc": cluster_limit,
            "strategy_daily_gross_limit_usdc": strategy_daily_limit,
        },
        "current_exposures": exposures,
        "market_outcome": {
            "active_exposure_usdc": exact_market_outcome_exposure,
            "pending_entry_count": pending_market_outcome_entries,
        },
        "projected": {
            "topic_exposure_usdc": projected_topic,
            "cluster_exposure_usdc": projected_cluster,
            "strategy_daily_gross_usdc": projected_strategy_daily,
        },
    }
