from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping

from ..common import infer_market_symbol, market_topic, sanitize_text, slugify_text, utc_now_iso
from ..db import replace_market_event_links, upsert_event_cluster


def market_type_for(market: Mapping[str, Any]) -> str:
    outcomes = market.get("outcomes", [])
    if isinstance(outcomes, list) and len(outcomes) == 2:
        return "binary"
    return "multi_outcome"


# --- Market class classification for risk segmentation ---

_CRYPTO_SYMBOLS = {"BTC", "ETH", "SOL", "DOGE", "XRP", "ADA", "TRUMP"}
_SPORTS_KEYWORDS = (
    "nba", "nfl", "mlb", "nhl", "epl", "la liga", "serie a", "bundesliga",
    "champions league", "premier league", "mls", "tennis", "atp", "wta",
    "ufc", "boxing", "f1", "formula", "golf", "pga", "cricket", "rugby",
)
_ESPORTS_KEYWORDS = ("esports", "e-sports", "league of legends", "dota", "csgo", "cs2", "valorant", "overwatch")
_TOTALS_KEYWORDS = ("over", "under", "total", "combined", "o/u")


def classify_market_class(market: Mapping[str, Any]) -> str:
    """Classify a market into one of: crypto_up_down, sports_winner, sports_totals, esports, other."""
    base = market.get("market_json") if isinstance(market.get("market_json"), Mapping) else market
    question = str(base.get("question") or "").lower()
    slug = str(base.get("slug") or "").lower()
    haystack = f"{question} {slug}"
    outcome_names = set()
    for item in base.get("outcomes", []):
        if isinstance(item, Mapping):
            outcome_names.add(str(item.get("name") or "").lower())

    # Crypto directional markets
    from ..common import infer_market_symbol
    symbol = infer_market_symbol(base)
    if symbol and symbol in _CRYPTO_SYMBOLS:
        if "up" in haystack or "down" in haystack or outcome_names == {"up", "down"}:
            return "crypto_up_down"

    # Esports
    if any(kw in haystack for kw in _ESPORTS_KEYWORDS):
        return "esports"

    # Sports totals (over/under)
    is_sports = any(kw in haystack for kw in _SPORTS_KEYWORDS)
    if is_sports:
        if any(kw in haystack for kw in _TOTALS_KEYWORDS):
            return "sports_totals"
        return "sports_winner"

    return "other"


# Per-class risk configuration
MARKET_CLASS_CONFIG: Dict[str, Dict[str, Any]] = {
    "sports_winner":  {"live_enabled": True,  "max_order_usdc": 10, "max_daily_gross": 50, "max_open_positions": 5},
    "sports_totals":  {"live_enabled": True,  "max_order_usdc": 5,  "max_daily_gross": 25, "max_open_positions": 3},
    "esports":        {"live_enabled": True,  "max_order_usdc": 5,  "max_daily_gross": 25, "max_open_positions": 3},
    "crypto_up_down": {"live_enabled": False, "max_order_usdc": 0,  "max_daily_gross": 0,  "max_open_positions": 0},
    "other":          {"live_enabled": False, "max_order_usdc": 0,  "max_daily_gross": 0,  "max_open_positions": 0},
}


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
