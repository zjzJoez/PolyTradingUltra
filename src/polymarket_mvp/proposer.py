from __future__ import annotations

import argparse
from typing import Any, Dict, List, Mapping

from .common import (
    dump_json,
    load_json,
    market_reference_price,
    normalize_proposal,
    proposal_id_for,
    read_proposals,
    utc_now_iso,
)
from .db import (
    connect_db,
    init_db,
    latest_research_memo,
    market_contexts,
    market_snapshot,
    replace_proposal_contexts,
    update_proposal_workflow_fields,
    upsert_market_snapshot,
    upsert_proposal,
    proposal_record,
)
from .agents.supervisor_agent import supervise_record
from .services.event_cluster_service import cluster_market
from .services.memo_service import build_and_store_memo


def _extract_yes_no_prices(market: Mapping[str, Any]) -> Dict[str, float]:
    prices = {}
    for outcome in market.get("outcomes", []):
        name = outcome.get("name")
        price = outcome.get("price")
        if name in {"Yes", "No"} and isinstance(price, (int, float)):
            prices[name] = float(price)
    return prices


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
        prices = _extract_yes_no_prices(market)
        if set(prices.keys()) != {"Yes", "No"}:
            continue
        favored = "Yes" if prices["Yes"] >= prices["No"] else "No"
        confidence = round(max(prices["Yes"], prices["No"]), 4)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate normalized proposal records and persist them to SQLite.")
    parser.add_argument("--market-file", help="Scanner JSON file.")
    parser.add_argument("--input", dest="legacy_market_file", help="Legacy alias for --market-file.")
    parser.add_argument("--context-file", help="event_fetcher output JSON file.")
    parser.add_argument("--engine", choices=["heuristic", "openclaw_llm"], default="heuristic")
    parser.add_argument("--proposal-file", help="External OpenClaw/LLM proposal JSON file when engine=openclaw_llm.")
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
    markets = list(markets_payload.get("markets", []))
    context_payload = load_json(args.context_file) if args.context_file else None
    if args.engine == "openclaw_llm":
        if not args.proposal_file:
            raise RuntimeError("--proposal-file is required when --engine=openclaw_llm")
        proposals = read_proposals(args.proposal_file)
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
