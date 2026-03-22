from __future__ import annotations

import argparse

from .common import dump_json, utc_now_iso
from .db import connect_db, init_db, list_positions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize positions by mode and status.")
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    with connect_db() as conn:
        positions = list_positions(conn)
    summary: dict[str, int] = {}
    for position in positions:
        key = f"{position.get('mode')}:{position.get('status')}"
        summary[key] = summary.get(key, 0) + 1
    print(dump_json({"generated_at": utc_now_iso(), "summary": summary, "positions": positions}, path=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
