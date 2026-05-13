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


def cryptopanic_soft_fail_context(error: str) -> Dict[str, Any]:
    message = "CryptoPanic news temporarily unavailable. Agent should rely on remaining context providers."
    detail = sanitize_text(error) or "unknown_cryptopanic_error"
    return {
        "source_type": "cryptopanic",
        "source_id": "soft_fail",
        "title": "CryptoPanic context unavailable",
        "published_at": utc_now_iso(),
        "url": None,
        "raw_text": detail,
        "display_text": short_context_line("NEWS: ", message, limit=280),
        "importance_weight": 0.2,
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
        api_key = (os.getenv("PERPLEXITY_API_KEY") or "").strip()
        if not api_key:
            return []
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
        try:
            response = self.session.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=40,
            )
            response.raise_for_status()
            body = response.json()
        except Exception:
            return []
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


class SportsDataAdapter:
    """football-data.org adapter — recent team form for sports markets.

    Polymarket sports questions often skip league keywords ("Will Arsenal FC
    win on 2026-04-29?", "Club Atlético de Madrid vs. Arsenal FC"), so the
    classifier keyword list misses them. Instead of gating on the classifier,
    we gate on whether team-name extraction succeeds — that's itself a strong
    sports signal, and `build_sports_context` already returns None on any
    extraction or lookup failure, keeping the path silent for non-sports
    questions.
    """

    def fetch(self, market: Mapping[str, Any], *, limit: int = 1) -> List[Dict[str, Any]]:
        from .services.sports_data import _extract_teams, build_sports_context

        if _extract_teams(market.get("question") or "") is None:
            return []
        text = build_sports_context(market)
        if not text:
            return []
        return [
            {
                "source_type": "sports_data",
                "source_id": f"fd_{market.get('market_id')}",
                "title": "Team recent form",
                "published_at": utc_now_iso(),
                "url": None,
                "raw_text": text,
                "display_text": short_context_line("TEAM FORM: ", text, limit=600),
                "importance_weight": 1.2,
                "normalized_payload_json": {"text": text},
            }
        ]


class WebSearchAdapter:
    """Free news context via DuckDuckGo Instant Answer API. No API key required."""

    endpoint = "https://api.duckduckgo.com/"

    def fetch(self, market: Mapping[str, Any]) -> List[Dict[str, Any]]:
        question = (market.get("question") or "")[:120].strip()
        if not question:
            return []
        try:
            resp = requests.get(
                self.endpoint,
                params={"q": question, "format": "json", "no_html": "1", "skip_disambig": "1"},
                timeout=10,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception:
            return []
        snippets: List[str] = []
        abstract = (body.get("AbstractText") or "").strip()
        if abstract:
            snippets.append(abstract)
        for topic in (body.get("RelatedTopics") or [])[:4]:
            if not isinstance(topic, dict):
                continue
            text = (topic.get("Text") or "").strip()
            if text and text not in snippets:
                snippets.append(text)
        return [
            {
                "source_type": "web_search",
                "source_id": None,
                "title": f"Web: {question[:80]}",
                "published_at": utc_now_iso(),
                "url": None,
                "raw_text": s,
                "display_text": short_context_line("WEB: ", s, limit=300),
                "importance_weight": 0.6,
                "normalized_payload_json": {"query": question, "snippet": s},
            }
            for s in snippets
            if s
        ]


class PolymarketHistoricalAdapter:
    """Computes a base-rate prior from this DB's `market_resolutions` table.

    The bot has been scanning Polymarket for weeks; every resolved market we
    saw is now a sample of the base rate for "questions like this one". For a
    fresh draw / O-U / underdog market, we look up similar resolved markets
    (matched by market_topic keyword + outcome shape) and surface the
    historical fraction that resolved on the bot's picked outcome.

    No API calls — pure local SQL. Free, fast, and ground-truth.
    """

    MIN_SAMPLES = 5
    LOOKBACK_DAYS = 90

    def fetch(self, market: Mapping[str, Any]) -> List[Dict[str, Any]]:
        from .db import connect_db
        from .services.event_cluster_service import classify_market_class

        question = (market.get("question") or "").strip()
        if not question:
            return []
        market_class = classify_market_class(market)
        topic = market_topic(market) or question[:60]
        keyword = self._topic_keyword(topic)
        if not keyword:
            return []
        with connect_db() as conn:
            rows = conn.execute(
                """
                SELECT mr.market_id, mr.resolved_outcome, ms.question
                FROM market_resolutions mr
                JOIN market_snapshots ms ON ms.market_id = mr.market_id
                WHERE mr.resolved_outcome IS NOT NULL
                  AND mr.resolved_at > datetime('now', ?)
                  AND ms.market_id != ?
                  AND (ms.question LIKE ? OR ms.question LIKE ?)
                ORDER BY mr.resolved_at DESC
                LIMIT 200
                """,
                (
                    f"-{self.LOOKBACK_DAYS} days",
                    str(market.get("market_id") or ""),
                    f"%{keyword}%",
                    f"%{keyword.lower()}%",
                ),
            ).fetchall()
        if len(rows) < self.MIN_SAMPLES:
            return []
        yes_count = sum(1 for r in rows if (r["resolved_outcome"] or "").strip().lower() == "yes")
        n = len(rows)
        yes_rate = yes_count / n
        text = (
            f"BASE RATE for similar '{keyword}' markets (last {self.LOOKBACK_DAYS}d, n={n}): "
            f"resolved YES {yes_count}/{n} = {yes_rate * 100:.1f}%."
        )
        return [
            {
                "source_type": "polymarket_historical",
                "source_id": f"hist_{keyword}_{n}",
                "title": f"Historical base rate ({n} similar)",
                "published_at": utc_now_iso(),
                "url": None,
                "raw_text": text,
                "display_text": short_context_line("HISTORICAL: ", text, limit=400),
                "importance_weight": 1.4,  # high — empirical base rate beats model guesses
                "normalized_payload_json": {
                    "keyword": keyword,
                    "n": n,
                    "yes_rate": yes_rate,
                    "yes_count": yes_count,
                    "lookback_days": self.LOOKBACK_DAYS,
                    "market_class": market_class,
                },
            }
        ]

    @staticmethod
    def _topic_keyword(topic: str) -> str:
        """Pull the most signal-bearing keyword: 'draw', 'win', 'O/U', etc.

        For "Will A vs B end in a draw?" → "end in a draw"
        For "Will Hradec Králové win on …" → "win"
        For "Real Betis vs Elche: O/U 2.5" → "O/U"
        """
        lowered = topic.lower()
        if "draw" in lowered:
            return "end in a draw"
        if "o/u" in lowered or "over/under" in lowered:
            return "O/U"
        if "both teams to score" in lowered or "btts" in lowered:
            return "Both Teams to Score"
        if " win" in lowered:
            return "win"
        return ""


class GdeltAdapter:
    """Global Database of Events, Language, and Tone — 2.0 DOC API.

    Free, no auth. Returns recent globally-tracked articles matching a query.
    Best for political / macro / news-driven markets where a public event
    has likely been reported by multiple outlets.

    https://api.gdeltproject.org/api/v2/doc/doc?query=Q&format=json
    """

    endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"
    timeout = 15

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def fetch(self, market: Mapping[str, Any], *, max_records: int = 5) -> List[Dict[str, Any]]:
        from .services.event_cluster_service import classify_market_class

        # GDELT is overkill for sports (chronicles every match endlessly) and
        # near-useless for esports / weather. Use a defensive double-gate:
        # skip if classifier says non-political-tech-other, AND skip if the
        # question contains common sports keywords (since the classifier
        # often misroutes sports as 'other').
        market_class = classify_market_class(market)
        if market_class not in ("politics", "tech", "other"):
            return []
        question_lower = (market.get("question") or "").lower()
        sports_hints = ("draw", " win on ", "vs.", "vs ", "o/u", "over/under",
                        "both teams to score", "btts", "winner")
        if any(hint in question_lower for hint in sports_hints):
            return []
        query = market_topic(market) or (market.get("question") or "")[:80]
        if not query:
            return []
        try:
            response = self.session.get(
                self.endpoint,
                params={
                    "query": query,
                    "format": "json",
                    "mode": "ArtList",
                    "maxrecords": max_records,
                    "sort": "DateDesc",
                    "timespan": "48H",  # last 48h only
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except Exception:
            return []
        articles = body.get("articles") or []
        contexts: List[Dict[str, Any]] = []
        for art in articles[:max_records]:
            title = sanitize_text(str(art.get("title") or ""))
            if not title:
                continue
            seendate = art.get("seendate") or ""
            source = sanitize_text(str(art.get("domain") or ""))
            line = f"{seendate[:10] if len(seendate) >= 10 else seendate} {source}: {title}"
            contexts.append(
                {
                    "source_type": "gdelt",
                    "source_id": art.get("url") or art.get("documentidentifier") or title[:60],
                    "title": title,
                    "published_at": seendate,
                    "url": art.get("url"),
                    "raw_text": json.dumps({"title": title, "domain": source, "seendate": seendate}, sort_keys=False),
                    "display_text": short_context_line("GDELT: ", line, limit=260),
                    "importance_weight": 0.85,
                    "normalized_payload_json": {
                        "title": title,
                        "domain": source,
                        "seendate": seendate,
                        "url": art.get("url"),
                    },
                }
            )
        return contexts


class TavilyAdapter:
    """Tavily LLM-friendly search API.

    PAID — 1000 lifetime credits on the dev key. Enforce a strict daily cap
    via adapter_budget_tracking so a single bad day can't burn the pool.

    Tavily returns an LLM-synthesized answer + ranked source snippets, which
    is more useful per-call than raw web search. Fire only as a fallback when
    other adapters returned thin context.
    """

    endpoint = "https://api.tavily.com/search"
    timeout = 30
    default_daily_cap = 30  # 30/day * 30 days = 900/month — safe under 1000 lifetime

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def fetch(self, market: Mapping[str, Any]) -> List[Dict[str, Any]]:
        from .db import connect_db, adapter_budget_calls_today, adapter_budget_increment

        api_key = (os.getenv("TAVILY_API_KEY") or "").strip()
        if not api_key:
            return []
        daily_cap = get_env_int("TAVILY_DAILY_CAP", self.default_daily_cap)
        with connect_db() as conn:
            used = adapter_budget_calls_today(conn, "tavily")
            if used >= daily_cap:
                return []  # cap hit — silently skip until tomorrow
        topic = market_topic(market)
        question = (market.get("question") or "").strip()
        end_date = (market.get("end_date") or "")[:10]
        query = (
            f"Latest factual updates on: {topic}. "
            f"Original market question: {question}. Market resolves by {end_date}."
        )[:380]
        try:
            response = self.session.post(
                self.endpoint,
                json={
                    "api_key": api_key,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": 5,
                    "include_answer": True,
                    "days": 7,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except Exception:
            return []
        with connect_db() as conn:
            adapter_budget_increment(conn, "tavily")
            conn.commit()
        contexts: List[Dict[str, Any]] = []
        answer = sanitize_text(str(body.get("answer") or ""))
        if answer:
            contexts.append(
                {
                    "source_type": "tavily",
                    "source_id": f"tavily_answer_{topic[:40]}",
                    "title": f"Tavily synthesized answer: {topic}",
                    "published_at": utc_now_iso(),
                    "url": None,
                    "raw_text": answer,
                    "display_text": short_context_line("TAVILY: ", answer, limit=500),
                    "importance_weight": 1.1,
                    "normalized_payload_json": {"topic": topic, "answer": answer, "kind": "answer"},
                }
            )
        for r in (body.get("results") or [])[:3]:
            title = sanitize_text(str(r.get("title") or ""))
            content = sanitize_text(str(r.get("content") or r.get("snippet") or ""))
            if not title and not content:
                continue
            snippet = f"{title} — {content}" if title else content
            contexts.append(
                {
                    "source_type": "tavily",
                    "source_id": r.get("url") or title[:80],
                    "title": title,
                    "published_at": utc_now_iso(),
                    "url": r.get("url"),
                    "raw_text": snippet,
                    "display_text": short_context_line("TAVILY: ", snippet, limit=280),
                    "importance_weight": 0.95,
                    "normalized_payload_json": {
                        "title": title,
                        "content": content,
                        "url": r.get("url"),
                        "score": r.get("score"),
                        "kind": "result",
                    },
                }
            )
        return contexts


class TheOddsApiAdapter:
    """The Odds API — bookmaker consensus prices across 40+ sportsbooks.

    PAID — 500 free reqs/month. Enforce a strict daily cap via
    adapter_budget_tracking.

    Returns the average implied probability across major books for a
    Polymarket question. The "alpha" comes from comparing Polymarket's price
    to the bookmaker consensus — when they diverge by more than the implied
    Polymarket fee, that's a structural mispricing.
    """

    base_url = "https://api.the-odds-api.com/v4"
    timeout = 25
    default_daily_cap = 15  # 15/day * 30 = 450/month — safe under 500

    # Polymarket question keyword → Odds API sport key (priority order).
    SPORT_KEYWORDS = (
        ("FC Barcelona", "soccer_spain_la_liga"),
        ("Real Madrid", "soccer_spain_la_liga"),
        ("Liga MX", "soccer_mexico_ligamx"),
        ("Premier League", "soccer_epl"),
        ("EFL Championship", "soccer_efl_champ"),
        ("Bundesliga", "soccer_germany_bundesliga"),
        ("Serie A", "soccer_italy_serie_a"),
        ("Ligue 1", "soccer_france_ligue_one"),
        ("MLS", "soccer_usa_mls"),
        ("UEFA Champions League", "soccer_uefa_champs_league"),
        ("La Liga", "soccer_spain_la_liga"),
        ("NBA", "basketball_nba"),
        ("NFL", "americanfootball_nfl"),
        ("MLB", "baseball_mlb"),
        ("NHL", "icehockey_nhl"),
    )

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def fetch(self, market: Mapping[str, Any]) -> List[Dict[str, Any]]:
        from .db import connect_db, adapter_budget_calls_today, adapter_budget_increment

        api_key = (os.getenv("ODDS_API_KEY") or "").strip()
        if not api_key:
            return []
        # Gate on whether we can map this market to a known league keyword.
        # classify_market_class returns 'other' for many Polymarket sports
        # questions ("Will A vs B end in a draw?"); using _resolve_sport as
        # the primary gate is more accurate because it directly looks for
        # league/team keywords we have Odds-API mappings for.
        sport_key = self._resolve_sport(market)
        if not sport_key:
            return []
        daily_cap = get_env_int("ODDS_API_DAILY_CAP", self.default_daily_cap)
        with connect_db() as conn:
            used = adapter_budget_calls_today(conn, "odds_api")
            if used >= daily_cap:
                return []
        try:
            response = self.session.get(
                f"{self.base_url}/sports/{sport_key}/odds",
                params={
                    "apiKey": api_key,
                    "regions": "eu,uk",
                    "markets": "h2h,totals",
                    "oddsFormat": "decimal",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            events = response.json()
        except Exception:
            return []
        with connect_db() as conn:
            adapter_budget_increment(conn, "odds_api")
            conn.commit()
        event = self._match_event(events, market)
        if not event:
            return []
        consensus = self._average_consensus(event)
        if not consensus:
            return []
        question = (market.get("question") or "")[:80]
        lines = []
        if consensus.get("h2h"):
            h2h = consensus["h2h"]
            parts = [f"{name}={prob:.3f}" for name, prob in h2h.items()]
            lines.append(f"H2H (avg {consensus['book_count']} books): " + " / ".join(parts))
        if consensus.get("totals"):
            for total_line, sides in consensus["totals"].items():
                parts = [f"{name}={prob:.3f}" for name, prob in sides.items()]
                lines.append(f"Totals {total_line}: " + " / ".join(parts))
        text = " | ".join(lines)
        return [
            {
                "source_type": "odds_api",
                "source_id": f"odds_{event.get('id') or sport_key}",
                "title": f"Bookmaker consensus: {question}",
                "published_at": event.get("commence_time") or utc_now_iso(),
                "url": None,
                "raw_text": text,
                "display_text": short_context_line("BOOKMAKERS: ", text, limit=500),
                "importance_weight": 1.3,  # consensus odds are very strong evidence
                "normalized_payload_json": {
                    "sport_key": sport_key,
                    "event_id": event.get("id"),
                    "commence_time": event.get("commence_time"),
                    "consensus": consensus,
                    "n_books": consensus.get("book_count"),
                },
            }
        ]

    def _resolve_sport(self, market: Mapping[str, Any]) -> str | None:
        haystack = " ".join([
            str(market.get("question") or ""),
            json.dumps(market.get("market_json") or {}, ensure_ascii=False)[:500],
        ])
        for keyword, sport_key in self.SPORT_KEYWORDS:
            if keyword.lower() in haystack.lower():
                return sport_key
        return None

    def _match_event(self, events: list, market: Mapping[str, Any]) -> Dict[str, Any] | None:
        """Find the Odds-API event corresponding to our Polymarket question.

        Polymarket questions like "Will FC Barcelona vs. Real Madrid end in a draw?"
        and "Real Madrid vs. Barcelona: O/U 2.5" both reference the same fixture.
        Match by checking if both home_team and away_team substrings appear in the
        question text."""
        question = (market.get("question") or "").lower()
        if not question:
            return None
        for ev in events or []:
            home = (ev.get("home_team") or "").lower()
            away = (ev.get("away_team") or "").lower()
            if home and away and home in question and away in question:
                return ev
            # Fallback: any single team match for win-based markets
            if home and home in question and "win" in question:
                return ev
        return None

    def _average_consensus(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Average bookmaker probabilities across markets."""
        bookmakers = event.get("bookmakers") or []
        if not bookmakers:
            return {}
        h2h_sum: Dict[str, list] = {}
        totals_sum: Dict[str, Dict[str, list]] = {}
        for bm in bookmakers:
            for mkt in (bm.get("markets") or []):
                if mkt.get("key") == "h2h":
                    for o in (mkt.get("outcomes") or []):
                        name = o.get("name") or ""
                        price = float(o.get("price") or 0)
                        if price > 0:
                            h2h_sum.setdefault(name, []).append(1.0 / price)
                elif mkt.get("key") == "totals":
                    for o in (mkt.get("outcomes") or []):
                        line = str(o.get("point") or "")
                        name = (o.get("name") or "") + f" {line}"
                        price = float(o.get("price") or 0)
                        if price > 0:
                            totals_sum.setdefault(line, {}).setdefault(name, []).append(1.0 / price)
        result: Dict[str, Any] = {"book_count": len(bookmakers)}
        if h2h_sum:
            # Raw implied probabilities sum > 1.0 due to vig; normalize to sum=1.
            avg = {k: sum(v) / len(v) for k, v in h2h_sum.items()}
            total = sum(avg.values()) or 1.0
            result["h2h"] = {k: v / total for k, v in avg.items()}
        if totals_sum:
            normalized_totals: Dict[str, Dict[str, float]] = {}
            for line, sides in totals_sum.items():
                avg = {k: sum(v) / len(v) for k, v in sides.items()}
                total = sum(avg.values()) or 1.0
                normalized_totals[line] = {k: v / total for k, v in avg.items()}
            result["totals"] = normalized_totals
        return result


class RedditAdapter:
    """Reddit sentiment via the Reddit OAuth API.

    Needs three env vars: REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET,
    REDDIT_USER_AGENT. Without them the adapter just returns []. The user
    needs to register a "script" app at https://www.reddit.com/prefs/apps
    and supply the resulting credentials.

    For each market class we hit a different subreddit cluster — sportsbook
    for sports, politicaldebate for politics, etc. Returns the top N hot
    comments mentioning the topic keyword.
    """

    endpoint = "https://www.reddit.com"
    oauth_endpoint = "https://www.reddit.com/api/v1/access_token"
    timeout = 15

    SUBREDDITS_BY_CLASS = {
        "sports_winner": ["sportsbook", "soccer", "nba", "nfl"],
        "sports_totals": ["sportsbook"],
        "politics": ["PoliticalForecasting", "Polymarket", "neoliberal"],
        "tech": ["stocks", "wallstreetbets"],
        "other": ["Polymarket", "prediction"],
    }

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self._token: str | None = None

    def fetch(self, market: Mapping[str, Any], *, limit: int = 5) -> List[Dict[str, Any]]:
        from .services.event_cluster_service import classify_market_class

        client_id = (os.getenv("REDDIT_CLIENT_ID") or "").strip()
        client_secret = (os.getenv("REDDIT_CLIENT_SECRET") or "").strip()
        user_agent = (os.getenv("REDDIT_USER_AGENT") or "PolyTradingUltra/0.1").strip()
        if not client_id or not client_secret:
            return []
        market_class = classify_market_class(market)
        subreddits = self.SUBREDDITS_BY_CLASS.get(market_class)
        if not subreddits:
            return []
        token = self._get_token(client_id, client_secret, user_agent)
        if not token:
            return []
        topic = market_topic(market) or (market.get("question") or "")[:60]
        if not topic:
            return []
        contexts: List[Dict[str, Any]] = []
        for sub in subreddits[:2]:  # cap subreddit fan-out
            try:
                resp = self.session.get(
                    f"https://oauth.reddit.com/r/{sub}/search",
                    headers={"Authorization": f"Bearer {token}", "User-Agent": user_agent},
                    params={"q": topic, "limit": limit, "restrict_sr": "true", "sort": "new", "t": "week"},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                continue
            for item in (data.get("data") or {}).get("children", [])[:limit]:
                post = item.get("data") or {}
                title = sanitize_text(str(post.get("title") or ""))
                selftext = sanitize_text(str(post.get("selftext") or ""))[:240]
                if not title:
                    continue
                snippet = f"r/{sub}: {title}" + (f" — {selftext}" if selftext else "")
                contexts.append(
                    {
                        "source_type": "reddit",
                        "source_id": post.get("id") or post.get("permalink") or title[:80],
                        "title": title,
                        "published_at": (post.get("created_utc") and str(int(post["created_utc"]))) or None,
                        "url": post.get("url"),
                        "raw_text": json.dumps(
                            {"title": title, "selftext": selftext, "subreddit": sub, "score": post.get("score")},
                            sort_keys=False,
                        ),
                        "display_text": short_context_line("REDDIT: ", snippet, limit=280),
                        "importance_weight": 0.5 + min((post.get("score") or 0) / 1000.0, 0.4),
                        "normalized_payload_json": {
                            "subreddit": sub,
                            "title": title,
                            "score": post.get("score"),
                            "selftext": selftext,
                            "url": post.get("url"),
                        },
                    }
                )
        return contexts

    def _get_token(self, client_id: str, client_secret: str, user_agent: str) -> str | None:
        if self._token:
            return self._token
        try:
            resp = self.session.post(
                self.oauth_endpoint,
                auth=(client_id, client_secret),
                data={"grant_type": "client_credentials"},
                headers={"User-Agent": user_agent},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            self._token = resp.json().get("access_token")
            return self._token
        except Exception:
            return None


def compose_context_payload(market: Mapping[str, Any], contexts: List[Dict[str, Any]], max_chars: int) -> Dict[str, Any]:
    _source_order = {
        "odds_api": 0,              # bookmaker consensus — strongest sports signal
        "sports_data": 1,           # team form
        "polymarket_historical": 2, # empirical base rate
        "gdelt": 3,                 # global news events
        "perplexity": 4,            # LLM summary
        "tavily": 5,                # LLM-friendly search
        "cryptopanic": 6,           # crypto news (mostly disabled)
        "reddit": 7,                # sentiment
        "web_search": 8,            # DuckDuckGo (mostly empty)
        "apify_twitter": 9,         # twitter (noisy)
    }
    ordered = sorted(
        contexts,
        key=lambda item: (
            _source_order.get(item["source_type"], 99),
            -(float(item.get("importance_weight") or 0)),
            str(item.get("published_at") or ""),
        ),
    )
    remaining = max_chars
    included: List[Dict[str, Any]] = []
    lines: List[str] = []
    _PREFIX_BY_SOURCE = {
        "sports_data": "TEAM FORM: ",
        "perplexity": "SUMMARY: ",
        "cryptopanic": "NEWS: ",
        "web_search": "WEB: ",
        "apify_twitter": "X: ",
        "polymarket_historical": "HISTORICAL: ",
        "gdelt": "GDELT: ",
        "tavily": "TAVILY: ",
        "odds_api": "BOOKMAKERS: ",
        "reddit": "REDDIT: ",
    }
    for item in ordered:
        source_type = item["source_type"]
        prefix = _PREFIX_BY_SOURCE.get(source_type, "X: ")
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
    """Default provider list — only enables paid adapters when their API keys are set.

    Always-on (free, no auth): polymarket_historical, gdelt, web_search, cryptopanic
      (cryptopanic auto-soft-fails to a stub when its key is missing).
    Conditional (env-key gated): sports_data, perplexity, tavily, odds_api, reddit.
    """
    import os as _os
    parts: List[str] = []
    # Always-on free adapters first.
    parts.append("polymarket_historical")
    parts.append("gdelt")
    # Conditional on API keys.
    if (_os.getenv("FOOTBALL_DATA_API_KEY") or "").strip():
        parts.append("sports_data")
    if (_os.getenv("PERPLEXITY_API_KEY") or "").strip():
        parts.append("perplexity")
    if (_os.getenv("TAVILY_API_KEY") or "").strip():
        parts.append("tavily")
    if (_os.getenv("ODDS_API_KEY") or "").strip():
        parts.append("odds_api")
    if (_os.getenv("REDDIT_CLIENT_ID") or "").strip() and (_os.getenv("REDDIT_CLIENT_SECRET") or "").strip():
        parts.append("reddit")
    # Existing free/soft-fail adapters.
    parts.extend(["cryptopanic", "web_search"])
    default = ",".join(parts)
    raw = value or default
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
    # Local/free adapters first — no network cost.
    if "polymarket_historical" in enabled:
        try:
            contexts.extend(PolymarketHistoricalAdapter().fetch(market))
        except Exception:
            pass
    if "odds_api" in enabled:
        try:
            contexts.extend(TheOddsApiAdapter().fetch(market))
        except Exception:
            pass
    if "sports_data" in enabled:
        try:
            contexts.extend(SportsDataAdapter().fetch(market, limit=limit))
        except Exception:
            pass
    if "gdelt" in enabled:
        try:
            contexts.extend(GdeltAdapter().fetch(market))
        except Exception:
            pass
    if "perplexity" in enabled:
        try:
            contexts.extend(PerplexityAdapter().fetch(market))
        except Exception:
            pass
    if "tavily" in enabled:
        try:
            contexts.extend(TavilyAdapter().fetch(market))
        except Exception:
            pass
    if "cryptopanic" in enabled:
        try:
            contexts.extend(CryptoPanicAdapter().fetch(market, limit=limit))
        except Exception as exc:
            contexts.append(cryptopanic_soft_fail_context(str(exc)))
    if "reddit" in enabled:
        try:
            contexts.extend(RedditAdapter().fetch(market, limit=limit))
        except Exception:
            pass
    if "web_search" in enabled:
        try:
            contexts.extend(WebSearchAdapter().fetch(market))
        except Exception:
            pass
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


def fetch_and_persist_contexts(conn, markets: List[Dict[str, Any]], *,
                               providers: List[str] | None = None,
                               limit: int = 5, min_favorite_count: int = 5,
                               budget_chars: int = 2400) -> List[Dict[str, Any]]:
    """Fetch contexts for a list of markets and persist to DB. Returns result dicts."""
    providers = providers or provider_names(None)
    results = []
    for market in markets:
        result = fetch_contexts_for_market(
            market,
            providers=providers,
            limit=limit,
            min_favorite_count=min_favorite_count,
            budget_chars=budget_chars,
        )
        results.append(result)
    for market, result in zip(markets, results):
        replace_market_contexts(conn, str(market["market_id"]), result["contexts"])
    return results


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
