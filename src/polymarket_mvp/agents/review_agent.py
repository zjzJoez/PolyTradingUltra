from __future__ import annotations

from typing import Dict

from ..db import market_resolution, record_agent_review
from ..services.openclaw_adapter import maybe_generate_review


def build_review(conn, position: Dict) -> Dict:
    resolution = market_resolution(conn, str(position["market_id"]))
    outcome = str(position["outcome"])
    won = bool(resolution and str(resolution.get("resolved_outcome")) == outcome)
    deterministic = {
        "position_id": position["id"],
        "proposal_id": position["proposal_id"],
        "event_cluster_id": position.get("event_cluster_id"),
        "review_type": "post_resolution",
        "summary": "Trade aligned with resolved outcome." if won else "Trade thesis failed at market resolution.",
        "what_worked": ["Execution and tracking data were captured in SQLite."] if won else [],
        "what_failed": [] if won else ["Resolved outcome did not match the traded outcome."],
        "failure_bucket": "unknown" if won else "signal",
        "next_action": "Keep collecting labeled outcomes for the strategy." if won else "Reduce confidence or size for similar setups until more evidence accumulates.",
        "payload_json": {"resolution": resolution or {}},
    }
    generated = maybe_generate_review(
        {
            "position": position,
            "resolution": resolution,
            "won": won,
        }
    )
    if isinstance(generated, dict):
        deterministic.update(
            {
                "summary": str(generated.get("summary") or deterministic["summary"]),
                "what_worked": generated.get("what_worked") or deterministic["what_worked"],
                "what_failed": generated.get("what_failed") or deterministic["what_failed"],
                "failure_bucket": str(generated.get("failure_bucket") or deterministic["failure_bucket"]),
                "next_action": str(generated.get("next_action") or deterministic["next_action"]),
            }
        )
    return deterministic


def run_review_agent(conn, position: Dict) -> Dict:
    return record_agent_review(conn, build_review(conn, position))
