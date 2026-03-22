from __future__ import annotations

import argparse

from .common import dump_json, utc_now_iso
from .db import connect_db, init_db
from .services.position_manager import sync_all_positions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill or refresh positions from executions.")
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    with connect_db() as conn:
        positions = sync_all_positions(conn)
        conn.commit()
    print(dump_json({"generated_at": utc_now_iso(), "positions": positions}, path=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
