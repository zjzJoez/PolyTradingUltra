from __future__ import annotations

from typing import Any, Dict, Mapping

from ..common import get_env_float, get_env_int, parse_iso8601, utc_now_iso
from ..strategy.conviction import (
    CONCURRENT_CAPS,
    portfolio_exposure_cap,
    tier_rank,
)


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


def _portfolio_daily_gross(conn) -> float:
    """Total non-failed execution spend today across all strategies."""
    today = parse_iso8601(utc_now_iso()).date().isoformat()
    row = conn.execute(
        """
        SELECT COALESCE(SUM(e.requested_size_usdc), 0)
        FROM executions e
        WHERE substr(e.created_at, 1, 10) = ? AND e.status NOT IN ('failed')
        """,
        (today,),
    ).fetchone()
    return float(row[0] if row else 0.0)


def _total_open_positions(conn) -> int:
    """Count all open positions across strategies."""
    row = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE status IN ('open', 'open_requested', 'partially_filled')"
    ).fetchone()
    return int(row[0] if row else 0)


def _total_open_exposure_usdc(conn) -> float:
    """Sum in-flight gross exposure across submitted/live/filled executions for
    positions that have not yet been resolved. Used for the balance-fraction cap.
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(e.requested_size_usdc), 0)
        FROM executions e
        JOIN proposals p ON p.proposal_id = e.proposal_id
        LEFT JOIN market_resolutions mr ON mr.market_id = p.market_id
        WHERE e.status IN ('submitted', 'live', 'filled')
          AND mr.market_id IS NULL
        """
    ).fetchone()
    return float(row[0] if row else 0.0)


def _tier_concurrent_count(conn, tier: str) -> int:
    if not tier:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM positions pos
        JOIN proposals p ON p.proposal_id = pos.proposal_id
        WHERE pos.status IN ('open', 'open_requested', 'partially_filled')
          AND p.conviction_tier = ?
        """,
        (tier,),
    ).fetchone()
    return int(row[0] if row else 0)


def check_drawdown_breaker(conn) -> Dict[str, Any]:
    """Check portfolio drawdown from peak. Returns breaker status."""
    max_drawdown = get_env_float("POLY_MAX_DRAWDOWN_USDC", 30.0)
    # Sum all realized PnL
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions WHERE status = 'resolved'"
    ).fetchone()
    realized = float(row[0] if row else 0.0)
    # Sum unrealized PnL from open positions
    row2 = conn.execute(
        "SELECT COALESCE(SUM(unrealized_pnl), 0) FROM positions WHERE status IN ('open', 'partially_filled')"
    ).fetchone()
    unrealized = float(row2[0] if row2 else 0.0)
    total_pnl = realized + unrealized
    # Approximate peak: use the max of (realized PnL at any point)
    # Since we don't track historical peaks, use 0 as baseline (initial capital)
    drawdown = max(0.0, -total_pnl) if total_pnl < 0 else 0.0
    breaker_active = drawdown >= max_drawdown
    return {
        "breaker_active": breaker_active,
        "drawdown_usdc": drawdown,
        "max_drawdown_usdc": max_drawdown,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": total_pnl,
    }


def evaluate_portfolio_risk(conn, record: Mapping[str, Any], *, available_balance_usdc: float | None = None) -> Dict[str, Any]:
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
    portfolio_daily_limit = get_env_float("POLY_RISK_MAX_DAILY_GROSS_USDC", 100.0)
    max_open_positions = get_env_int("POLY_RISK_MAX_OPEN_POSITIONS", 10)
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
    projected_portfolio_daily = _portfolio_daily_gross(conn) + float(proposal["recommended_size_usdc"])
    current_open_positions = _total_open_positions(conn)
    reasons: list[str] = []
    # Drawdown circuit breaker
    drawdown = check_drawdown_breaker(conn)
    if drawdown["breaker_active"]:
        reasons.append(f"drawdown_breaker_active[dd={drawdown['drawdown_usdc']:.2f}]")
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
    # Portfolio-level daily gross cap
    if projected_portfolio_daily > portfolio_daily_limit:
        reasons.append("portfolio_daily_gross_limit_exceeded")
    # Portfolio-level open position cap
    if record.get("proposal_kind") != "exit" and current_open_positions >= max_open_positions:
        reasons.append("max_open_positions_reached")

    # Conviction-strategy limits: total open exposure as fraction of balance + per-tier caps.
    balance_cap = portfolio_exposure_cap(available_balance_usdc)
    current_open_exposure = _total_open_exposure_usdc(conn)
    projected_open_exposure = current_open_exposure + float(proposal["recommended_size_usdc"])
    if record.get("proposal_kind") != "exit" and projected_open_exposure > balance_cap:
        reasons.append(
            f"portfolio_open_exposure_limit_exceeded[projected={projected_open_exposure:.2f}>cap={balance_cap:.2f}]"
        )
    tier = record.get("conviction_tier")
    tier_cap_val = CONCURRENT_CAPS.get(tier) if tier else None
    tier_open_count = _tier_concurrent_count(conn, tier) if tier else 0
    if (
        record.get("proposal_kind") != "exit"
        and tier is not None
        and tier_cap_val is not None
        and tier_open_count >= tier_cap_val
    ):
        reasons.append(f"tier_concurrent_cap_reached[tier={tier},cap={tier_cap_val}]")

    return {
        "approved": not reasons,
        "reasons": reasons,
        "limits": {
            "topic_limit_usdc": topic_limit,
            "cluster_limit_usdc": cluster_limit,
            "strategy_daily_gross_limit_usdc": strategy_daily_limit,
            "portfolio_daily_gross_limit_usdc": portfolio_daily_limit,
            "max_open_positions": max_open_positions,
            "portfolio_open_exposure_cap_usdc": balance_cap,
            "tier_concurrent_cap": tier_cap_val,
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
            "portfolio_daily_gross_usdc": projected_portfolio_daily,
            "open_exposure_usdc": projected_open_exposure,
        },
        "drawdown": drawdown,
        "open_positions": current_open_positions,
        "conviction_tier": tier,
        "tier_open_count": tier_open_count,
    }
