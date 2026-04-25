from __future__ import annotations

from typing import Any, Dict, List

from ..common import get_env_int, parse_iso8601, utc_now_iso
from ..db import market_contexts, market_resolution, market_snapshot, record_exit_recommendation


# Slippage-aware take-profit: 0.7 time-decay × 0.9 two-sided slippage.
_ASYMMETRIC_TARGET_DISCOUNT = 0.63


def _proposal_conviction_fields(conn, proposal_id: str) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT conviction_tier, catalyst_clarity, downside_risk,
               asymmetric_target_multiplier, thesis_catalyst_deadline
        FROM proposals WHERE proposal_id = ?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        return {}
    return {
        "conviction_tier": row["conviction_tier"],
        "catalyst_clarity": row["catalyst_clarity"],
        "downside_risk": row["downside_risk"],
        "asymmetric_target_multiplier": row["asymmetric_target_multiplier"],
        "thesis_catalyst_deadline": row["thesis_catalyst_deadline"],
    }


def _take_profit_triggered(entry_price: float | None, last_price: float | None, target_mult: float | None) -> float | None:
    """Return take-profit trigger price if mark >= entry × target × 0.63, else None."""
    if entry_price is None or last_price is None or target_mult is None:
        return None
    try:
        ep = float(entry_price)
        lp = float(last_price)
        tm = float(target_mult)
    except (TypeError, ValueError):
        return None
    if ep <= 0 or tm <= 1.0:
        return None
    trigger = ep * tm * _ASYMMETRIC_TARGET_DISCOUNT
    if lp >= trigger:
        return trigger
    return None


def _catalyst_deadline_passed(deadline: str | None) -> bool:
    if not deadline:
        return False
    try:
        return parse_iso8601(deadline) <= parse_iso8601(utc_now_iso())
    except Exception:
        return False


def evaluate_position(conn, position: Dict) -> Dict:
    """Deterministic exit evaluation — no LLM required."""
    market = market_snapshot(conn, str(position["market_id"])) or {}
    resolution = market_resolution(conn, str(position["market_id"]))
    if resolution is not None:
        return {
            "position_id": position["id"],
            "recommendation": "close",
            "target_reduce_pct": 1.0,
            "reasoning": "Market already resolved; position should be closed and reviewed.",
            "confidence_score": 0.99,
            "payload_json": {"resolution": resolution},
        }

    # Asymmetric take-profit: partial close when mark hits target × 0.63.
    conv = _proposal_conviction_fields(conn, str(position.get("proposal_id", "")))
    trigger = _take_profit_triggered(
        position.get("entry_price"),
        position.get("last_mark_price"),
        conv.get("asymmetric_target_multiplier"),
    )
    if trigger is not None:
        return {
            "position_id": position["id"],
            "recommendation": "reduce",
            "target_reduce_pct": 0.5,
            "reasoning": (
                f"Asymmetric take-profit triggered: mark {position.get('last_mark_price'):.3f} >= "
                f"entry×target×0.63 ({trigger:.3f}); trim 50% and let winner ride."
            ),
            "confidence_score": 0.85,
            "payload_json": {
                "source": "asymmetric_take_profit",
                "trigger_price": trigger,
                "entry_price": position.get("entry_price"),
                "last_mark_price": position.get("last_mark_price"),
                "asymmetric_target_multiplier": conv.get("asymmetric_target_multiplier"),
                "discount_applied": _ASYMMETRIC_TARGET_DISCOUNT,
            },
        }

    # Catalyst-deadline time-stop: after deadline, kick to LLM review via hold
    # with a flag the LLM-backed path will pick up. When LLM is disabled this
    # stays as hold — conservative default.
    if _catalyst_deadline_passed(conv.get("thesis_catalyst_deadline")):
        return {
            "position_id": position["id"],
            "recommendation": "hold",
            "target_reduce_pct": None,
            "reasoning": (
                "Catalyst deadline passed — flagged for LLM reassessment on next exit-agent cycle."
            ),
            "confidence_score": 0.6,
            "payload_json": {
                "source": "catalyst_deadline_passed",
                "thesis_catalyst_deadline": conv.get("thesis_catalyst_deadline"),
            },
        }

    end_date = market.get("end_date")
    if end_date:
        minutes_to_expiry = int((parse_iso8601(end_date) - parse_iso8601(utc_now_iso())).total_seconds() / 60)
        threshold = get_env_int("POLY_EXIT_EXPIRY_MINUTES", 30)
        if minutes_to_expiry <= threshold:
            return {
                "position_id": position["id"],
                "recommendation": "close",
                "target_reduce_pct": 1.0,
                "reasoning": f"Market is within {threshold} minutes of expiry.",
                "confidence_score": 0.8,
                "payload_json": {"minutes_to_expiry": minutes_to_expiry},
            }
    return {
        "position_id": position["id"],
        "recommendation": "hold",
        "target_reduce_pct": None,
        "reasoning": "No deterministic exit trigger fired.",
        "confidence_score": 0.55,
        "payload_json": {},
    }


def evaluate_position_with_llm(conn, position: Dict) -> Dict:
    """Ask PolyExiter (Sonnet 4.6) for an exit decision. Falls back to deterministic."""
    from ..services.openclaw_adapter import is_enabled
    from .poly_exiter import generate_exit_decisions

    if not is_enabled():
        return evaluate_position(conn, position)

    # Deterministic rules always take priority over the LLM.
    deterministic = evaluate_position(conn, position)
    if deterministic["recommendation"] != "hold":
        return deterministic

    market = market_snapshot(conn, str(position["market_id"])) or {}
    contexts = market_contexts(conn, str(position["market_id"]))
    prompt_payload = {
        "position": {
            "id": position["id"],
            "market_id": position["market_id"],
            "outcome": position["outcome"],
            "entry_price": position.get("entry_price"),
            "size_usdc": position.get("size_usdc"),
            "filled_qty": position.get("filled_qty"),
            "unrealized_pnl": position.get("unrealized_pnl"),
            "entry_time": position.get("entry_time"),
        },
        "market": {
            "question": market.get("question"),
            "end_date": market.get("end_date"),
            "liquidity_usdc": market.get("liquidity_usdc"),
            "outcomes": market.get("outcomes_json") or market.get("outcomes", []),
        },
        "contexts": [
            {"title": c.get("title"), "text": (c.get("display_text") or "")[:500]}
            for c in (contexts or [])[:5]
        ],
    }
    try:
        result = generate_exit_decisions(prompt_payload)
    except Exception:
        return deterministic
    if not result:
        return deterministic
    item = next(
        (r for r in result if int(r.get("position_id", -1)) == int(position["id"])),
        result[0],
    )
    recommendation = item.get("recommendation", "hold")
    if recommendation not in ("hold", "reduce", "close", "cancel"):
        recommendation = "hold"
    return {
        "position_id": position["id"],
        "recommendation": recommendation,
        "target_reduce_pct": item.get("target_reduce_pct"),
        "reasoning": item.get("reasoning", "PolyExiter decision"),
        "confidence_score": float(item.get("confidence_score", 0.6)),
        "payload_json": {"source": "poly_exiter", "raw": item},
    }


def run_exit_agent(conn, position: Dict, *, use_llm: bool = False) -> Dict:
    if use_llm:
        recommendation = evaluate_position_with_llm(conn, position)
    else:
        recommendation = evaluate_position(conn, position)
    return record_exit_recommendation(conn, recommendation)
