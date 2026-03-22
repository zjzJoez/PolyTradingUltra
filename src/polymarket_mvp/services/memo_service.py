from __future__ import annotations

from typing import Any, Dict, List, Mapping

from ..common import sanitize_text, stable_hash
from ..db import latest_research_memo, market_contexts, market_snapshot, upsert_research_memo
from .openclaw_adapter import maybe_generate_research_memo


def _deterministic_memo(market: Mapping[str, Any], contexts: List[Mapping[str, Any]], cluster: Mapping[str, Any] | None) -> Dict[str, Any]:
    top_lines = [sanitize_text(str(item.get("display_text") or "")) for item in contexts if sanitize_text(str(item.get("display_text") or ""))]
    supporting = top_lines[:3]
    counter = [line for line in top_lines[3:5] if "softly" in line.lower() or "unavailable" in line.lower()]
    summary_line = supporting[0] if supporting else f"Limited context for market {market['market_id']}."
    uncertainty = "Context is deterministic and may miss nuance." if contexts else "No external contexts available."
    topic = str((cluster or {}).get("topic") or market.get("question") or market["market_id"])
    return {
        "market_id": str(market["market_id"]),
        "event_cluster_id": (cluster or {}).get("id"),
        "event_cluster_key": (cluster or {}).get("cluster_key"),
        "topic": topic,
        "thesis": summary_line[:400],
        "supporting_evidence": supporting,
        "counter_evidence": counter,
        "uncertainty_notes": uncertainty,
        "generated_by": "deterministic",
    }


def build_research_memo(market: Mapping[str, Any], contexts: List[Mapping[str, Any]], cluster: Mapping[str, Any] | None) -> Dict[str, Any]:
    deterministic = _deterministic_memo(market, contexts, cluster)
    prompt_payload = {
        "market": {
            "market_id": market["market_id"],
            "question": market.get("question"),
            "end_date": market.get("end_date"),
        },
        "cluster": {
            "event_cluster_id": (cluster or {}).get("id"),
            "event_cluster_key": (cluster or {}).get("cluster_key"),
            "topic": (cluster or {}).get("topic"),
        },
        "contexts": [
            {
                "source_type": item.get("source_type"),
                "display_text": item.get("display_text"),
                "published_at": item.get("published_at"),
            }
            for item in contexts[:8]
        ],
    }
    generated = maybe_generate_research_memo(prompt_payload)
    if isinstance(generated, dict):
        memo = dict(deterministic)
        memo.update(
            {
                "thesis": sanitize_text(str(generated.get("thesis") or memo["thesis"]))[:400],
                "supporting_evidence": generated.get("supporting_evidence") or memo["supporting_evidence"],
                "counter_evidence": generated.get("counter_evidence") or memo["counter_evidence"],
                "uncertainty_notes": sanitize_text(str(generated.get("uncertainty_notes") or memo["uncertainty_notes"]))[:300],
                "generated_by": "openclaw",
            }
        )
        return memo
    return deterministic


def build_and_store_memo(conn, market_id: str, *, cluster: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    market = market_snapshot(conn, market_id)
    if market is None:
        raise KeyError(f"Unknown market_id: {market_id}")
    contexts = market_contexts(conn, market_id)
    memo = build_research_memo(market, contexts, cluster)
    source_bundle_hash = stable_hash(
        [
            {
                "source_type": item.get("source_type"),
                "source_id": item.get("source_id"),
                "display_text": item.get("display_text"),
                "published_at": item.get("published_at"),
            }
            for item in contexts
        ]
    )
    memo["source_bundle_hash"] = source_bundle_hash
    return upsert_research_memo(conn, memo)


def memo_for_market(conn, market_id: str) -> Dict[str, Any] | None:
    return latest_research_memo(conn, market_id)
