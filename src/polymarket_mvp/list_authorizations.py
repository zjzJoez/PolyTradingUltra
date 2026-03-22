from __future__ import annotations

import argparse

from .common import dump_json, utc_now_iso
from .db import connect_db, init_db, list_strategy_authorizations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List strategy authorizations.")
    parser.add_argument("--status", help="Optional status filter.")
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    with connect_db() as conn:
        items = list_strategy_authorizations(conn, status=args.status)
    print(dump_json({"generated_at": utc_now_iso(), "authorizations": items}, path=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
