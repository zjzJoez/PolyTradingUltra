"""Tests for Strategy B — Odds API divergence detector."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from polymarket_mvp.services.odds_divergence_signal import (
    _average_consensus,
    _match_event,
    _parse_ou_question,
    _polymarket_yes_price,
    _resolve_sport_key,
    signal_for_market,
)


class ParseOuQuestionTests(unittest.TestCase):
    def test_recognizes_ou_format(self):
        self.assertEqual(
            _parse_ou_question("Real Madrid vs. Barcelona: O/U 2.5"),
            ("Real Madrid", "Barcelona", 2.5),
        )
        self.assertEqual(
            _parse_ou_question("Yankees vs. Red Sox: O/U 8.5"),
            ("Yankees", "Red Sox", 8.5),
        )

    def test_rejects_non_ou(self):
        self.assertIsNone(_parse_ou_question("Will Lens win on 2026-05-13?"))
        self.assertIsNone(_parse_ou_question("Real Madrid vs. Barcelona: BTTS"))


class ResolveSportKeyTests(unittest.TestCase):
    def test_la_liga_match(self):
        market = {"question": "Will Deportivo Alavés vs. FC Barcelona end in a draw?"}
        self.assertEqual(_resolve_sport_key(market), "soccer_spain_la_liga")

    def test_unknown_league_returns_none(self):
        market = {"question": "Will Henan FC vs. Shenzhen FC end in a draw?"}
        self.assertIsNone(_resolve_sport_key(market))


class MatchEventTests(unittest.TestCase):
    def test_matches_h2h_event(self):
        events = [
            {"home_team": "Real Madrid", "away_team": "FC Barcelona", "id": "x1"},
            {"home_team": "Atlético Madrid", "away_team": "Valencia", "id": "x2"},
        ]
        match = _match_event(events, {}, "Real Madrid", "FC Barcelona")
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], "x1")

    def test_matches_event_when_team_order_reversed(self):
        events = [{"home_team": "Barcelona", "away_team": "Real Madrid", "id": "x1"}]
        match = _match_event(events, {}, "Real Madrid", "FC Barcelona")
        self.assertEqual(match["id"], "x1")

    def test_no_match_returns_none(self):
        events = [{"home_team": "Bayern", "away_team": "Dortmund", "id": "x1"}]
        match = _match_event(events, {}, "Real Madrid", "FC Barcelona")
        self.assertIsNone(match)


class AverageConsensusTests(unittest.TestCase):
    def _build_event(self):
        return {
            "home_team": "Real Madrid",
            "away_team": "FC Barcelona",
            "bookmakers": [
                {"key": "pinnacle", "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Real Madrid", "price": 2.0},
                        {"name": "Draw", "price": 4.0},
                        {"name": "FC Barcelona", "price": 3.5},
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over", "point": 2.5, "price": 1.8},
                        {"name": "Under", "point": 2.5, "price": 2.0},
                    ]},
                ]},
                {"key": "bet365", "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Real Madrid", "price": 2.1},
                        {"name": "Draw", "price": 3.9},
                        {"name": "FC Barcelona", "price": 3.4},
                    ]},
                ]},
            ],
        }

    def test_h2h_normalizes_to_sum_one(self):
        consensus = _average_consensus(self._build_event())
        self.assertIn("h2h", consensus)
        self.assertAlmostEqual(sum(consensus["h2h"].values()), 1.0, places=4)
        self.assertEqual(consensus["book_count"], 2)
        self.assertGreater(consensus["h2h"]["Real Madrid"], consensus["h2h"]["Draw"])

    def test_totals_normalized_per_line(self):
        consensus = _average_consensus(self._build_event())
        self.assertIn("totals", consensus)
        line_data = consensus["totals"].get("2.5") or {}
        self.assertAlmostEqual(sum(line_data.values()), 1.0, places=4)


class PolymarketYesPriceTests(unittest.TestCase):
    def test_yes_outcome(self):
        market = {"outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]}
        self.assertAlmostEqual(_polymarket_yes_price(market), 0.42)

    def test_over_fallback(self):
        market = {"outcomes": [{"name": "Over", "price": 0.51}, {"name": "Under", "price": 0.49}]}
        self.assertAlmostEqual(_polymarket_yes_price(market), 0.51)


class SignalForMarketTests(unittest.TestCase):
    def _odds_response_with_draw(self):
        return [{
            "id": "evt-1",
            "home_team": "Deportivo Alavés",
            "away_team": "FC Barcelona",
            "bookmakers": [
                {"key": "pin", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Deportivo Alavés", "price": 5.0},
                    {"name": "Draw", "price": 3.6},
                    {"name": "FC Barcelona", "price": 1.7},
                ]}]},
                {"key": "bet", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Deportivo Alavés", "price": 5.5},
                    {"name": "Draw", "price": 3.5},
                    {"name": "FC Barcelona", "price": 1.65},
                ]}]},
            ],
        }]

    def test_bet_when_polymarket_under_prices_draw(self):
        from polymarket_mvp.db import init_db
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        db_path = tmp.name
        tmp.close()
        try:
            os.unlink(db_path)
            init_db(Path(db_path))
            session = Mock()
            session.get.return_value = Mock(
                status_code=200, raise_for_status=Mock(),
                json=lambda: self._odds_response_with_draw(),
            )
            market = {
                "market_id": "test1",
                "question": "Will Deportivo Alavés vs. FC Barcelona end in a draw?",
                "market_json": {},
                "outcomes": [{"name": "Yes", "price": 0.18}, {"name": "No", "price": 0.82}],
            }
            with patch.dict(os.environ, {"ODDS_API_KEY": "test", "POLYMARKET_MVP_DB_PATH": db_path}):
                signal = signal_for_market(market, edge_threshold=0.03, session=session)
            self.assertIsNotNone(signal)
            self.assertEqual(signal.recommendation, "bet")
            self.assertEqual(signal.side, "draw")
            self.assertEqual(signal.outcome, "Yes")
            self.assertGreater(signal.edge, 0.05)
            self.assertEqual(signal.book_count, 2)
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_skip_when_polymarket_aligned_with_consensus(self):
        from polymarket_mvp.db import init_db
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        db_path = tmp.name
        tmp.close()
        try:
            os.unlink(db_path)
            init_db(Path(db_path))
            session = Mock()
            session.get.return_value = Mock(
                status_code=200, raise_for_status=Mock(),
                json=lambda: self._odds_response_with_draw(),
            )
            market = {
                "market_id": "test2",
                "question": "Will Deportivo Alavés vs. FC Barcelona end in a draw?",
                "market_json": {},
                "outcomes": [{"name": "Yes", "price": 0.28}, {"name": "No", "price": 0.72}],
            }
            with patch.dict(os.environ, {"ODDS_API_KEY": "test", "POLYMARKET_MVP_DB_PATH": db_path}):
                signal = signal_for_market(market, edge_threshold=0.03, session=session)
            self.assertEqual(signal.recommendation, "skip")
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_skips_without_api_key(self):
        with patch.dict(os.environ, {"ODDS_API_KEY": ""}, clear=False):
            signal = signal_for_market(
                {"market_id": "x", "question": "Will FC Barcelona vs. Real Madrid end in a draw?",
                 "market_json": {}, "outcomes": [{"name": "Yes", "price": 0.2}]},
            )
        self.assertIsNone(signal)


if __name__ == "__main__":
    unittest.main()
