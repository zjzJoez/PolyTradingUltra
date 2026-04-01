from __future__ import annotations

import argparse

from .agents.exit_agent import run_exit_agent
from .common import dump_json, get_env_int, normalize_proposal, utc_now_iso
from .db import connect_db, init_db, list_positions, upsert_proposal


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate exit recommendations for open positions.")
    parser.add_argument("--output", help="Optional file path for JSON output.")
    parser.add_argument("--use-llm", action="store_true", help="Use OpenClaw LLM for exit decisions.")
    parser.add_argument("--create-proposals", action="store_true", help="Create exit proposals for close recommendations.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    exit_proposals_created = 0
    with connect_db() as conn:
        positions = list_positions(conn, statuses=["open_requested", "open", "partially_filled"])
        recommendations = [run_exit_agent(conn, position, use_llm=args.use_llm) for position in positions]
        if args.create_proposals:
            positions_by_id = {p["id"]: p for p in positions}
            for rec in recommendations:
                if rec.get("recommendation") == "close" and float(rec.get("confidence_score", 0)) >= 0.7:
                    position = positions_by_id.get(rec["position_id"])
                    if position is None:
                        continue
                    exit_proposal = normalize_proposal({
                        "market_id": position["market_id"],
                        "outcome": position["outcome"],
                        "confidence_score": rec["confidence_score"],
                        "recommended_size_usdc": position["size_usdc"],
                        "reasoning": rec.get("reasoning", "exit recommendation"),
                        "max_slippage_bps": get_env_int("POLY_RISK_MAX_SLIPPAGE_BPS", 500),
                    })
                    upsert_proposal(
                        conn,
                        exit_proposal,
                        decision_engine="openclaw_llm" if args.use_llm else "heuristic",
                        status="proposed",
                        context_payload={},
                        proposal_kind="exit",
                        target_position_id=position["id"],
                    )
                    exit_proposals_created += 1
        conn.commit()
    print(dump_json({
        "generated_at": utc_now_iso(),
        "recommendations": recommendations,
        "exit_proposals_created": exit_proposals_created,
    }, path=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
