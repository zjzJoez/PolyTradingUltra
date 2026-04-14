"""Import alpha_signals from Alpha Lab into polymarket-mvp proposals.

This is the only supported handoff path from the research system:
    alpha_signals (status='ready_for_import') -> importer -> proposals

Signals flow through the normal downstream pipeline after import:
risk_engine -> authorization -> approval -> execution.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any, Dict, List

from .common import (
    dump_json,
    parse_iso8601,
    utc_now_iso,
)
from .db import (
    connect_db,
    init_db,
    market_snapshot,
    upsert_proposal,
)


# ---------------------------------------------------------------------------
# Read signals from shared DB
# ---------------------------------------------------------------------------

def list_importable_signals(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Fetch alpha_signals with status='ready_for_import'."""
    rows = conn.execute(
        """
        SELECT * FROM alpha_signals
        WHERE status = 'ready_for_import'
        ORDER BY net_edge_bps DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def already_imported_signal_ids(conn: sqlite3.Connection) -> set[str]:
    """Return set of alpha_signal_ids already present on proposals."""
    rows = conn.execute(
        "SELECT alpha_signal_id FROM proposals WHERE alpha_signal_id IS NOT NULL"
    ).fetchall()
    return {str(r[0]) for r in rows}


def mark_signal_imported(conn: sqlite3.Connection, signal_id: str) -> None:
    """Mark an alpha_signal as imported after proposal creation succeeds."""
    conn.execute(
        """
        UPDATE alpha_signals
        SET status = 'imported', updated_at = ?
        WHERE signal_id = ?
        """,
        (utc_now_iso(), signal_id),
    )


# ---------------------------------------------------------------------------
# Signal -> Proposal conversion
# ---------------------------------------------------------------------------

def signal_to_proposal(signal: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an alpha_signal row into a normalized proposal dict."""
    explanation = signal.get("explanation_json") or "{}"
    if isinstance(explanation, str):
        try:
            explanation = json.loads(explanation)
        except (json.JSONDecodeError, TypeError):
            explanation = {}

    reasoning_parts = [
        f"Alpha Lab signal ({signal['strategy_name']} {signal.get('model_version', '')})",
        f"fair={signal['fair_probability']:.4f} vs market={signal['market_probability']:.4f}",
        f"net_edge={signal['net_edge_bps']:.0f}bps",
    ]
    if isinstance(explanation, dict) and explanation.get("summary"):
        reasoning_parts.append(str(explanation["summary"]))

    return {
        "market_id": signal["market_id"],
        "outcome": signal["outcome"],
        "confidence_score": min(max(float(signal.get("confidence_score", 0.6)), 0.0), 1.0),
        "recommended_size_usdc": float(signal["recommended_size_usdc"]),
        "reasoning": " | ".join(reasoning_parts),
        "max_slippage_bps": int(round(max(signal.get("net_edge_bps", 200) * 0.25, 100))),
    }


def signal_context_payload(signal: Dict[str, Any]) -> Dict[str, Any]:
    """Build the context_payload_json for an imported proposal."""
    explanation = signal.get("explanation_json") or "{}"
    if isinstance(explanation, str):
        try:
            explanation = json.loads(explanation)
        except (json.JSONDecodeError, TypeError):
            explanation = {}

    source_summary = signal.get("source_summary_json") or "{}"
    if isinstance(source_summary, str):
        try:
            source_summary = json.loads(source_summary)
        except (json.JSONDecodeError, TypeError):
            source_summary = {}

    quality_flags = signal.get("quality_flags_json") or "[]"
    if isinstance(quality_flags, str):
        try:
            quality_flags = json.loads(quality_flags)
        except (json.JSONDecodeError, TypeError):
            quality_flags = []

    return {
        "source": "alpha_lab",
        "signal_id": signal["signal_id"],
        "strategy_name": signal["strategy_name"],
        "model_version": signal.get("model_version"),
        "market_family": signal.get("market_family"),
        "fixture_id": signal.get("fixture_id"),
        "fair_probability": signal["fair_probability"],
        "market_probability": signal["market_probability"],
        "gross_edge_bps": signal["gross_edge_bps"],
        "net_edge_bps": signal["net_edge_bps"],
        "max_entry_price": signal.get("max_entry_price"),
        "mapping_confidence": signal.get("mapping_confidence"),
        "feature_freshness_seconds": signal.get("feature_freshness_seconds"),
        "confidence_score": signal.get("confidence_score"),
        "expected_clv_bps": signal.get("expected_clv_bps"),
        "signal_expires_at": signal.get("signal_expires_at"),
        "explanation": explanation,
        "source_summary": source_summary,
        "quality_flags": quality_flags,
    }


# ---------------------------------------------------------------------------
# Import orchestration
# ---------------------------------------------------------------------------

def is_signal_expired(signal: Dict[str, Any]) -> bool:
    """Check if a signal has passed its validity window."""
    expires_at = signal.get("signal_expires_at")
    if not expires_at:
        return False
    try:
        now = parse_iso8601(utc_now_iso())
        expiry = parse_iso8601(str(expires_at))
        return now >= expiry
    except (ValueError, TypeError):
        return False


def import_signals(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    max_signals: int | None = None,
) -> List[Dict[str, Any]]:
    """Import ready alpha_signals into proposals.

    Returns list of import result dicts.
    """
    signals = list_importable_signals(conn)
    existing_ids = already_imported_signal_ids(conn)

    results: List[Dict[str, Any]] = []
    imported_count = 0

    for signal in signals:
        signal_id = signal["signal_id"]

        # Skip duplicates
        if signal_id in existing_ids:
            results.append({"signal_id": signal_id, "action": "skipped_duplicate"})
            continue

        # Skip expired
        if is_signal_expired(signal):
            if not dry_run:
                conn.execute(
                    "UPDATE alpha_signals SET status='expired', updated_at=? WHERE signal_id=?",
                    (utc_now_iso(), signal_id),
                )
            results.append({"signal_id": signal_id, "action": "skipped_expired"})
            continue

        # Check market exists in shared DB
        mkt = market_snapshot(conn, signal["market_id"])
        if mkt is None:
            results.append({
                "signal_id": signal_id,
                "action": "skipped_no_market",
                "market_id": signal["market_id"],
            })
            continue

        # Cap import count
        if max_signals is not None and imported_count >= max_signals:
            results.append({"signal_id": signal_id, "action": "skipped_max_reached"})
            continue

        # Convert signal to proposal
        proposal = signal_to_proposal(signal)
        context = signal_context_payload(signal)

        if dry_run:
            results.append({
                "signal_id": signal_id,
                "action": "would_import",
                "proposal": proposal,
            })
            continue

        # Upsert proposal with alpha_lab metadata
        record = upsert_proposal(
            conn,
            proposal,
            decision_engine="alpha_lab",
            status="proposed",
            context_payload=context,
            strategy_name=signal["strategy_name"],
            topic=signal.get("market_family", "soccer"),
            alpha_signal_id=signal_id,
            alpha_fair_probability=signal["fair_probability"],
            alpha_market_probability=signal["market_probability"],
            alpha_gross_edge_bps=signal["gross_edge_bps"],
            alpha_net_edge_bps=signal["net_edge_bps"],
            alpha_model_version=signal.get("model_version"),
            alpha_mapping_confidence=signal.get("mapping_confidence"),
        )

        # Mark signal as imported only after proposal persistence succeeds
        mark_signal_imported(conn, signal_id)

        results.append({
            "signal_id": signal_id,
            "action": "imported",
            "proposal_id": record["proposal_id"],
        })
        imported_count += 1

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Import Alpha Lab signals into polymarket-mvp proposals."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview imports without writing proposals or updating signal status.",
    )
    parser.add_argument(
        "--max-signals",
        type=int,
        default=None,
        help="Maximum number of signals to import in this run.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Write import results JSON to this file.",
    )
    args = parser.parse_args(argv)

    init_db()
    conn = connect_db()

    try:
        results = import_signals(
            conn,
            dry_run=args.dry_run,
            max_signals=args.max_signals,
        )
        conn.commit()
    finally:
        conn.close()

    imported = [r for r in results if r["action"] == "imported"]
    skipped = [r for r in results if r["action"].startswith("skipped")]
    preview = [r for r in results if r["action"] == "would_import"]

    summary = {
        "imported": len(imported),
        "skipped": len(skipped),
        "preview": len(preview),
        "total_signals": len(results),
        "results": results,
    }

    if args.dry_run:
        print(f"[DRY RUN] Would import {len(preview)} signals, skip {len(skipped)}")
    else:
        print(f"Imported {len(imported)} signals, skipped {len(skipped)}")

    if args.output:
        dump_json(summary, args.output)
        print(f"Results written to {args.output}")
    else:
        print(dump_json(summary))


if __name__ == "__main__":
    main()
