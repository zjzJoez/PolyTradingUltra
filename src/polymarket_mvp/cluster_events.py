from __future__ import annotations

import argparse

from .common import dump_json, load_json, utc_now_iso
from .db import connect_db, init_db, upsert_market_snapshot
from .services.event_cluster_service import cluster_markets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministically cluster scanned markets into event clusters.")
    parser.add_argument("--market-file", required=True, help="Scanner JSON file.")
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    payload = load_json(args.market_file)
    markets = payload.get("markets", [])
    with connect_db() as conn:
        for market in markets:
            upsert_market_snapshot(conn, market)
        results = cluster_markets(conn, markets)
        conn.commit()
    print(dump_json({"generated_at": utc_now_iso(), "clusters": results}, path=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
