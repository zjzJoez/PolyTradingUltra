from __future__ import annotations

import argparse

from .common import dump_json, get_db_path
from .db import init_db


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize or migrate the SQLite schema for Polymarket Trading OS.")
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    path = init_db()
    print(dump_json({"ok": True, "db_path": str(path or get_db_path())}, path=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
