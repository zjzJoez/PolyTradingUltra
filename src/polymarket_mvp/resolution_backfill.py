from __future__ import annotations

import argparse

from .common import dump_json, load_json, utc_now_iso
from .db import connect_db, init_db, upsert_market_resolution


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill resolved market outcomes into SQLite.")
    parser.add_argument("--input", required=True, help="JSON file containing a list of {market_id, resolved_outcome}.")
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    payload = load_json(args.input)
    items = payload if isinstance(payload, list) else [payload]
    with connect_db() as conn:
        for item in items:
            upsert_market_resolution(conn, str(item["market_id"]), str(item["resolved_outcome"]), item)
        conn.commit()
    print(dump_json({"generated_at": utc_now_iso(), "count": len(items)}, path=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
