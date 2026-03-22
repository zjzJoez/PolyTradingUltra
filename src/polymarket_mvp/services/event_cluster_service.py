from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping

from ..common import infer_market_symbol, market_topic, sanitize_text, slugify_text, utc_now_iso
from ..db import replace_market_event_links, upsert_event_cluster


def market_type_for(market: Mapping[str, Any]) -> str:
    outcomes = market.get("outcomes", [])
    if isinstance(outcomes, list) and len(outcomes) == 2:
        return "binary"
    return "multi_outcome"


def _time_bucket(end_date: str | None) -> str:
    if not end_date:
        return "undated"
    normalized = str(end_date).replace(":", "").replace("-", "").replace("T", "-").replace("Z", "")
    return normalized[:13] or "undated"


def _base_cluster_text(market: Mapping[str, Any]) -> str:
    question = sanitize_text(str(market.get("question") or ""))
    symbol = infer_market_symbol(market)
    if symbol and ("UP" in question.upper() or "DOWN" in question.upper()):
        return f"{symbol.lower()}-directional"
    return slugify_text(question or str(market.get("market_id")), fallback="market")


def build_cluster_payload(market: Mapping[str, Any]) -> Dict[str, Any]:
    topic = str(market_topic(market))
    cluster_key = f"{slugify_text(topic, fallback='topic', max_length=32)}-{_base_cluster_text(market)}-{_time_bucket(market.get('end_date'))}"
    title = sanitize_text(str(market.get("question") or topic))
    return {
        "cluster_key": cluster_key[:120],
        "topic": topic,
        "title": title[:200] or cluster_key,
        "description": f"Deterministic cluster for {title or cluster_key}",
        "status": "active",
        "canonical_start_time": None,
        "canonical_end_time": market.get("end_date"),
    }


def cluster_market(conn, market: Mapping[str, Any]) -> Dict[str, Any]:
    cluster = upsert_event_cluster(conn, build_cluster_payload(market))
    replace_market_event_links(
        conn,
        str(market["market_id"]),
        [
            {
                "event_cluster_id": cluster["id"],
                "link_confidence": 1.0,
                "link_reason": "deterministic_topic_slug_match",
                "created_at": utc_now_iso(),
            }
        ],
    )
    return {
        "market_id": str(market["market_id"]),
        "topic": cluster["topic"],
        "market_type": market_type_for(market),
        "cluster": cluster,
    }


def cluster_markets(conn, markets: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [cluster_market(conn, market) for market in markets]
