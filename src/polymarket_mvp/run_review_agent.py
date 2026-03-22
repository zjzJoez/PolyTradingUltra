from __future__ import annotations

import argparse

from .agents.review_agent import run_review_agent
from .common import dump_json, utc_now_iso
from .db import connect_db, init_db, list_positions, list_reviews, market_resolution


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate deterministic post-resolution reviews.")
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    created = []
    with connect_db() as conn:
        reviewed_position_ids = {item.get("position_id") for item in list_reviews(conn) if item.get("position_id") is not None}
        for position in list_positions(conn):
            if position["id"] in reviewed_position_ids:
                continue
            if market_resolution(conn, str(position["market_id"])) is None:
                continue
            created.append(run_review_agent(conn, position))
        conn.commit()
    print(dump_json({"generated_at": utc_now_iso(), "reviews": created}, path=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
