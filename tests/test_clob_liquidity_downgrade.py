from __future__ import annotations

import unittest
from unittest.mock import patch

from polymarket_mvp.proposer import (
    _enforce_clob_slippage_cap,
    _size_from_conviction,
)


class EnforceClobSlippageCapTests(unittest.TestCase):
    def test_no_downgrade_when_liquidity_ample(self):
        with patch(
            "polymarket_mvp.proposer._clob_top5_liquidity_usdc",
            return_value=500.0,  # 500 USDC depth → cap 125
        ):
            size, tier = _enforce_clob_slippage_cap(
                proposed_size_usdc=15.0,
                tier="extreme",
                token_id="tok",
                market_price=0.15,
                balance_usdc=50.0,
            )
        self.assertEqual(tier, "extreme")
        self.assertEqual(size, 15.0)

    def test_downgrades_when_size_exceeds_25pct_of_depth(self):
        with patch(
            "polymarket_mvp.proposer._clob_top5_liquidity_usdc",
            return_value=20.0,  # depth 20 → cap 5
        ):
            size, tier = _enforce_clob_slippage_cap(
                proposed_size_usdc=15.0,
                tier="extreme",
                token_id="tok",
                market_price=0.15,
                balance_usdc=50.0,
            )
        # extreme=$15 > $5 cap → high=$8 > cap → medium=$4 ≤ cap ✓
        # $4 vs 5-share floor at 0.15: 5×0.15×1.02 = 0.765 ✓
        self.assertEqual(tier, "medium")
        self.assertEqual(size, 4.0)

    def test_returns_unchanged_when_api_unreachable(self):
        with patch(
            "polymarket_mvp.proposer._clob_top5_liquidity_usdc",
            return_value=None,
        ):
            size, tier = _enforce_clob_slippage_cap(
                proposed_size_usdc=15.0,
                tier="extreme",
                token_id="tok",
                market_price=0.15,
                balance_usdc=50.0,
            )
        self.assertEqual(tier, "extreme")
        self.assertEqual(size, 15.0)

    def test_skips_when_all_tiers_violate_cap(self):
        with patch(
            "polymarket_mvp.proposer._clob_top5_liquidity_usdc",
            return_value=1.0,  # depth 1 → cap 0.25
        ):
            size, tier = _enforce_clob_slippage_cap(
                proposed_size_usdc=15.0,
                tier="extreme",
                token_id="tok",
                market_price=0.15,
                balance_usdc=50.0,
            )
        # Even speculative=$2 > $0.25 cap → None
        self.assertIsNone(tier)
        self.assertEqual(size, 0.0)


class SizeFromConvictionTests(unittest.TestCase):
    def test_returns_zero_when_confidence_missing(self):
        size, tier = _size_from_conviction(
            llm_item={},
            market_price=0.30,
            balance_usdc=50.0,
        )
        self.assertEqual(size, 0.0)
        self.assertIsNone(tier)

    def test_downgrades_to_meet_five_share_floor(self):
        # At market_price 0.50, 5-share floor = 5 × 0.50 × 1.02 = 2.55.
        # speculative base $2 < floor → should fall through to None.
        # BUT conviction.compute_tier with edge 0.08 returns speculative.
        # So the result should be (0, None).
        size, tier = _size_from_conviction(
            llm_item={
                "confidence_score": 0.58,
                "catalyst_clarity": "weak",
                "downside_risk": "moderate",
            },
            market_price=0.50,
            balance_usdc=50.0,
        )
        self.assertEqual(size, 0.0)
        self.assertIsNone(tier)

    def test_speculative_at_low_price_clears_floor(self):
        # At 0.15 market price, 5-share floor = 0.765.
        # edge 0.08 → speculative → $2 ≥ 0.765 ✓
        size, tier = _size_from_conviction(
            llm_item={
                "confidence_score": 0.23,
                "catalyst_clarity": "weak",
                "downside_risk": "moderate",
            },
            market_price=0.15,
            balance_usdc=50.0,
        )
        self.assertEqual(tier, "speculative")
        self.assertEqual(size, 2.0)

    def test_extreme_path_with_strong_clarity(self):
        size, tier = _size_from_conviction(
            llm_item={
                "confidence_score": 0.75,
                "catalyst_clarity": "strong",
                "downside_risk": "limited",
            },
            market_price=0.35,
            balance_usdc=50.0,
        )
        # edge 0.40, strong + limited → extreme → $15
        self.assertEqual(tier, "extreme")
        self.assertEqual(size, 15.0)


class ConvictionByIdKeyingTests(unittest.TestCase):
    """Regression: conviction_by_id must be keyed by the *final* proposal_id
    (computed AFTER recommended_size_usdc is rewritten), or run_proposal_pipeline
    will look up the wrong key and persist conviction_tier as NULL.
    """

    def test_conviction_lookup_uses_post_resize_proposal_id(self):
        from unittest.mock import patch
        from polymarket_mvp.proposer import build_openclaw_proposals
        from polymarket_mvp.common import proposal_id_for

        market = {
            "market_id": "m_test",
            "question": "Test market",
            "liquidity_usdc": 5000.0,
            "volume_24h_usdc": 2000.0,
            "days_to_expiry": 3,
            "outcomes": [
                {"name": "Yes", "price": 0.20, "token_id": "t_yes"},
                {"name": "No", "price": 0.80, "token_id": "t_no"},
            ],
        }
        # LLM returns confident YES with size 5.0; sizer should override to
        # speculative tier ($2.0). proposal_id_for the *final* proposal
        # must be the key used in conviction_by_id.
        with patch("polymarket_mvp.proposer.poly_proposer_generate") as mocked, \
             patch("polymarket_mvp.proposer._clob_top5_liquidity_usdc",
                   return_value=1000.0):
            mocked.return_value = [
                {
                    "market_id": "m_test",
                    "outcome": "Yes",
                    "confidence_score": 0.30,
                    "catalyst_clarity": "weak",
                    "downside_risk": "moderate",
                    "asymmetric_target_multiplier": 4.0,
                    "thesis_catalyst_deadline": "2026-05-15",
                    "recommended_size_usdc": 5.0,
                    "reasoning": "long-tail YES",
                    "max_slippage_bps": 400,
                }
            ]
            proposals, _meta, conviction_by_id = build_openclaw_proposals(
                [market], context_file=None, size_usdc=5.0, top=1, max_slippage_bps=500,
            )
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        # Sizer must have downgraded to speculative ($2)
        self.assertEqual(proposal["recommended_size_usdc"], 2.0)
        # The conviction map key must equal the post-resize proposal_id
        final_pid = proposal_id_for(proposal)
        self.assertIn(final_pid, conviction_by_id)
        self.assertEqual(conviction_by_id[final_pid]["conviction_tier"], "speculative")


if __name__ == "__main__":
    unittest.main()
