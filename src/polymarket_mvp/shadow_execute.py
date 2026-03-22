from __future__ import annotations

import argparse

from .common import dump_json, proposal_id_for, read_proposals, utc_now_iso
from .db import connect_db, init_db, list_proposals_by_status, proposal_record
from .poly_executor import _current_worst_price
from .services.shadow_service import create_shadow_execution


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record shadow executions for proposals without sending live orders.")
    parser.add_argument("--proposal-file", help="Optional proposal JSON file.")
    parser.add_argument("--source", choices=["file", "authorized_queue"], default="file")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    created = []
    with connect_db() as conn:
        records = []
        if args.source == "authorized_queue":
            records = list_proposals_by_status(conn, ["authorized_for_execution"], limit=args.limit)
        else:
            if not args.proposal_file:
                raise RuntimeError("--proposal-file is required when --source=file")
            proposal_ids = [proposal_id_for(item) for item in read_proposals(args.proposal_file)]
            for proposal_id in proposal_ids:
                record = proposal_record(conn, proposal_id)
                if record is not None:
                    records.append(record)
        for record in records:
            simulated_price = _current_worst_price(record, mode="mock")
            created.append(create_shadow_execution(conn, record, simulated_fill_price=simulated_price))
        conn.commit()
    print(dump_json({"generated_at": utc_now_iso(), "shadow_executions": created}, path=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
