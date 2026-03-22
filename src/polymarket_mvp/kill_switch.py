from __future__ import annotations

import argparse

from .common import dump_json, utc_now_iso
from .db import connect_db, init_db, list_kill_switches, release_kill_switch, set_kill_switch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage kill switches.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    set_parser = subparsers.add_parser("set", help="Create an active kill switch.")
    set_parser.add_argument("--scope-type", required=True, choices=["global", "strategy", "market", "event_cluster"])
    set_parser.add_argument("--scope-key", required=True)
    set_parser.add_argument("--reason", required=True)
    set_parser.add_argument("--created-by")
    set_parser.add_argument("--output", help="Optional file path for JSON output.")

    list_parser = subparsers.add_parser("list", help="List kill switches.")
    list_parser.add_argument("--active-only", action="store_true")
    list_parser.add_argument("--output", help="Optional file path for JSON output.")

    release_parser = subparsers.add_parser("release", help="Release a kill switch.")
    release_parser.add_argument("--id", required=True, type=int)
    release_parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    with connect_db() as conn:
        if args.command == "set":
            item = set_kill_switch(
                conn,
                scope_type=args.scope_type,
                scope_key=args.scope_key,
                reason=args.reason,
                created_by=args.created_by,
            )
            conn.commit()
            print(dump_json({"generated_at": utc_now_iso(), "kill_switch": item}, path=args.output))
            return 0
        if args.command == "list":
            items = list_kill_switches(conn, active_only=args.active_only)
            print(dump_json({"generated_at": utc_now_iso(), "kill_switches": items}, path=args.output))
            return 0
        if args.command == "release":
            item = release_kill_switch(conn, args.id)
            conn.commit()
            print(dump_json({"generated_at": utc_now_iso(), "kill_switch": item}, path=args.output))
            return 0
    raise RuntimeError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
