"""Conviction-tier sizing — replaces Kelly for small accounts.

LLM outputs objective fields (confidence, catalyst_clarity, downside_risk);
code deterministically maps to a tier and a USDC size. This avoids LLM
self-calibration error on "which tier is this?"
"""
from __future__ import annotations

import math
import os
from typing import Optional


TIER_SPECULATIVE = "speculative"
TIER_MEDIUM = "medium"
TIER_HIGH = "high"
TIER_EXTREME = "extreme"
TIER_ORDER = (TIER_SPECULATIVE, TIER_MEDIUM, TIER_HIGH, TIER_EXTREME)


BASE_TIER_SIZES: dict[str, float] = {
    TIER_SPECULATIVE: 2.0,
    TIER_MEDIUM: 4.0,
    TIER_HIGH: 8.0,
    TIER_EXTREME: 15.0,
}


# Max concurrent open positions per tier. Enforces portfolio diversification.
CONCURRENT_CAPS: dict[str, int] = {
    TIER_SPECULATIVE: 5,
    TIER_MEDIUM: 4,
    TIER_HIGH: 3,
    TIER_EXTREME: 2,
}

# Total exposure cap as fraction of balance (70% → keep 30% cash buffer).
PORTFOLIO_BALANCE_EXPOSURE_FRACTION: float = 0.70

# Default baseline balance the tier ladder was calibrated against.
_BASELINE_BALANCE_USDC: float = 50.0

_CATALYST_STRONG = {"strong"}
_CATALYST_MODERATE_OR_STRONG = {"moderate", "strong"}
_DOWNSIDE_BLOCKING = {"substantial"}

_EDGE_SKIP = 0.05
_EDGE_SPECULATIVE = 0.10
_EDGE_MEDIUM = 0.20
_EDGE_HIGH = 0.30


def tier_rank(tier: str) -> int:
    """Higher rank = stronger conviction. Unknown tiers rank -1."""
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        return -1


def downgrade_tier(tier: str) -> Optional[str]:
    """Return next tier down, or None if already at speculative (should skip)."""
    rank = tier_rank(tier)
    if rank <= 0:
        return None
    return TIER_ORDER[rank - 1]


def compute_tier(
    *,
    confidence: float,
    market_price: float,
    catalyst_clarity: str,
    downside_risk: str,
) -> Optional[str]:
    """Deterministic tier mapping from LLM-provided objective fields.

    Returns None when edge is too thin to justify any bet.
    """
    try:
        c = float(confidence)
        p = float(market_price)
    except (TypeError, ValueError):
        return None
    if not (0.0 < p < 1.0) or not (0.0 <= c <= 1.0):
        return None
    edge = abs(c - p)
    clarity = (catalyst_clarity or "").strip().lower()
    risk = (downside_risk or "").strip().lower()
    if edge < _EDGE_SKIP:
        return None
    if edge < _EDGE_SPECULATIVE:
        return TIER_SPECULATIVE
    if edge < _EDGE_MEDIUM:
        return TIER_MEDIUM if clarity in _CATALYST_MODERATE_OR_STRONG else TIER_SPECULATIVE
    if edge < _EDGE_HIGH:
        return TIER_HIGH if clarity in _CATALYST_STRONG else TIER_MEDIUM
    # edge >= 0.30
    if clarity in _CATALYST_STRONG and risk not in _DOWNSIDE_BLOCKING:
        return TIER_EXTREME
    if clarity in _CATALYST_MODERATE_OR_STRONG:
        return TIER_HIGH
    return TIER_MEDIUM


def account_scale(balance_usdc: Optional[float] = None) -> float:
    """Scale tier sizes as the account grows. Doubles for each doubling of balance.

    balance_usdc source precedence: explicit arg > POLY_ACCOUNT_BALANCE_USDC env > $50.
    Cap at POLY_TIER_SCALE_MAX (default 8x) so a lucky run doesn't blow up sizing.
    """
    if balance_usdc is None:
        raw = os.getenv("POLY_ACCOUNT_BALANCE_USDC")
        try:
            balance_usdc = float(raw) if raw else _BASELINE_BALANCE_USDC
        except ValueError:
            balance_usdc = _BASELINE_BALANCE_USDC
    if balance_usdc <= 0:
        return 1.0
    try:
        scale_max = float(os.getenv("POLY_TIER_SCALE_MAX") or 8.0)
    except ValueError:
        scale_max = 8.0
    ratio = balance_usdc / _BASELINE_BALANCE_USDC
    if ratio < 1.0:
        return 1.0
    exp = int(math.floor(math.log2(ratio)))
    return min(scale_max, float(2 ** exp))


def compute_tier_size(tier: str, balance_usdc: Optional[float] = None) -> float:
    base = BASE_TIER_SIZES.get(tier)
    if base is None:
        return 0.0
    return round(base * account_scale(balance_usdc), 4)


def portfolio_exposure_cap(balance_usdc: Optional[float] = None) -> float:
    """Total open-exposure cap in USDC: fraction × balance."""
    if balance_usdc is None:
        raw = os.getenv("POLY_ACCOUNT_BALANCE_USDC")
        try:
            balance_usdc = float(raw) if raw else _BASELINE_BALANCE_USDC
        except ValueError:
            balance_usdc = _BASELINE_BALANCE_USDC
    return round(balance_usdc * PORTFOLIO_BALANCE_EXPOSURE_FRACTION, 4)
