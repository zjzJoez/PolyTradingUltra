from __future__ import annotations

import argparse

from .agents.research_agent import run_research_agent
from .common import dump_json, load_json, utc_now_iso
from .db import connect_db, init_db, upsert_market_snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and persist research memos for markets.")
    parser.add_argument("--market-file", required=True, help="Scanner JSON file.")
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    payload = load_json(args.market_file)
    markets = payload.get("markets", [])
    results = []
    with connect_db() as conn:
        for market in markets:
            upsert_market_snapshot(conn, market)
            results.append(run_research_agent(conn, str(market["market_id"])))
        conn.commit()
    print(dump_json({"generated_at": utc_now_iso(), "memos": results}, path=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
