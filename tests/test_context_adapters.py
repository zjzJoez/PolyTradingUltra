"""Unit tests for the 5 new evidence-source adapters added 2026-05-13.

Each adapter is tested for:
  - Graceful behavior when API key / data is missing (returns [])
  - Correct contract shape on a successful response (the 10 required dict keys)
  - Budget enforcement (Tavily, Odds API)
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from polymarket_mvp.event_fetcher import (
    GdeltAdapter,
    PolymarketHistoricalAdapter,
    RedditAdapter,
    TavilyAdapter,
    TheOddsApiAdapter,
    compose_context_payload,
    provider_names,
)


REQUIRED_KEYS = {
    "source_type", "source_id", "title", "published_at", "url",
    "raw_text", "display_text", "importance_weight", "normalized_payload_json",
}


def _assert_contract(test, contexts, expected_source_type):
    test.assertGreater(len(contexts), 0)
    for ctx in contexts:
        missing = REQUIRED_KEYS - set(ctx.keys())
        test.assertFalse(missing, f"missing required keys: {missing}")
        test.assertEqual(ctx["source_type"], expected_source_type)
        test.assertIsInstance(ctx["display_text"], str)
        test.assertGreater(len(ctx["display_text"]), 0)
        test.assertGreater(float(ctx["importance_weight"] or 0), 0)
        test.assertIsInstance(ctx["normalized_payload_json"], dict)


class PolymarketHistoricalAdapterTests(unittest.TestCase):
    """Pure local SQL — no network. Validates the keyword extraction + the
    base-rate query path against a fixture DB."""

    def _make_fixture_db(self, resolved_yes_count: int, resolved_no_count: int):
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        path = tmp.name
        tmp.close()
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE market_snapshots (
                market_id TEXT PRIMARY KEY,
                question TEXT,
                slug TEXT,
                market_url TEXT,
                condition_id TEXT,
                active INTEGER NOT NULL DEFAULT 0,
                closed INTEGER NOT NULL DEFAULT 0,
                accepting_orders INTEGER NOT NULL DEFAULT 0,
                end_date TEXT,
                seconds_to_expiry INTEGER,
                days_to_expiry REAL,
                liquidity_usdc REAL,
                volume_usdc REAL,
                volume_24h_usdc REAL,
                outcomes_json TEXT NOT NULL,
                market_json TEXT NOT NULL,
                last_scanned_at TEXT NOT NULL
            );
            CREATE TABLE market_resolutions (
                market_id TEXT PRIMARY KEY,
                resolved_outcome TEXT,
                resolution_payload_json TEXT NOT NULL,
                resolved_at TEXT NOT NULL
            );
        """)
        for i in range(resolved_yes_count):
            mid = f"y{i}"
            conn.execute("INSERT INTO market_snapshots(market_id, question, outcomes_json, market_json, last_scanned_at) VALUES (?,?,?,?,?)",
                         (mid, "Will Team A vs Team B end in a draw?", "[]", "{}", "2026-05-13T00:00:00Z"))
            conn.execute("INSERT INTO market_resolutions(market_id, resolved_outcome, resolution_payload_json, resolved_at) VALUES (?,?,?, datetime('now', '-3 days'))",
                         (mid, "Yes", "{}"))
        for i in range(resolved_no_count):
            mid = f"n{i}"
            conn.execute("INSERT INTO market_snapshots(market_id, question, outcomes_json, market_json, last_scanned_at) VALUES (?,?,?,?,?)",
                         (mid, "Will Team C vs Team D end in a draw?", "[]", "{}", "2026-05-13T00:00:00Z"))
            conn.execute("INSERT INTO market_resolutions(market_id, resolved_outcome, resolution_payload_json, resolved_at) VALUES (?,?,?, datetime('now', '-3 days'))",
                         (mid, "No", "{}"))
        conn.commit()
        conn.close()
        return path

    def test_returns_base_rate_for_draw_market(self):
        db = self._make_fixture_db(resolved_yes_count=6, resolved_no_count=20)
        try:
            with patch.dict(os.environ, {"POLYMARKET_MVP_DB_PATH": db}):
                ctxs = PolymarketHistoricalAdapter().fetch({
                    "market_id": "x1",
                    "question": "Will Real Madrid vs Atletico end in a draw?",
                })
            _assert_contract(self, ctxs, "polymarket_historical")
            payload = ctxs[0]["normalized_payload_json"]
            self.assertEqual(payload["n"], 26)
            self.assertEqual(payload["yes_count"], 6)
            self.assertAlmostEqual(payload["yes_rate"], 6 / 26, places=3)
            self.assertIn("BASE RATE", ctxs[0]["display_text"])
        finally:
            os.unlink(db)

    def test_skips_when_fewer_than_min_samples(self):
        db = self._make_fixture_db(resolved_yes_count=1, resolved_no_count=2)
        try:
            with patch.dict(os.environ, {"POLYMARKET_MVP_DB_PATH": db}):
                ctxs = PolymarketHistoricalAdapter().fetch({
                    "market_id": "x1",
                    "question": "Will A vs B end in a draw?",
                })
            self.assertEqual(ctxs, [])
        finally:
            os.unlink(db)

    def test_skips_when_keyword_unrecognized(self):
        db = self._make_fixture_db(resolved_yes_count=10, resolved_no_count=10)
        try:
            with patch.dict(os.environ, {"POLYMARKET_MVP_DB_PATH": db}):
                # Question without any of the draw/win/O.U/btts keywords
                ctxs = PolymarketHistoricalAdapter().fetch({
                    "market_id": "x1",
                    "question": "Generic untracked event question",
                })
            self.assertEqual(ctxs, [])
        finally:
            os.unlink(db)


class GdeltAdapterTests(unittest.TestCase):
    def _political_market(self):
        return {
            "market_id": "p1",
            "question": "Will the US Congress pass HR 1234 by 2026-06-01?",
            "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}],
        }

    def test_returns_articles_for_political_market(self):
        adapter = GdeltAdapter(session=Mock())
        adapter.session.get.return_value = Mock(
            status_code=200,
            raise_for_status=Mock(),
            json=lambda: {
                "articles": [
                    {
                        "url": "https://example.com/article1",
                        "title": "Congress debates HR 1234",
                        "seendate": "20260513T120000Z",
                        "domain": "example.com",
                    },
                    {
                        "url": "https://news.com/2",
                        "title": "Senate vote scheduled",
                        "seendate": "20260513T100000Z",
                        "domain": "news.com",
                    },
                ]
            },
        )
        with patch("polymarket_mvp.event_fetcher.classify_market_class", create=True, return_value="politics"):
            from polymarket_mvp.services import event_cluster_service
            with patch.object(event_cluster_service, "classify_market_class", return_value="politics"):
                ctxs = adapter.fetch(self._political_market())
        _assert_contract(self, ctxs, "gdelt")
        self.assertEqual(len(ctxs), 2)

    def test_skips_sports_markets(self):
        adapter = GdeltAdapter(session=Mock())
        from polymarket_mvp.services import event_cluster_service
        with patch.object(event_cluster_service, "classify_market_class", return_value="sports_winner"):
            ctxs = adapter.fetch({"market_id": "s1", "question": "Real Madrid vs Barca draw?"})
        self.assertEqual(ctxs, [])
        adapter.session.get.assert_not_called()


class TavilyAdapterTests(unittest.TestCase):
    def _market(self):
        return {
            "market_id": "t1",
            "question": "Will Apple release Vision Pro 2 before 2026-07-01?",
            "end_date": "2026-07-01T00:00:00Z",
            "outcomes": [{"name": "Yes", "price": 0.35}, {"name": "No", "price": 0.65}],
        }

    def test_skips_without_api_key(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": ""}, clear=False):
            ctxs = TavilyAdapter().fetch(self._market())
        self.assertEqual(ctxs, [])

    def test_returns_answer_and_results(self):
        from polymarket_mvp.db import init_db, connect_db
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        db_path = tmp.name
        tmp.close()
        try:
            os.unlink(db_path)  # let init_db create fresh
            init_db(Path(db_path))
            adapter = TavilyAdapter(session=Mock())
            adapter.session.post.return_value = Mock(
                status_code=200,
                raise_for_status=Mock(),
                json=lambda: {
                    "answer": "Vision Pro 2 has not been announced yet.",
                    "results": [
                        {"title": "Apple roadmap leak", "content": "snippet1", "url": "https://x.com/a", "score": 0.9},
                        {"title": "Bloomberg report", "content": "snippet2", "url": "https://x.com/b", "score": 0.8},
                    ],
                },
            )
            with patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test", "POLYMARKET_MVP_DB_PATH": db_path}):
                ctxs = adapter.fetch(self._market())
                _assert_contract(self, ctxs, "tavily")
                # 1 answer + 2 results
                self.assertEqual(len(ctxs), 3)
                with connect_db() as conn:
                    from polymarket_mvp.db import adapter_budget_calls_today
                    self.assertEqual(adapter_budget_calls_today(conn, "tavily"), 1)
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_enforces_daily_cap(self):
        from polymarket_mvp.db import init_db, connect_db, adapter_budget_increment
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        db_path = tmp.name
        tmp.close()
        try:
            os.unlink(db_path)
            init_db(Path(db_path))
            adapter = TavilyAdapter(session=Mock())
            with patch.dict(os.environ, {
                "TAVILY_API_KEY": "tvly-test",
                "TAVILY_DAILY_CAP": "5",
                "POLYMARKET_MVP_DB_PATH": db_path,
            }):
                # Pre-populate budget to the cap
                with connect_db() as conn:
                    for _ in range(5):
                        adapter_budget_increment(conn, "tavily")
                    conn.commit()
                ctxs = adapter.fetch(self._market())
                self.assertEqual(ctxs, [])
                adapter.session.post.assert_not_called()
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)


class TheOddsApiAdapterTests(unittest.TestCase):
    def _market_match(self):
        return {
            "market_id": "o1",
            "question": "Will Real Madrid vs FC Barcelona end in a draw?",
            "market_json": {"category": "soccer"},
            "outcomes": [{"name": "Yes", "price": 0.24}, {"name": "No", "price": 0.76}],
        }

    def test_skips_without_api_key(self):
        with patch.dict(os.environ, {"ODDS_API_KEY": ""}, clear=False):
            ctxs = TheOddsApiAdapter().fetch(self._market_match())
        self.assertEqual(ctxs, [])

    def test_returns_consensus_for_matched_event(self):
        from polymarket_mvp.db import init_db, connect_db
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        db_path = tmp.name
        tmp.close()
        try:
            os.unlink(db_path)
            init_db(Path(db_path))
            adapter = TheOddsApiAdapter(session=Mock())
            adapter.session.get.return_value = Mock(
                status_code=200,
                raise_for_status=Mock(),
                json=lambda: [
                    {
                        "id": "match-1",
                        "home_team": "Real Madrid",
                        "away_team": "FC Barcelona",
                        "commence_time": "2026-05-14T20:00:00Z",
                        "bookmakers": [
                            {
                                "key": "bet365",
                                "markets": [
                                    {
                                        "key": "h2h",
                                        "outcomes": [
                                            {"name": "Real Madrid", "price": 2.0},
                                            {"name": "Draw", "price": 4.0},
                                            {"name": "FC Barcelona", "price": 3.5},
                                        ],
                                    }
                                ],
                            },
                            {
                                "key": "pinnacle",
                                "markets": [
                                    {
                                        "key": "h2h",
                                        "outcomes": [
                                            {"name": "Real Madrid", "price": 2.1},
                                            {"name": "Draw", "price": 4.2},
                                            {"name": "FC Barcelona", "price": 3.4},
                                        ],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            )
            from polymarket_mvp.services import event_cluster_service
            with patch.object(event_cluster_service, "classify_market_class", return_value="sports_winner"), \
                 patch.dict(os.environ, {"ODDS_API_KEY": "odds-test", "POLYMARKET_MVP_DB_PATH": db_path}):
                ctxs = adapter.fetch(self._market_match())
            _assert_contract(self, ctxs, "odds_api")
            self.assertEqual(len(ctxs), 1)
            payload = ctxs[0]["normalized_payload_json"]
            self.assertIn("h2h", payload["consensus"])
            # Normalized h2h probabilities should sum to ~1.0
            self.assertAlmostEqual(sum(payload["consensus"]["h2h"].values()), 1.0, places=2)
            self.assertEqual(payload["n_books"], 2)
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_skips_when_sport_unknown(self):
        adapter = TheOddsApiAdapter(session=Mock())
        from polymarket_mvp.services import event_cluster_service
        with patch.object(event_cluster_service, "classify_market_class", return_value="sports_winner"), \
             patch.dict(os.environ, {"ODDS_API_KEY": "odds-test"}):
            ctxs = adapter.fetch({
                "market_id": "u1",
                "question": "Will The Mongolz beat Aurora Gaming?",
                "market_json": {},
            })
        self.assertEqual(ctxs, [])
        adapter.session.get.assert_not_called()


class RedditAdapterTests(unittest.TestCase):
    def test_skips_without_credentials(self):
        with patch.dict(os.environ, {"REDDIT_CLIENT_ID": "", "REDDIT_CLIENT_SECRET": ""}, clear=False):
            ctxs = RedditAdapter().fetch({"market_id": "r1", "question": "test"})
        self.assertEqual(ctxs, [])


class ProviderNamesTests(unittest.TestCase):
    def test_defaults_include_always_on_free_adapters(self):
        with patch.dict(os.environ, {
            "FOOTBALL_DATA_API_KEY": "",
            "PERPLEXITY_API_KEY": "",
            "TAVILY_API_KEY": "",
            "ODDS_API_KEY": "",
            "REDDIT_CLIENT_ID": "",
            "REDDIT_CLIENT_SECRET": "",
            "CRYPTOPANIC_AUTH_TOKEN": "",
        }, clear=False):
            names = provider_names(None)
        self.assertIn("polymarket_historical", names)
        self.assertIn("gdelt", names)
        self.assertIn("web_search", names)
        # cryptopanic now gated on token — used to spam soft_fail stubs without one
        self.assertNotIn("cryptopanic", names)
        self.assertNotIn("tavily", names)
        self.assertNotIn("odds_api", names)
        self.assertNotIn("perplexity", names)
        self.assertNotIn("reddit", names)

    def test_paid_adapters_enabled_when_keys_set(self):
        with patch.dict(os.environ, {
            "FOOTBALL_DATA_API_KEY": "x",
            "PERPLEXITY_API_KEY": "y",
            "TAVILY_API_KEY": "z",
            "ODDS_API_KEY": "w",
            "REDDIT_CLIENT_ID": "rc",
            "REDDIT_CLIENT_SECRET": "rs",
        }, clear=False):
            names = provider_names(None)
        for adapter in ("sports_data", "perplexity", "tavily", "odds_api", "reddit"):
            self.assertIn(adapter, names)


class ComposeContextPayloadTests(unittest.TestCase):
    def test_prefix_mapping_for_all_new_sources(self):
        market = {"market_id": "m1", "question": "Test"}
        contexts = [
            {"source_type": "polymarket_historical", "display_text": "HISTORICAL: yes 6/26 = 23%", "importance_weight": 1.4, "published_at": "2026-05-13T00:00:00Z"},
            {"source_type": "gdelt", "display_text": "GDELT: news article", "importance_weight": 0.85, "published_at": "2026-05-13T00:00:00Z"},
            {"source_type": "tavily", "display_text": "TAVILY: synthesized answer", "importance_weight": 1.1, "published_at": "2026-05-13T00:00:00Z"},
            {"source_type": "odds_api", "display_text": "BOOKMAKERS: h2h 0.55/0.25/0.20", "importance_weight": 1.3, "published_at": "2026-05-13T00:00:00Z"},
            {"source_type": "reddit", "display_text": "REDDIT: r/sportsbook hot post", "importance_weight": 0.6, "published_at": "2026-05-13T00:00:00Z"},
        ]
        result = compose_context_payload(market, contexts, max_chars=2000)
        # All five new sources should be in the assembled text
        text = result["assembled_text"]
        self.assertIn("HISTORICAL:", text)
        self.assertIn("GDELT:", text)
        self.assertIn("TAVILY:", text)
        self.assertIn("BOOKMAKERS:", text)
        self.assertIn("REDDIT:", text)
        # Ordering: odds_api first (rank 0), then sports_data ... reddit last
        # We have no sports_data; so order is odds_api > historical > gdelt > tavily > reddit
        positions = {k: text.find(prefix) for k, prefix in [
            ("odds_api", "BOOKMAKERS:"),
            ("polymarket_historical", "HISTORICAL:"),
            ("gdelt", "GDELT:"),
            ("tavily", "TAVILY:"),
            ("reddit", "REDDIT:"),
        ]}
        ordered_positions = [positions[k] for k in ("odds_api", "polymarket_historical", "gdelt", "tavily", "reddit")]
        self.assertEqual(ordered_positions, sorted(ordered_positions))


if __name__ == "__main__":
    unittest.main()
