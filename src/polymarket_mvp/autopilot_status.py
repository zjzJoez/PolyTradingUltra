"""Autopilot status helper — shows supervisor health, pending approvals, live orders."""
from __future__ import annotations

import argparse

from .common import dump_json, utc_now_iso
from .db import connect_db, init_db, list_executions, list_positions, list_proposals_by_status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show autopilot supervisor status.")
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    with connect_db() as conn:
        # Last heartbeat per loop
        heartbeats = conn.execute(
            """
            SELECT loop_name, MAX(started_at) as last_started,
                   MAX(finished_at) as last_finished
            FROM autopilot_heartbeats
            GROUP BY loop_name
            ORDER BY loop_name
            """
        ).fetchall()
        heartbeat_summary = [
            {"loop": row["loop_name"], "last_started": row["last_started"], "last_finished": row["last_finished"]}
            for row in heartbeats
        ]

        # Pending approvals with TTLs
        pending = list_proposals_by_status(conn, ["pending_approval"])
        pending_summary = [
            {
                "proposal_id": p["proposal_id"],
                "market": (p.get("market") or {}).get("question", ""),
                "approval_expires_at": p.get("approval_expires_at"),
                "telegram_message_id": p.get("telegram_message_id"),
            }
            for p in pending
        ]

        # Live orders
        live = list_executions(conn, statuses=["submitted", "live"], mode="real")
        live_summary = [
            {
                "execution_id": e["id"],
                "proposal_id": e["proposal_id"],
                "order_id": e.get("txhash_or_order_id"),
                "order_live_ttl": (e.get("order_intent_json") or {}).get("order_live_ttl_seconds"),
                "order_posted_at": (e.get("order_intent_json") or {}).get("order_posted_at"),
            }
            for e in live
        ]

        # Open positions
        positions = list_positions(conn, statuses=["open_requested", "open", "partially_filled"])
        position_summary = [
            {
                "id": p["id"],
                "market_id": p["market_id"],
                "outcome": p["outcome"],
                "size_usdc": p["size_usdc"],
                "status": p["status"],
                "unrealized_pnl": p.get("unrealized_pnl"),
            }
            for p in positions
        ]

    status = {
        "timestamp": utc_now_iso(),
        "heartbeats": heartbeat_summary,
        "pending_approvals": pending_summary,
        "pending_count": len(pending_summary),
        "live_orders": live_summary,
        "live_order_count": len(live_summary),
        "open_positions": position_summary,
        "open_position_count": len(position_summary),
    }
    print(dump_json(status, path=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
