from __future__ import annotations

import argparse
import json
import os
from datetime import timedelta
from typing import Any, Dict, List, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .common import blocked_market_reason, dump_json, load_repo_env, parse_iso8601, utc_now_iso
from .db import connect_db, init_db, upsert_market_snapshot

load_repo_env()


def gamma_api_base() -> str:
    return os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com").rstrip("/")


def _parse_json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        return json.loads(value)
    return []


def _build_retrying_session() -> requests.Session:
    attempts = max(1, int(os.getenv("POLY_HTTP_RETRY_ATTEMPTS", "4")))
    retries = Retry(
        total=attempts,
        connect=attempts,
        read=attempts,
        status=attempts,
        backoff_factor=float(os.getenv("POLY_HTTP_RETRY_BACKOFF_SECONDS", "1.0")),
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    adapter = HTTPAdapter(max_retries=retries)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def normalize_market(raw: Dict[str, Any], now_utc) -> Dict[str, Any]:
    if not raw.get("endDate"):
        raise ValueError("market missing endDate")
    end_date = parse_iso8601(raw["endDate"])
    delta = end_date - now_utc
    outcomes = _parse_json_list(raw.get("outcomes"))
    prices = [float(item) for item in _parse_json_list(raw.get("outcomePrices"))]
    token_ids = [str(item) for item in _parse_json_list(raw.get("clobTokenIds"))]

    normalized_outcomes = []
    for idx, name in enumerate(outcomes):
        normalized_outcomes.append(
            {
                "name": str(name),
                "price": prices[idx] if idx < len(prices) else None,
                "token_id": token_ids[idx] if idx < len(token_ids) else None,
            }
        )

    return {
        "market_id": str(raw["id"]),
        "condition_id": raw.get("conditionId"),
        "question": raw.get("question"),
        "slug": raw.get("slug"),
        "market_url": f"https://polymarket.com/event/{raw.get('slug')}",
        "end_date": raw.get("endDate"),
        "seconds_to_expiry": int(delta.total_seconds()),
        "days_to_expiry": round(delta.total_seconds() / 86400, 4),
        "liquidity_usdc": float(raw.get("liquidityNum") or raw.get("liquidity") or 0),
        "volume_usdc": float(raw.get("volumeNum") or raw.get("volume") or 0),
        "volume_24h_usdc": float(raw.get("volume24hr") or 0),
        "best_bid": raw.get("bestBid"),
        "best_ask": raw.get("bestAsk"),
        "last_trade_price": raw.get("lastTradePrice"),
        "spread": raw.get("spread"),
        "accepting_orders": bool(raw.get("acceptingOrders")),
        "closed": bool(raw.get("closed")),
        "active": bool(raw.get("active")),
        "outcomes": normalized_outcomes,
        "source": "polymarket_gamma_api",
    }


def fetch_markets(
    min_liquidity: float,
    max_expiry_days: float,
    page_size: int,
    max_pages: int,
    session: requests.Session | None = None,
) -> Tuple[List[Dict[str, Any]], int]:
    client = session or _build_retrying_session()
    now_utc = parse_iso8601(utc_now_iso())
    end_date_max = now_utc + timedelta(days=max_expiry_days)
    matched: List[Dict[str, Any]] = []
    fetched = 0

    for page in range(max_pages):
        params = {
            "active": "true",
            "closed": "false",
            "limit": page_size,
            "offset": page * page_size,
            "order": "endDate",
            "ascending": "true",
            "liquidity_num_min": min_liquidity,
            "end_date_min": now_utc.isoformat().replace("+00:00", "Z"),
            "end_date_max": end_date_max.isoformat().replace("+00:00", "Z"),
        }
        response = client.get(f"{gamma_api_base()}/markets", params=params, timeout=20)
        response.raise_for_status()
        items = response.json()
        if not items:
            break

        fetched += len(items)
        for raw in items:
            try:
                market = normalize_market(raw, now_utc)
            except ValueError:
                continue
            if not market["accepting_orders"] or market["closed"] or not market["active"]:
                continue
            if market["liquidity_usdc"] <= min_liquidity:
                continue
            if market["seconds_to_expiry"] < 0:
                continue
            if market["days_to_expiry"] > max_expiry_days:
                continue
            if blocked_market_reason(market):
                continue
            matched.append(market)

        if len(items) < page_size:
            break

    matched.sort(key=lambda item: (item["days_to_expiry"], -item["liquidity_usdc"]))
    return matched, fetched


def build_payload(args: argparse.Namespace) -> Dict[str, Any]:
    markets, fetched = fetch_markets(
        min_liquidity=args.min_liquidity,
        max_expiry_days=args.max_expiry_days,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )
    return {
        "generated_at": utc_now_iso(),
        "source": f"{gamma_api_base()}/markets",
        "filters": {
            "min_liquidity_usdc": args.min_liquidity,
            "max_expiry_days": args.max_expiry_days,
            "page_size": args.page_size,
            "max_pages": args.max_pages,
        },
        "counts": {
            "fetched": fetched,
            "matched": len(markets),
        },
        "markets": markets,
    }


def scan_and_persist(conn, *, min_liquidity: float = 10000, max_expiry_days: float = 7,
                     page_size: int = 100, max_pages: int = 5) -> List[Dict[str, Any]]:
    """Scan markets and persist to DB. Returns market dicts."""
    markets, _ = fetch_markets(
        min_liquidity=min_liquidity,
        max_expiry_days=max_expiry_days,
        page_size=page_size,
        max_pages=max_pages,
    )
    for market in markets:
        upsert_market_snapshot(conn, market)
    return markets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan Polymarket markets and emit normalized JSON.")
    parser.add_argument("--min-liquidity", type=float, default=10000, help="Minimum market liquidity in USDC.")
    parser.add_argument("--max-expiry-days", type=float, default=7, help="Maximum days until expiry.")
    parser.add_argument("--page-size", type=int, default=100, help="Gamma API page size.")
    parser.add_argument("--max-pages", type=int, default=5, help="Maximum number of pages to scan.")
    parser.add_argument("--output", type=str, help="Optional file path for JSON output.")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    payload = build_payload(args)
    init_db()
    with connect_db() as conn:
        for market in payload["markets"]:
            upsert_market_snapshot(conn, market)
        conn.commit()
    output_path = args.output if args.output else None
    print(dump_json(payload, path=output_path, pretty=not args.compact))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
