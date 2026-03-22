from __future__ import annotations

import argparse

from .common import dump_json, load_json, utc_now_iso
from .db import connect_db, create_strategy_authorization, init_db, list_strategy_authorizations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage strategy authorizations.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create an authorization from JSON.")
    create_parser.add_argument("--json-file", required=True, help="JSON file with authorization payload.")
    create_parser.add_argument("--output", help="Optional file path for JSON output.")

    list_parser = subparsers.add_parser("list", help="List authorizations.")
    list_parser.add_argument("--status", help="Optional status filter.")
    list_parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    with connect_db() as conn:
        if args.command == "create":
            payload = load_json(args.json_file)
            created = create_strategy_authorization(conn, payload)
            conn.commit()
            print(dump_json({"generated_at": utc_now_iso(), "authorization": created}, path=args.output))
            return 0
        if args.command == "list":
            items = list_strategy_authorizations(conn, status=args.status)
            print(dump_json({"generated_at": utc_now_iso(), "authorizations": items}, path=args.output))
            return 0
    raise RuntimeError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
