from __future__ import annotations

import argparse
import json
import os
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Mapping

import requests

from .common import (
    dump_json,
    get_env_int,
    infer_market_symbol,
    load_json,
    load_repo_env,
    market_topic,
    parse_iso8601,
    sanitize_text,
    short_context_line,
    strip_urls,
    utc_now_iso,
)
from .db import connect_db, init_db, replace_market_contexts, upsert_market_snapshot

load_repo_env()


class CryptoPanicAdapter:
    default_posts_url = "https://cryptopanic.com/api/developer/v2/posts/"

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        configured_base = (os.getenv("CRYPTOPANIC_BASE_URL") or self.default_posts_url).strip()
        normalized = configured_base.rstrip("/")
        if not normalized.endswith("/posts"):
            normalized = f"{normalized}/posts"
        self.base_url = f"{normalized}/"

    def fetch(self, market: Mapping[str, Any], limit: int) -> List[Dict[str, Any]]:
        auth_token = os.getenv("CRYPTOPANIC_AUTH_TOKEN")
        if not auth_token:
            raise RuntimeError("CRYPTOPANIC_AUTH_TOKEN is required for CryptoPanicAdapter.")
        params = {"auth_token": auth_token, "public": "true"}
        symbol = infer_market_symbol(market)
        if symbol:
            params["currencies"] = symbol
        response = self.session.get(self.base_url, params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()
        contexts = []
        for item in payload.get("results", [])[:limit]:
            title = sanitize_text(str(item.get("title") or ""))
            published_at = item.get("published_at")
            contexts.append(
                {
                    "source_type": "cryptopanic",
                    "source_id": str(item.get("id") or ""),
                    "title": title,
                    "published_at": published_at,
                    "url": item.get("url"),
                    "raw_text": json.dumps({"title": title, "published_at": published_at}, sort_keys=False),
                    "display_text": short_context_line(
                        "NEWS: ",
                        f"{published_at or 'unknown time'} {title}",
                        limit=220,
                    ),
                    "importance_weight": 0.7,
                    "normalized_payload_json": {
                        "title": title,
                        "published_at": published_at,
                        "url": item.get("url"),
                    },
                }
            )
        return contexts


class ApifyTwitterAdapter:
    actor_id = "apidojo/tweet-scraper"

    def fetch(self, market: Mapping[str, Any], limit: int, min_favorite_count: int) -> List[Dict[str, Any]]:
        token = os.getenv("APIFY_TOKEN") or os.getenv("APIFY_API_KEY")
        if not token:
            raise RuntimeError("APIFY_TOKEN (or APIFY_API_KEY) is required for ApifyTwitterAdapter.")
        try:
            from apify_client import ApifyClient
        except ImportError as exc:
            raise RuntimeError("apify-client must be installed to use ApifyTwitterAdapter.") from exc

        query = market_topic(market)
        client = ApifyClient(token)
        run = client.actor(self.actor_id).call(run_input={"searchTerms": [query], "maxItems": limit})
        status_message = str(run.get("statusMessage") or "")
        normalized_status = status_message.lower()
        if "free plan" in normalized_status or "subscribe to a paid plan" in normalized_status:
            raise RuntimeError(
                "Apify actor access denied by current plan. Upgrade Apify plan to enable apidojo/tweet-scraper API runs."
            )
        items = client.dataset(run["defaultDatasetId"]).list_items().items
        contexts = []
        for item in items[:limit]:
            favorite_count = int(item.get("favorite_count") or item.get("likeCount") or 0)
            if favorite_count < min_favorite_count:
                continue
            text = strip_urls(str(item.get("full_text") or item.get("text") or ""))
            if not text:
                continue
            contexts.append(
                {
                    "source_type": "apify_twitter",
                    "source_id": str(item.get("id") or item.get("url") or ""),
                    "title": None,
                    "published_at": item.get("created_at") or item.get("createdAt"),
                    "url": item.get("url"),
                    "raw_text": json.dumps(
                        {
                            "full_text": str(item.get("full_text") or item.get("text") or ""),
                            "favorite_count": favorite_count,
                        },
                        sort_keys=False,
                    ),
                    "display_text": short_context_line("X: ", text, limit=280),
                    "importance_weight": 0.4 + min(favorite_count / 1000.0, 0.5),
                    "normalized_payload_json": {
                        "full_text": text,
                        "favorite_count": favorite_count,
                        "url": item.get("url"),
                    },
                }
            )
        return contexts


def twitter_soft_fail_context(error: str) -> Dict[str, Any]:
    message = "Twitter scraping temporarily unavailable. Agent should rely solely on news contexts."
    detail = sanitize_text(error) or "unknown_apify_error"
    return {
        "source_type": "apify_twitter",
        "source_id": "soft_fail",
        "title": "Twitter context unavailable",
        "published_at": utc_now_iso(),
        "url": None,
        "raw_text": detail,
        "display_text": short_context_line("X: ", message, limit=280),
        "importance_weight": 0.15,
        "normalized_payload_json": {
            "soft_fail": True,
            "message": message,
            "error": detail,
        },
    }


class PerplexityAdapter:
    endpoint = "https://api.perplexity.ai/chat/completions"

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def fetch(self, market: Mapping[str, Any]) -> List[Dict[str, Any]]:
        api_key = os.getenv("PERPLEXITY_API_KEY")
        if not api_key:
            raise RuntimeError("PERPLEXITY_API_KEY is required for PerplexityAdapter.")
        topic = market_topic(market)
        payload = {
            "model": os.getenv("PERPLEXITY_MODEL", "sonar"),
            "messages": [
                {
                    "role": "user",
                    "content": f"Summarize the latest 24h factual updates regarding {topic} in under 150 words.",
                }
            ],
        }
        response = self.session.post(
            self.endpoint,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=40,
        )
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        summary = sanitize_text(content)
        return [
            {
                "source_type": "perplexity",
                "source_id": body.get("id"),
                "title": f"Perplexity summary for {topic}",
                "published_at": utc_now_iso(),
                "url": None,
                "raw_text": summary,
                "display_text": short_context_line("SUMMARY: ", summary, limit=600),
                "importance_weight": 1.0,
                "normalized_payload_json": {
                    "topic": topic,
                    "summary": summary,
                    "provider_response_id": body.get("id"),
                },
            }
        ]


def compose_context_payload(market: Mapping[str, Any], contexts: List[Dict[str, Any]], max_chars: int) -> Dict[str, Any]:
    ordered = sorted(
        contexts,
        key=lambda item: (
            {"perplexity": 0, "cryptopanic": 1, "apify_twitter": 2}[item["source_type"]],
            -(float(item.get("importance_weight") or 0)),
            str(item.get("published_at") or ""),
        ),
    )
    remaining = max_chars
    included: List[Dict[str, Any]] = []
    lines: List[str] = []
    for item in ordered:
        source_type = item["source_type"]
        prefix = "SUMMARY: " if source_type == "perplexity" else "NEWS: " if source_type == "cryptopanic" else "X: "
        display_text = str(item.get("display_text") or "")
        if display_text.startswith(prefix):
            display_text = display_text[len(prefix) :].lstrip()
        line = short_context_line(prefix, display_text, limit=min(remaining, 600))
        if remaining <= len(prefix):
            break
        if len(line) > remaining:
            line = short_context_line(prefix, display_text, limit=remaining)
        if len(line) <= len(prefix):
            continue
        lines.append(line)
        included.append(
            {
                "source_type": item["source_type"],
                "source_id": item.get("source_id"),
                "display_text": line,
                "published_at": item.get("published_at"),
            }
        )
        remaining -= len(line) + 1
        if remaining <= 0:
            break
    return {
        "market_id": str(market["market_id"]),
        "topic": market_topic(market),
        "context_budget_chars": max_chars,
        "assembled_text": "\n".join(lines),
        "sources": included,
    }


def provider_names(value: str | None) -> List[str]:
    raw = value or "perplexity,cryptopanic,apify_twitter"
    return [item.strip() for item in raw.split(",") if item.strip()]


def load_markets(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    markets = payload.get("markets")
    if not isinstance(markets, list):
        raise ValueError("market payload must contain a 'markets' list")
    return [dict(item) for item in markets]


def fetch_contexts_for_market(
    market: Mapping[str, Any],
    *,
    providers: Iterable[str],
    limit: int,
    min_favorite_count: int,
    budget_chars: int,
) -> Dict[str, Any]:
    contexts: List[Dict[str, Any]] = []
    enabled = list(providers)
    if "perplexity" in enabled:
        contexts.extend(PerplexityAdapter().fetch(market))
    if "cryptopanic" in enabled:
        contexts.extend(CryptoPanicAdapter().fetch(market, limit=limit))
    if "apify_twitter" in enabled:
        try:
            contexts.extend(ApifyTwitterAdapter().fetch(market, limit=limit, min_favorite_count=min_favorite_count))
        except Exception as exc:
            contexts.append(twitter_soft_fail_context(str(exc)))
    payload = compose_context_payload(market, contexts, max_chars=budget_chars)
    return {
        "market_id": str(market["market_id"]),
        "topic": market_topic(market),
        "symbol": infer_market_symbol(market),
        "contexts": contexts,
        "context_payload": payload,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch external event contexts for scanned Polymarket markets.")
    parser.add_argument("--market-file", required=True, help="Scanner JSON file.")
    parser.add_argument("--providers", help="Comma-separated providers: perplexity,cryptopanic,apify_twitter")
    parser.add_argument("--limit-per-provider", type=int, default=5, help="Maximum items per provider.")
    parser.add_argument("--min-favorite-count", type=int, default=5, help="Filter low-quality tweets.")
    parser.add_argument("--context-budget-chars", type=int, default=2400, help="OpenClaw context character budget.")
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    payload = load_json(args.market_file)
    markets = load_markets(payload)
    providers = provider_names(args.providers)
    results = []
    with connect_db() as conn:
        for market in markets:
            upsert_market_snapshot(conn, market)
            result = fetch_contexts_for_market(
                market,
                providers=providers,
                limit=args.limit_per_provider,
                min_favorite_count=args.min_favorite_count,
                budget_chars=args.context_budget_chars,
            )
            replace_market_contexts(conn, str(market["market_id"]), result["contexts"])
            results.append(result)
        conn.commit()
    response = {
        "generated_at": utc_now_iso(),
        "providers": providers,
        "window_start": (parse_iso8601(utc_now_iso()) - timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
        "window_end": utc_now_iso(),
        "markets": results,
    }
    print(dump_json(response, path=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
