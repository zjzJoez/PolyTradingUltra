"""Unit tests for clubelo_signal — strategy D from the 2026-05-15 deep dive.

Covers: team-name normalization, three-way probability math, market parsing,
end-to-end signal_for_market with mocked ClubElo data."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from polymarket_mvp.services.clubelo_signal import (
    DEFAULT_HOME_ADVANTAGE,
    DRAW_MAX,
    EloSignal,
    _build_normalized_index,
    _parse_question,
    compute_three_way_probs,
    normalize_team,
    signal_for_market,
)


class NormalizeTeamTests(unittest.TestCase):
    def test_strips_accents_and_boilerplate(self):
        # normalize_team strips: fc/cf/sc/ac/as/sv/sk/tsg/vfl/vfb/ff/cd/club/
        # football/the/de/del/da/do/dos/das/cp. NOT "deportivo" / "real" / "sport"
        # — those carry signal for distinguishing (e.g. Real Madrid vs Atletico
        # Madrid). The substring/alias fallback in lookup_elo handles the rest.
        cases = [
            ("FC Barcelona", "barcelona"),
            ("Deportivo Alavés", "deportivo alaves"),
            ("Club Atlético de Madrid", "atletico madrid"),
            ("Real Madrid", "real madrid"),
            ("Manchester City FC", "manchester city"),
            ("RCD Mallorca", "rcd mallorca"),  # RCD not in boilerplate list — kept
            ("FC Slovan Liberec", "slovan liberec"),
            ("Paris Saint-Germain FC", "paris saint germain"),
        ]
        for raw, expected in cases:
            self.assertEqual(normalize_team(raw), expected, f"failed: {raw}")

    def test_empty_input_returns_empty(self):
        self.assertEqual(normalize_team(""), "")
        self.assertEqual(normalize_team(None), "")


class BuildNormalizedIndexTests(unittest.TestCase):
    def test_indexes_clubs_by_normalized_name(self):
        rows = [
            {"club": "Barcelona", "country": "ESP", "level": "1", "elo": 1969.0},
            {"club": "Real Madrid", "country": "ESP", "level": "1", "elo": 1918.0},
            {"club": "Alaves", "country": "ESP", "level": "1", "elo": 1636.0},
        ]
        index = _build_normalized_index(rows)
        self.assertEqual(index["barcelona"]["elo"], 1969.0)
        self.assertEqual(index["real madrid"]["elo"], 1918.0)
        self.assertEqual(index["alaves"]["elo"], 1636.0)

    def test_first_wins_on_duplicate_club_name(self):
        # Two rows with the SAME club name (rare in practice — ClubElo dedupes
        # by club name). Current behaviour: first wins.
        rows = [
            {"club": "Real Madrid", "country": "ESP", "level": "1", "elo": 1918.0},
            {"club": "Real Madrid", "country": "USA", "level": "1", "elo": 1500.0},
        ]
        index = _build_normalized_index(rows)
        self.assertEqual(index["real madrid"]["country"], "ESP")
        self.assertEqual(index["real madrid"]["elo"], 1918.0)

    def test_genuine_ambiguous_collision_marks_sentinel(self):
        # Two DIFFERENT clubs whose names normalize to the same key — this
        # should be flagged so we don't pick one at random.
        rows = [
            {"club": "Athletic Bilbao", "country": "ESP", "level": "1", "elo": 1700.0},
            {"club": "Athletic Bilbao FC", "country": "ESP", "level": "1", "elo": 1700.0},
        ]
        # These would normalize identically. Use names that genuinely differ:
        rows = [
            {"club": "Sporting CP", "country": "POR", "level": "1", "elo": 1750.0},
            {"club": "Sporting", "country": "POR", "level": "1", "elo": 1740.0},
        ]
        index = _build_normalized_index(rows)
        key = "sporting"  # both normalize here ("cp" stripped by boilerplate)
        self.assertTrue(index[key].get("_ambiguous"))


class ComputeThreeWayProbsTests(unittest.TestCase):
    def test_strong_home_favorite(self):
        # Barcelona (1969) home vs Alaves (1636) — Barca should be heavy fav.
        # Pass explicit params so test isn't fragile to default tuning.
        probs = compute_three_way_probs(
            elo_home=1969, elo_away=1636,
            home_advantage=65.0, logistic_divisor=400.0,
        )
        self.assertGreater(probs["home"], 0.65)
        self.assertLess(probs["away"], 0.15)
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=5)

    def test_strong_underdog_at_home(self):
        # Alaves (1636) HOME vs Barca (1969) — with explicit home_advantage,
        # Alaves's win prob is higher when at home than when away.
        probs_alaves_home = compute_three_way_probs(
            elo_home=1636, elo_away=1969,
            home_advantage=65.0, logistic_divisor=400.0,
        )
        probs_alaves_away = compute_three_way_probs(
            elo_home=1969, elo_away=1636,
            home_advantage=65.0, logistic_divisor=400.0,
        )
        self.assertGreater(probs_alaves_home["home"], probs_alaves_away["away"])

    def test_equal_teams(self):
        # Same ELO → home favored only by explicit home_advantage
        probs = compute_three_way_probs(
            elo_home=1700, elo_away=1700,
            home_advantage=65.0, logistic_divisor=400.0,
        )
        # With home_advantage=65, home should win more often than away
        self.assertGreater(probs["home"], probs["away"])
        # Equal teams → high draw rate
        self.assertGreater(probs["draw"], 0.2)
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=5)

    def test_default_home_advantage_is_zero(self):
        # Verify the default behavior: equal teams → roughly symmetric
        probs = compute_three_way_probs(elo_home=1700, elo_away=1700)
        self.assertAlmostEqual(probs["home"], probs["away"], places=3)
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=5)

    def test_large_gap_reduces_draw(self):
        # Bayern (top of table) vs lower-tier should have low draw rate
        probs_close = compute_three_way_probs(elo_home=1700, elo_away=1700)
        probs_huge_gap = compute_three_way_probs(elo_home=2050, elo_away=1300)
        self.assertGreater(probs_close["draw"], probs_huge_gap["draw"])
        self.assertGreaterEqual(probs_huge_gap["draw"], 0.10)  # floor


class ParseQuestionTests(unittest.TestCase):
    def test_draw_question(self):
        result = _parse_question("Will Deportivo Alavés vs. FC Barcelona end in a draw?")
        self.assertIsNotNone(result)
        a, b, side = result
        self.assertIn("alav", a.lower())
        self.assertIn("barcelona", b.lower())
        self.assertEqual(side, "draw")

    def test_beat_question(self):
        result = _parse_question("Will Real Madrid beat FC Barcelona on 2026-05-15?")
        self.assertIsNotNone(result)
        a, b, side = result
        self.assertIn("madrid", a.lower())
        self.assertEqual(side, "home_win")

    def test_single_team_win_question_returns_partial(self):
        # "Will X win on YYYY-MM-DD?" — opponent is None, looked up by caller
        result = _parse_question("Will Lens win on 2026-05-13?")
        self.assertIsNotNone(result)
        a, b, side = result
        self.assertEqual(a, "lens")
        self.assertIsNone(b)
        self.assertEqual(side, "home_win")

    def test_unsupported_questions_return_none(self):
        # O/U markets — different shape
        self.assertIsNone(_parse_question("Real Madrid vs. Barcelona: O/U 2.5"))
        # Empty
        self.assertIsNone(_parse_question(""))


class SignalForMarketTests(unittest.TestCase):
    def _patch_elo(self, club_to_elo):
        """Patch the snapshot loader to return the given clubs. Lets the real
        lookup_elo path (with substring + alias fallback) exercise."""
        def fake_load(**kwargs):
            return [
                {"club": club, "country": "XX", "level": "1", "elo": float(elo)}
                for club, elo in club_to_elo.items()
            ]
        return patch("polymarket_mvp.services.clubelo_signal._load_snapshot", side_effect=fake_load)

    def test_draw_market_with_strong_favorite(self):
        # Alaves home vs Barca away, market priced draw at 0.245 (Polymarket actual price)
        market = {
            "market_id": "2123236",
            "question": "Will Deportivo Alavés vs. FC Barcelona end in a draw?",
            "outcomes": [{"name": "Yes", "price": 0.245}, {"name": "No", "price": 0.755}],
        }
        with self._patch_elo({"Alaves": 1636.0, "Barcelona": 1969.0}):
            signal = signal_for_market(market, edge_threshold=0.05)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "draw")
        # With Elo gap of 333 + home_adv 65 → P(draw) somewhere around 0.20
        # Market p = 0.245 → market overestimates draw → edge negative
        self.assertLess(signal.model_p, signal.market_p)
        self.assertEqual(signal.recommendation, "skip")
        self.assertLess(signal.edge, 0)

    def test_beat_market_with_positive_edge(self):
        # Hypothetical: Real Madrid heavily favored, market underestimates them
        market = {
            "market_id": "test_beat",
            "question": "Will Real Madrid beat Mallorca on 2026-05-15?",
            "outcomes": [{"name": "Yes", "price": 0.55}, {"name": "No", "price": 0.45}],
        }
        with self._patch_elo({"Real Madrid": 1918.0, "Mallorca": 1500.0}):
            signal = signal_for_market(market, edge_threshold=0.05)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "home_win")
        # 1918 + 65 home_adv = 1983 vs 1500 → ELO logistic strongly favors home
        # Three-way → P(home) should be well above 0.55
        self.assertGreater(signal.model_p, 0.60)
        self.assertGreater(signal.edge, 0.05)
        self.assertEqual(signal.recommendation, "bet")

    def test_unknown_team_returns_no_match(self):
        market = {
            "market_id": "x",
            "question": "Will Unknown Club FC beat Mystery United on 2026-05-15?",
            "outcomes": [{"name": "Yes", "price": 0.5}],
        }
        with self._patch_elo({}):
            signal = signal_for_market(market, edge_threshold=0.05)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.recommendation, "no_match")

    def test_unsupported_question_returns_none(self):
        market = {
            "market_id": "x",
            "question": "Will the highest temperature in Austin be 75°F?",
            "outcomes": [{"name": "Yes", "price": 0.4}],
        }
        with self._patch_elo({}):
            signal = signal_for_market(market)
        self.assertIsNone(signal)


if __name__ == "__main__":
    unittest.main()
