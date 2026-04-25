from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from polymarket_mvp.strategy.conviction import (
    BASE_TIER_SIZES,
    TIER_EXTREME,
    TIER_HIGH,
    TIER_MEDIUM,
    TIER_SPECULATIVE,
    account_scale,
    compute_tier,
    compute_tier_size,
    downgrade_tier,
    portfolio_exposure_cap,
)


class ComputeTierTests(unittest.TestCase):
    def test_below_skip_returns_none(self):
        self.assertIsNone(
            compute_tier(
                confidence=0.52, market_price=0.50,
                catalyst_clarity="strong", downside_risk="limited",
            )
        )

    def test_speculative_for_small_edge(self):
        self.assertEqual(
            compute_tier(
                confidence=0.58, market_price=0.50,
                catalyst_clarity="weak", downside_risk="moderate",
            ),
            TIER_SPECULATIVE,
        )

    def test_medium_requires_moderate_or_strong_clarity(self):
        # edge = 0.15, clarity=none → falls back to speculative
        self.assertEqual(
            compute_tier(
                confidence=0.65, market_price=0.50,
                catalyst_clarity="none", downside_risk="limited",
            ),
            TIER_SPECULATIVE,
        )
        # edge = 0.15, clarity=moderate → medium
        self.assertEqual(
            compute_tier(
                confidence=0.65, market_price=0.50,
                catalyst_clarity="moderate", downside_risk="limited",
            ),
            TIER_MEDIUM,
        )

    def test_high_requires_strong_clarity(self):
        # edge = 0.25, clarity=moderate → medium (not high)
        self.assertEqual(
            compute_tier(
                confidence=0.75, market_price=0.50,
                catalyst_clarity="moderate", downside_risk="limited",
            ),
            TIER_MEDIUM,
        )
        # edge = 0.25, clarity=strong → high
        self.assertEqual(
            compute_tier(
                confidence=0.75, market_price=0.50,
                catalyst_clarity="strong", downside_risk="limited",
            ),
            TIER_HIGH,
        )

    def test_extreme_requires_strong_clarity_and_non_substantial_downside(self):
        # edge = 0.35, strong + substantial → high (not extreme)
        self.assertEqual(
            compute_tier(
                confidence=0.85, market_price=0.50,
                catalyst_clarity="strong", downside_risk="substantial",
            ),
            TIER_HIGH,
        )
        # edge = 0.35, strong + limited → extreme
        self.assertEqual(
            compute_tier(
                confidence=0.85, market_price=0.50,
                catalyst_clarity="strong", downside_risk="limited",
            ),
            TIER_EXTREME,
        )

    def test_reverse_edge_also_works(self):
        # NO tail: confidence below market price
        self.assertEqual(
            compute_tier(
                confidence=0.20, market_price=0.50,
                catalyst_clarity="strong", downside_risk="limited",
            ),
            TIER_EXTREME,
        )

    def test_rejects_out_of_range_inputs(self):
        self.assertIsNone(compute_tier(
            confidence=1.5, market_price=0.50,
            catalyst_clarity="strong", downside_risk="limited",
        ))
        self.assertIsNone(compute_tier(
            confidence=0.5, market_price=0.0,
            catalyst_clarity="strong", downside_risk="limited",
        ))


class TierSizeTests(unittest.TestCase):
    def setUp(self):
        self._env_backup = dict(os.environ)
        os.environ.pop("POLY_ACCOUNT_BALANCE_USDC", None)
        os.environ.pop("POLY_TIER_SCALE_MAX", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_base_sizes_at_50_balance(self):
        self.assertEqual(compute_tier_size(TIER_SPECULATIVE, 50.0), 2.0)
        self.assertEqual(compute_tier_size(TIER_MEDIUM, 50.0), 4.0)
        self.assertEqual(compute_tier_size(TIER_HIGH, 50.0), 8.0)
        self.assertEqual(compute_tier_size(TIER_EXTREME, 50.0), 15.0)

    def test_scaling_doubles_at_100_balance(self):
        self.assertEqual(account_scale(100.0), 2.0)
        self.assertEqual(compute_tier_size(TIER_SPECULATIVE, 100.0), 4.0)
        self.assertEqual(compute_tier_size(TIER_EXTREME, 100.0), 30.0)

    def test_scaling_quadruples_at_200_balance(self):
        self.assertEqual(account_scale(200.0), 4.0)
        self.assertEqual(compute_tier_size(TIER_EXTREME, 200.0), 60.0)

    def test_scaling_capped_by_env(self):
        os.environ["POLY_TIER_SCALE_MAX"] = "4.0"
        self.assertEqual(account_scale(10000.0), 4.0)

    def test_scaling_floored_at_1_below_baseline(self):
        self.assertEqual(account_scale(25.0), 1.0)
        self.assertEqual(compute_tier_size(TIER_EXTREME, 25.0), 15.0)

    def test_env_balance_fallback(self):
        os.environ["POLY_ACCOUNT_BALANCE_USDC"] = "100"
        self.assertEqual(compute_tier_size(TIER_HIGH), 16.0)

    def test_unknown_tier_returns_zero(self):
        self.assertEqual(compute_tier_size("bogus", 50.0), 0.0)


class DowngradeAndExposureTests(unittest.TestCase):
    def test_downgrade_chain(self):
        self.assertEqual(downgrade_tier(TIER_EXTREME), TIER_HIGH)
        self.assertEqual(downgrade_tier(TIER_HIGH), TIER_MEDIUM)
        self.assertEqual(downgrade_tier(TIER_MEDIUM), TIER_SPECULATIVE)
        self.assertIsNone(downgrade_tier(TIER_SPECULATIVE))
        self.assertIsNone(downgrade_tier("unknown"))

    def test_portfolio_exposure_cap_at_50(self):
        with patch.dict(os.environ, {"POLY_ACCOUNT_BALANCE_USDC": "50"}, clear=False):
            self.assertEqual(portfolio_exposure_cap(), 35.0)

    def test_portfolio_exposure_cap_at_200(self):
        self.assertEqual(portfolio_exposure_cap(200.0), 140.0)


if __name__ == "__main__":
    unittest.main()
