from __future__ import annotations

from typing import Any, Dict, Mapping

from ..services.openclaw_adapter import maybe_generate_supervisor_decision


def supervise_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    proposal = record["proposal_json"]
    deterministic = {
        "strategy_name": record.get("strategy_name") or "near_expiry_conviction",
        "topic": record.get("topic") or str(record.get("context_payload_json", {}).get("topic") or proposal["market_id"]),
        "event_cluster_id": record.get("event_cluster_id"),
        "decision": "promote",
        "priority_score": round(float(proposal["confidence_score"]), 4),
        "merge_group": None,
        "notes": "deterministic supervisor pass-through",
    }
    try:
        generated = maybe_generate_supervisor_decision(
            {
                "proposal": proposal,
                "topic": deterministic["topic"],
                "event_cluster_id": deterministic["event_cluster_id"],
                "reasoning": proposal["reasoning"],
            }
        )
    except Exception:
        generated = None
    if isinstance(generated, dict):
        deterministic.update(
            {
                "strategy_name": str(generated.get("strategy_name") or deterministic["strategy_name"]),
                "topic": str(generated.get("topic") or deterministic["topic"]),
                "decision": str(generated.get("decision") or deterministic["decision"]),
                "priority_score": float(generated.get("priority_score") or deterministic["priority_score"]),
                "merge_group": generated.get("merge_group"),
                "notes": str(generated.get("notes") or deterministic["notes"]),
            }
        )
    return deterministic
