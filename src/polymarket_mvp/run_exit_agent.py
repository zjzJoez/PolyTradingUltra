from __future__ import annotations

import argparse

from .agents.exit_agent import run_exit_agent
from .common import dump_json, utc_now_iso
from .db import connect_db, init_db, list_positions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate deterministic exit recommendations for open positions.")
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    with connect_db() as conn:
        positions = list_positions(conn, statuses=["open_requested", "open", "partially_filled"])
        recommendations = [run_exit_agent(conn, position) for position in positions]
        conn.commit()
    print(dump_json({"generated_at": utc_now_iso(), "recommendations": recommendations}, path=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
