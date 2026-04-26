from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Mapping

from .common import (
    blocked_market_reason,
    dump_json,
    get_env_float,
    get_env_int,
    load_json,
    market_reference_price,
    normalize_proposal,
    price_is_tradable,
    proposal_id_for,
    read_proposals,
    tradable_price_bounds,
    utc_now_iso,
)
from .db import (
    connect_db,
    init_db,
    latest_research_memo,
    market_contexts,
    market_snapshot,
    recent_proposals_for_market,
    replace_proposal_contexts,
    update_proposal_workflow_fields,
    upsert_market_snapshot,
    upsert_proposal,
    proposal_record,
)
from .agents.supervisor_agent import supervise_record
from .services.event_cluster_service import (
    MARKET_CLASS_CONFIG,
    classify_market_class,
    cluster_market,
)
from .agents.poly_proposer import generate_trade_proposals as poly_proposer_generate
from .services.openclaw_adapter import get_last_meta
from .services.memo_service import build_and_store_memo
from .strategy.conviction import (
    BASE_TIER_SIZES,
    compute_tier,
    compute_tier_size,
    downgrade_tier,
    tier_rank,
)


def _extract_yes_no_prices(market: Mapping[str, Any]) -> Dict[str, float]:
    prices = {}
    for outcome in market.get("outcomes", []):
        name = outcome.get("name")
        price = outcome.get("price")
        if name in {"Yes", "No"} and isinstance(price, (int, float)):
            prices[name] = float(price)
    return prices


def _market_outcome_names(market: Mapping[str, Any]) -> List[str]:
    names: List[str] = []
    for outcome in market.get("outcomes", []):
        name = outcome.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def build_heuristic_proposals(
    markets: List[Mapping[str, Any]],
    *,
    min_confidence: float,
    size_usdc: float,
    top: int,
    max_slippage_bps: int,
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for market in markets:
        if blocked_market_reason(market):
            continue
        # Skip markets whose class is disabled in risk config, otherwise the
        # top-N slots get consumed by candidates that will be blocked downstream.
        market_class = classify_market_class(market)
        class_config = MARKET_CLASS_CONFIG.get(market_class) or MARKET_CLASS_CONFIG.get("other", {})
        if not class_config.get("live_enabled", False):
            continue
        prices = _extract_yes_no_prices(market)
        if set(prices.keys()) != {"Yes", "No"}:
            continue
        favored = "Yes" if prices["Yes"] >= prices["No"] else "No"
        confidence = round(max(prices["Yes"], prices["No"]), 4)
        if not price_is_tradable(confidence):
            continue
        if confidence < min_confidence:
            continue
        reason = (
            f"Event-driven heuristic candidate. Liquidity ${float(market.get('liquidity_usdc') or 0):.0f}, "
            f"expiry {float(market.get('days_to_expiry') or 0):.2f}d, "
            f"{favored} reference price {confidence:.2f}."
        )
        candidates.append(
            normalize_proposal(
                {
                    "market_id": market["market_id"],
                    "outcome": favored,
                    "confidence_score": confidence,
                    "recommended_size_usdc": size_usdc,
                    "reasoning": reason,
                    "max_slippage_bps": max_slippage_bps,
                }
            )
        )
    candidates.sort(key=lambda item: (-item["confidence_score"], item["market_id"]))
    deduped: List[Dict[str, Any]] = []
    seen_ids = set()
    for item in candidates:
        proposal_id = proposal_id_for(item)
        if proposal_id in seen_ids:
            continue
        seen_ids.add(proposal_id)
        deduped.append(item)
        if len(deduped) >= top:
            break
    return deduped


def resolve_context_payload(context_file: Dict[str, Any] | None, market_id: str) -> Dict[str, Any]:
    if context_file:
        for item in context_file.get("markets", []):
            if str(item.get("market_id")) == market_id:
                return dict(item.get("context_payload") or {})
    return {"market_id": market_id, "context_budget_chars": 0, "assembled_text": "", "sources": []}


def _market_llm_score(market: Mapping[str, Any]) -> float:
    """Cheap score for ranking markets before sending to the LLM.

    Long-tail bias: favor markets with at least one outcome in the lottery
    zones (0.08-0.30 for YES tails, 0.70-0.92 for NO tails). Near-50/50 and
    extreme-price markets score lower.
    """
    min_tradable, max_tradable = tradable_price_bounds()
    prices: List[float] = []
    for outcome in market.get("outcomes", []) or []:
        price = outcome.get("price")
        if isinstance(price, (int, float)):
            prices.append(float(price))
    if not prices:
        return 0.0

    has_yes_tail = any(0.08 <= p <= 0.30 for p in prices)
    has_no_tail = any(0.70 <= p <= 0.92 for p in prices)
    all_extreme = all(p < min_tradable or p > max_tradable for p in prices)
    near_50_50 = all(0.45 <= p <= 0.55 for p in prices) and len(prices) <= 2

    liquidity = float(market.get("liquidity_usdc") or 0.0)
    volume = float(market.get("volume_24h_usdc") or 0.0)
    base = (liquidity + 0.5 * volume) ** 0.5

    if all_extreme:
        return 0.0
    multiplier = 1.0
    if has_yes_tail or has_no_tail:
        multiplier *= 1.6
    if near_50_50:
        multiplier *= 0.5
    days = market.get("days_to_expiry")
    if isinstance(days, (int, float)) and float(days) < 0.5:
        multiplier *= 0.5
    return base * multiplier


def select_llm_candidates(
    markets: List[Mapping[str, Any]],
    *,
    limit: int,
) -> List[Mapping[str, Any]]:
    """Heuristic pre-filter: pick the top-N markets worth sending to the LLM."""
    if limit <= 0 or not markets:
        return []
    scored = [(_market_llm_score(m), idx, m) for idx, m in enumerate(markets)]
    scored = [item for item in scored if item[0] > 0]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [market for _score, _idx, market in scored[:limit]]


def _legacy_kelly_size(
    *,
    base_size_usdc: float,
    confidence: float,
    market_price: float | None,
    min_floor_usdc: float,
) -> float:
    """Legacy linear-Kelly sizer. Retained for POLY_SIZING_MODE=kelly rollback.

    Do not use as the primary sizer on small accounts — the 5-share-per-order
    floor forces this to zero out almost every proposal when base_size_usdc
    is under ~$10. The conviction-tier sizer in strategy/conviction.py is the
    current default.
    """
    if market_price is None:
        return base_size_usdc
    try:
        p = float(market_price)
        c = float(confidence)
    except (TypeError, ValueError):
        return base_size_usdc
    if p <= 0 or p >= 1 or c <= p:
        return 0.0
    edge = (c - p) / (1.0 - p)
    scaled = base_size_usdc * max(0.0, min(1.0, edge))
    min_viable = max(min_floor_usdc, 5.0 * p * 1.02)
    if scaled < min_viable:
        return 0.0
    return min(base_size_usdc, scaled)


def _five_share_floor(market_price: float) -> float:
    """Polymarket exchange minimum: 5 shares × price × 2% safety buffer."""
    return 5.0 * float(market_price) * 1.02


def _size_from_conviction(
    *,
    llm_item: Mapping[str, Any],
    market_price: float,
    balance_usdc: float | None = None,
) -> tuple[float, str | None]:
    """Compute (size_usdc, tier) from the LLM's objective conviction fields.

    Returns (0.0, None) when:
    - LLM did not provide the required fields
    - edge is below skip threshold
    - even the lowest tier ($2 × scale) falls below the 5-share floor at
      market_price (order would be rejected by the exchange)
    """
    confidence = llm_item.get("confidence_score")
    clarity = llm_item.get("catalyst_clarity") or ""
    downside = llm_item.get("downside_risk") or ""
    if confidence is None:
        return 0.0, None
    tier = compute_tier(
        confidence=float(confidence),
        market_price=market_price,
        catalyst_clarity=str(clarity),
        downside_risk=str(downside),
    )
    if tier is None:
        return 0.0, None
    # Auto-downgrade until size clears the 5-share floor.
    floor = _five_share_floor(market_price)
    while tier is not None:
        size = compute_tier_size(tier, balance_usdc)
        if size >= floor:
            return size, tier
        tier = downgrade_tier(tier)
    return 0.0, None


def _clob_top5_liquidity_usdc(token_id: str) -> float | None:
    """Read top-5 ask-side depth for the target YES/NO token.

    Returns None on any failure; callers should treat None as "unknown, don't
    block". This is a best-effort pre-trade check, not a hard dependency.
    """
    if not token_id:
        return None
    try:
        import requests as _rq
        resp = _rq.get(
            "https://clob.polymarket.com/book",
            params={"token_id": str(token_id)},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        book = resp.json() or {}
    except Exception:
        return None
    asks = book.get("asks") or []
    if not isinstance(asks, list):
        return None
    total = 0.0
    taken = 0
    # CLOB returns asks sorted worst-to-best; we want the 5 best (highest bid
    # side) — but for asks the "best" is the lowest price. The book API
    # returns a standard L2 book; take the first 5 levels from the side.
    for level in asks[:5]:
        try:
            price = float(level.get("price"))
            size = float(level.get("size"))
        except (TypeError, ValueError):
            continue
        total += price * size
        taken += 1
    if taken == 0:
        return None
    return total


def _enforce_clob_slippage_cap(
    *,
    proposed_size_usdc: float,
    tier: str,
    token_id: str,
    market_price: float,
    balance_usdc: float | None,
) -> tuple[float, str | None]:
    """Downgrade tier until proposed size stays ≤25% of top-5 CLOB depth.

    Returns (size, tier) or (0.0, None) if even the smallest tier can't fit
    (or is below the 5-share floor).
    """
    top5 = _clob_top5_liquidity_usdc(token_id)
    if top5 is None or top5 <= 0:
        return proposed_size_usdc, tier
    cap = 0.25 * top5
    floor = _five_share_floor(market_price)
    current_tier: str | None = tier
    current_size = proposed_size_usdc
    while current_tier is not None and current_size > cap:
        current_tier = downgrade_tier(current_tier)
        if current_tier is None:
            return 0.0, None
        current_size = compute_tier_size(current_tier, balance_usdc)
        if current_size < floor:
            return 0.0, None
    return current_size, current_tier


def _prior_proposals_snippet(conn, market_id: str, limit: int) -> List[Dict[str, Any]]:
    if conn is None or limit <= 0:
        return []
    try:
        records = recent_proposals_for_market(conn, market_id, limit=limit)
    except Exception:
        return []
    snippet: List[Dict[str, Any]] = []
    for rec in records:
        fill_price = rec.get("fill_price")
        snippet.append({
            "created_at": rec.get("created_at"),
            "outcome": rec.get("outcome"),
            "confidence_score": rec.get("confidence_score"),
            "status": rec.get("status"),
            "decision_engine": rec.get("decision_engine"),
            "fill_price": float(fill_price) if isinstance(fill_price, (int, float)) else None,
            "fill_size_usdc": rec.get("fill_size_usdc"),
            "execution_status": rec.get("execution_status"),
        })
    return snippet


def build_openclaw_proposals(
    markets: List[Mapping[str, Any]],
    *,
    context_file: Dict[str, Any] | None,
    size_usdc: float,
    top: int,
    max_slippage_bps: int,
    conn=None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any] | None, Dict[str, Dict[str, Any]]]:
    eligible_markets = [market for market in markets if not blocked_market_reason(market)]
    if not eligible_markets:
        return [], None, {}
    llm_candidate_limit = max(1, get_env_int("POLY_PROPOSER_LLM_CANDIDATES", 8))
    candidate_markets = select_llm_candidates(eligible_markets, limit=llm_candidate_limit)
    if not candidate_markets:
        return [], None, {}
    prior_proposal_depth = max(0, get_env_int("POLY_PROPOSER_PRIOR_DEPTH", 3))
    market_outcomes = {
        str(market["market_id"]): _market_outcome_names(market) for market in candidate_markets
    }
    min_tradable_price, max_tradable_price = tradable_price_bounds()
    prompt_payload = {
        "constraints": {
            "top": top,
            "default_recommended_size_usdc": size_usdc,
            "max_slippage_bps": max_slippage_bps,
            "min_tradable_price": min_tradable_price,
            "max_tradable_price": max_tradable_price,
            "outcome_rule": "Each proposal outcome must exactly match one of the provided outcomes for that market.",
            "duplicate_rule": "Do not return duplicate proposals or multiple outcomes for the same market unless explicitly requested.",
            "always_propose_rule": (
                f"Always try to return at least {min(top, 3)} proposals. "
                "When external context is absent, use pricing patterns, market structure, liquidity, and statistical reasoning "
                "(e.g. spread markets near 0.5 can still offer value when one side is mispriced by even 2-3%). "
                "Only return fewer proposals if the market list itself has fewer than that many tradable opportunities."
            ),
            "tradeability_rule": (
                "Only propose entries with meaningful upside and realistic fill potential. "
                f"Reject outcomes priced below {min_tradable_price:.2f} or above {max_tradable_price:.2f}. "
                "Do not select near-certain, low-upside markets even if confidence is high."
            ),
        },
        "output_contract": {
            "json_only": True,
            "preferred_top_level_shape": {"proposals": []},
            "top_level_array_also_accepted": True,
            "proposal_required_keys": [
                "market_id",
                "outcome",
                "confidence_score",
                "catalyst_clarity",
                "downside_risk",
                "asymmetric_target_multiplier",
                "thesis_catalyst_deadline",
                "recommended_size_usdc",
                "reasoning",
                "max_slippage_bps",
            ],
            "proposal_field_rules": {
                "market_id": "must exactly match a provided market_id",
                "outcome": "must exactly match one provided allowed_outcomes entry for that market",
                "confidence_score": "number in [0, 1] — your independent probability estimate, NOT the current market price. Only propose when your estimate meaningfully diverges from market price.",
                "catalyst_clarity": 'one of "none" | "weak" | "moderate" | "strong"',
                "downside_risk": 'one of "limited" | "moderate" | "substantial"',
                "asymmetric_target_multiplier": "positive number — realistic payoff ratio (e.g. 2.5, 5, 10). null allowed only if truly unestimable.",
                "thesis_catalyst_deadline": 'ISO date string like "2026-05-10" or null',
                "recommended_size_usdc": "positive number (hint only; system resizes by conviction tier)",
                "reasoning": "concise factual plain text grounded only in the provided payload",
                "max_slippage_bps": f"positive integer <= {max_slippage_bps}",
            },
            "drop_invalid_instead_of_guessing": True,
            "extra_fields_forbidden": True,
        },
        "markets": [],
    }
    for market in candidate_markets:
        market_id_str = str(market["market_id"])
        context_blob = resolve_context_payload(context_file, market_id_str)
        market_block: Dict[str, Any] = {
            "market_id": market["market_id"],
            "question": market.get("question"),
            "liquidity_usdc": market.get("liquidity_usdc"),
            "volume_24h_usdc": market.get("volume_24h_usdc"),
            "days_to_expiry": market.get("days_to_expiry"),
            "end_date": market.get("end_date"),
            "allowed_outcomes": market_outcomes[market_id_str],
            "outcomes": [
                {
                    "name": item.get("name"),
                    "price": item.get("price"),
                    "token_id": item.get("token_id"),
                }
                for item in market.get("outcomes", [])
            ],
            "context": {
                "topic": context_blob.get("topic"),
                "assembled_text": context_blob.get("assembled_text"),
                "sources": context_blob.get("sources", [])[:5],
            },
        }
        prior = _prior_proposals_snippet(conn, market_id_str, prior_proposal_depth)
        if prior:
            market_block["prior_proposals"] = prior
        prompt_payload["markets"].append(market_block)
    generated = poly_proposer_generate(prompt_payload)
    llm_meta = get_last_meta()
    if not generated:
        import sys as _sys
        print(
            "[proposer] PolyProposer returned no proposals this cycle (LLM may have found no edge). "
            "Will retry next cycle.",
            file=_sys.stderr,
        )
        return [], llm_meta, {}
    proposals: List[Dict[str, Any]] = []
    raw_items_by_id: Dict[str, Mapping[str, Any]] = {}
    invalid_items: List[str] = []
    seen_ids = set()
    for item in generated:
        market_id = str(item["market_id"])
        allowed_outcomes = market_outcomes.get(market_id, [])
        outcome = str(item["outcome"]).strip()
        if outcome not in allowed_outcomes:
            invalid_items.append(
                f"market_id={market_id} outcome={outcome!r} allowed={allowed_outcomes}"
            )
            continue
        normalized = normalize_proposal(
            {
                "market_id": market_id,
                "outcome": outcome,
                "confidence_score": item["confidence_score"],
                "recommended_size_usdc": item.get("recommended_size_usdc", size_usdc),
                "reasoning": item["reasoning"],
                "max_slippage_bps": item.get("max_slippage_bps", max_slippage_bps),
            }
        )
        proposal_id = proposal_id_for(normalized)
        if proposal_id in seen_ids:
            continue
        seen_ids.add(proposal_id)
        proposals.append(normalized)
        raw_items_by_id[proposal_id] = item
        if len(proposals) >= top:
            break
    if not proposals and invalid_items:
        raise RuntimeError(
            "OpenClaw proposal generation returned only invalid outcomes: "
            + "; ".join(invalid_items[:5])
        )
    sizing_mode = (os.getenv("POLY_SIZING_MODE") or "conviction").strip().lower()
    min_order_floor = get_env_float("POLY_ORDER_MIN_USDC", 1.0)
    try:
        balance_override: float | None = float(os.getenv("POLY_ACCOUNT_BALANCE_USDC") or "") if os.getenv("POLY_ACCOUNT_BALANCE_USDC") else None
    except ValueError:
        balance_override = None
    tradable: List[Dict[str, Any]] = []
    conviction_by_id: Dict[str, Dict[str, Any]] = {}
    for proposal in proposals:
        pid = proposal_id_for(proposal)
        market = next((item for item in markets if str(item["market_id"]) == str(proposal["market_id"])), None)
        reference_price = market_reference_price(market or {}, proposal["outcome"]) if market is not None else None
        if not price_is_tradable(reference_price):
            continue
        # Resolve token_id for CLOB slippage check.
        token_id = ""
        if market is not None:
            for out in market.get("outcomes", []) or []:
                if str(out.get("name") or "").strip() == proposal["outcome"]:
                    token_id = str(out.get("token_id") or "")
                    break

        raw = raw_items_by_id.get(pid, {})
        conviction_payload = {
            "catalyst_clarity": raw.get("catalyst_clarity"),
            "downside_risk": raw.get("downside_risk"),
            "asymmetric_target_multiplier": raw.get("asymmetric_target_multiplier"),
            "thesis_catalyst_deadline": raw.get("thesis_catalyst_deadline"),
            "conviction_tier": None,
        }

        if sizing_mode in {"off", "flat"}:
            tradable.append(proposal)
            conviction_by_id[pid] = conviction_payload
            continue

        if sizing_mode == "kelly":
            sized = _legacy_kelly_size(
                base_size_usdc=float(proposal["recommended_size_usdc"]),
                confidence=float(proposal["confidence_score"]),
                market_price=reference_price,
                min_floor_usdc=min_order_floor,
            )
            if sized <= 0:
                continue
            proposal = dict(proposal)
            proposal["recommended_size_usdc"] = round(sized, 4)
            # proposal_id is derived from the *final* proposal contents
            # (including the post-resize recommended_size_usdc), so
            # conviction_by_id MUST be keyed off the recomputed pid.
            tradable.append(proposal)
            conviction_by_id[proposal_id_for(proposal)] = conviction_payload
            continue

        # Default path: conviction-tier sizing.
        size_usdc_new, tier = _size_from_conviction(
            llm_item=proposal | conviction_payload | {"confidence_score": proposal["confidence_score"]},
            market_price=reference_price,
            balance_usdc=balance_override,
        )
        if size_usdc_new <= 0 or tier is None:
            continue
        # CLOB liquidity downgrade (best-effort; returns input unchanged if API unreachable).
        size_usdc_new, tier = _enforce_clob_slippage_cap(
            proposed_size_usdc=size_usdc_new,
            tier=tier,
            token_id=token_id,
            market_price=reference_price,
            balance_usdc=balance_override,
        )
        if size_usdc_new <= 0 or tier is None:
            continue
        proposal = dict(proposal)
        proposal["recommended_size_usdc"] = round(size_usdc_new, 4)
        conviction_payload["conviction_tier"] = tier
        tradable.append(proposal)
        # Recompute pid after the size mutation — the original `pid`
        # was hashed against the pre-resize proposal and would no
        # longer match the persisted record's proposal_id, causing
        # conviction fields to drop to NULL on upsert lookup.
        conviction_by_id[proposal_id_for(proposal)] = conviction_payload
    return tradable, llm_meta, conviction_by_id


def run_proposal_pipeline(
    conn,
    markets: List[Dict[str, Any]],
    *,
    engine: str = "heuristic",
    context_payload: Dict[str, Any] | None = None,
    size_usdc: float = 10.0,
    top: int = 3,
    max_slippage_bps: int = 500,
    min_confidence: float = 0.6,
) -> List[Dict[str, Any]]:
    """Generate proposals, enrich with memos/supervision, and persist. Returns enriched records."""
    markets = [dict(item) for item in markets if not blocked_market_reason(item)]
    if not markets:
        return []
    llm_meta: Dict[str, Any] | None = None
    conviction_by_id: Dict[str, Dict[str, Any]] = {}
    if engine == "openclaw_llm":
        proposals, llm_meta, conviction_by_id = build_openclaw_proposals(
            markets,
            context_file=context_payload,
            size_usdc=size_usdc,
            top=top,
            max_slippage_bps=max_slippage_bps,
            conn=conn,
        )
    else:
        proposals = build_heuristic_proposals(
            markets,
            min_confidence=min_confidence,
            size_usdc=size_usdc,
            top=top,
            max_slippage_bps=max_slippage_bps,
        )
    market_map = {str(item["market_id"]): dict(item) for item in markets}
    cluster_map: Dict[str, Dict[str, Any]] = {}
    for m in market_map.values():
        upsert_market_snapshot(conn, m)
        cluster_map[str(m["market_id"])] = cluster_market(conn, m)
    conn.commit()
    persisted = []
    for proposal in proposals:
        market_id = str(proposal["market_id"])
        market = market_map.get(market_id) or market_snapshot(conn, market_id)
        if market is None:
            continue
        cluster_result = cluster_map.get(market_id)
        if cluster_result is None:
            cluster_result = cluster_market(conn, market)
            conn.commit()
        memo = latest_research_memo(conn, market_id)
        contexts = market_contexts(conn, market_id)
        if memo is None and contexts:
            conn.commit()
            memo = build_and_store_memo(conn, market_id, cluster=cluster_result["cluster"])
            conn.commit()
        context_blob = resolve_context_payload(context_payload, market_id)
        default_topic = (
            (memo or {}).get("topic")
            or cluster_result["cluster"].get("topic")
            or context_blob.get("topic")
            or market_id
        )
        strategy_name = "near_expiry_conviction"
        proposal_id_guess = proposal_id_for(proposal)
        conv = conviction_by_id.get(proposal_id_guess, {})
        target_mult = conv.get("asymmetric_target_multiplier")
        try:
            target_mult_float = float(target_mult) if target_mult is not None else None
        except (TypeError, ValueError):
            target_mult_float = None
        record = upsert_proposal(
            conn,
            proposal,
            decision_engine=engine,
            status="proposed",
            context_payload=context_blob,
            strategy_name=strategy_name,
            topic=default_topic,
            event_cluster_id=cluster_result["cluster"]["id"],
            source_memo_id=(memo or {}).get("id"),
            llm_meta=llm_meta if engine == "openclaw_llm" else None,
            conviction_tier=conv.get("conviction_tier"),
            catalyst_clarity=conv.get("catalyst_clarity"),
            downside_risk=conv.get("downside_risk"),
            asymmetric_target_multiplier=target_mult_float,
            thesis_catalyst_deadline=conv.get("thesis_catalyst_deadline"),
        )
        replace_proposal_contexts(conn, record["proposal_id"], contexts)
        conn.commit()
        enriched = proposal_record(conn, record["proposal_id"])
        if enriched is None:
            continue
        supervisor = supervise_record(enriched)
        update_proposal_workflow_fields(
            conn,
            record["proposal_id"],
            strategy_name=supervisor.get("strategy_name") or strategy_name,
            topic=supervisor.get("topic") or default_topic,
            event_cluster_id=cluster_result["cluster"]["id"],
            source_memo_id=(memo or {}).get("id"),
            supervisor_decision=supervisor.get("decision"),
            priority_score=supervisor.get("priority_score"),
        )
        conn.commit()
        persisted.append({
            "proposal_id": record["proposal_id"],
            "proposal": normalize_proposal(proposal),
            "market_reference_price": market_reference_price(market, proposal["outcome"]),
            "decision_engine": engine,
            "status": "proposed",
            "topic": supervisor.get("topic") or default_topic,
            "strategy_name": supervisor.get("strategy_name") or strategy_name,
        })
    return persisted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate normalized proposal records and persist them to SQLite.")
    parser.add_argument("--market-file", help="Scanner JSON file.")
    parser.add_argument("--input", dest="legacy_market_file", help="Legacy alias for --market-file.")
    parser.add_argument("--context-file", help="event_fetcher output JSON file.")
    parser.add_argument("--engine", choices=["heuristic", "openclaw_llm"], default="heuristic")
    parser.add_argument("--proposal-file", help="Optional external OpenClaw/LLM proposal JSON file when engine=openclaw_llm.")
    parser.add_argument("--min-confidence", type=float, default=0.6)
    parser.add_argument("--size-usdc", type=float, default=10.0)
    parser.add_argument("--size-u", dest="legacy_size_usdc", type=float, help="Legacy alias for --size-usdc.")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--max-slippage-bps", type=int, default=500)
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    market_file = args.market_file or args.legacy_market_file
    if not market_file:
        raise RuntimeError("--market-file is required")
    size_usdc = args.legacy_size_usdc if args.legacy_size_usdc is not None else args.size_usdc
    markets_payload = load_json(market_file)
    markets = [dict(item) for item in markets_payload.get("markets", []) if not blocked_market_reason(item)]
    context_payload = load_json(args.context_file) if args.context_file else None
    cli_llm_meta: Dict[str, Any] | None = None
    cli_conv: Dict[str, Any] = {}
    if args.engine == "openclaw_llm":
        if args.proposal_file:
            proposals = read_proposals(args.proposal_file)
        else:
            proposals, cli_llm_meta, cli_conv = build_openclaw_proposals(
                markets,
                context_file=context_payload,
                size_usdc=size_usdc,
                top=args.top,
                max_slippage_bps=args.max_slippage_bps,
            )
    else:
        proposals = build_heuristic_proposals(
            markets,
            min_confidence=args.min_confidence,
            size_usdc=size_usdc,
            top=args.top,
            max_slippage_bps=args.max_slippage_bps,
        )
    persisted = []
    with connect_db() as conn:
        market_map = {str(item["market_id"]): dict(item) for item in markets}
        for market in market_map.values():
            upsert_market_snapshot(conn, market)
            cluster_market(conn, market)
        for proposal in proposals:
            market_id = str(proposal["market_id"])
            market = market_map.get(market_id) or market_snapshot(conn, market_id)
            if market is None:
                raise RuntimeError(f"Unknown market for proposal {market_id}")
            cluster_result = cluster_market(conn, market)
            memo = latest_research_memo(conn, market_id)
            if memo is None and market_contexts(conn, market_id):
                memo = build_and_store_memo(conn, market_id, cluster=cluster_result["cluster"])
            context_blob = resolve_context_payload(context_payload, market_id)
            default_topic = (
                (memo or {}).get("topic")
                or cluster_result["cluster"].get("topic")
                or context_blob.get("topic")
                or market_id
            )
            strategy_name = "near_expiry_conviction"
            conv_data = cli_conv.get(proposal_id_for(normalize_proposal(proposal)), {})
            record = upsert_proposal(
                conn,
                proposal,
                decision_engine=args.engine,
                status="proposed",
                context_payload=context_blob,
                strategy_name=strategy_name,
                topic=default_topic,
                event_cluster_id=cluster_result["cluster"]["id"],
                source_memo_id=(memo or {}).get("id"),
                llm_meta=cli_llm_meta if args.engine == "openclaw_llm" else None,
                conviction_tier=conv_data.get("conviction_tier"),
                catalyst_clarity=conv_data.get("catalyst_clarity"),
                downside_risk=conv_data.get("downside_risk"),
                asymmetric_target_multiplier=conv_data.get("asymmetric_target_multiplier"),
                thesis_catalyst_deadline=conv_data.get("thesis_catalyst_deadline"),
            )
            replace_proposal_contexts(conn, record["proposal_id"], market_contexts(conn, market_id))
            enriched = proposal_record(conn, record["proposal_id"])
            if enriched is None:
                raise RuntimeError(f"Unable to reload proposal record: {record['proposal_id']}")
            supervisor = supervise_record(enriched)
            update_proposal_workflow_fields(
                conn,
                record["proposal_id"],
                strategy_name=supervisor.get("strategy_name") or strategy_name,
                topic=supervisor.get("topic") or default_topic,
                event_cluster_id=cluster_result["cluster"]["id"],
                source_memo_id=(memo or {}).get("id"),
                supervisor_decision=supervisor.get("decision"),
                priority_score=supervisor.get("priority_score"),
            )
            persisted.append(
                {
                    "proposal_id": record["proposal_id"],
                    "proposal": normalize_proposal(proposal),
                    "market_reference_price": market_reference_price(market, proposal["outcome"]),
                    "decision_engine": args.engine,
                    "status": "proposed",
                    "context_payload": context_blob,
                    "topic": supervisor.get("topic") or default_topic,
                    "strategy_name": supervisor.get("strategy_name") or strategy_name,
                    "event_cluster_id": cluster_result["cluster"]["id"],
                    "source_memo_id": (memo or {}).get("id"),
                    "supervisor": supervisor,
                }
            )
        conn.commit()
    print(dump_json({"generated_at": utc_now_iso(), "proposals": persisted}, path=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
