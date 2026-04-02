"""Autopilot status helper — shows supervisor health, pending approvals, live orders."""
from __future__ import annotations

import argparse

from .common import dump_json, utc_now_iso
from .db import connect_db, init_db
from .ops_snapshot import build_ops_snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show autopilot supervisor status.")
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    with connect_db() as conn:
        status = build_ops_snapshot(conn)
    status["generated_at"] = utc_now_iso()
    print(dump_json(status, path=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
