"""Strategy B shadow harness — Odds API divergence detector.

Live mode only (no backtest — we don't have historical Odds API data cached).
Queries Odds API for current active sports markets, computes divergences,
persists to signal_events. Future cron runs will accumulate samples; as the
underlying Polymarket markets resolve, we can compute hit rate and CLV.

Usage:
    .venv/bin/python scripts/run_odds_divergence_shadow.py --mode=live
    .venv/bin/python scripts/run_odds_divergence_shadow.py --mode=live --edge-threshold=0.05
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

from polymarket_mvp.common import load_repo_env, utc_now_iso
from polymarket_mvp.db import connect_db, init_db
from polymarket_mvp.services.odds_divergence_signal import (
    DEFAULT_EDGE_THRESHOLD,
    SIGNAL_NAME,
    DivergenceSignal,
    signal_for_market,
)

load_repo_env()


def _row_to_market(row) -> Dict[str, Any]:
    m = dict(row)
    for field in ("market_json", "outcomes_json"):
        v = m.get(field)
        if isinstance(v, str):
            try:
                m[field] = json.loads(v)
            except Exception:
                pass
    if "outcomes" not in m and m.get("outcomes_json"):
        m["outcomes"] = m["outcomes_json"]
    elif "outcomes" not in m and isinstance(m.get("market_json"), dict):
        outs = m["market_json"].get("outcomes")
        if outs:
            m["outcomes"] = outs
    return m


def _persist(conn, signal: DivergenceSignal) -> None:
    size_rec = None
    if signal.recommendation == "bet":
        size_rec = min(2.0, max(0.5, abs(signal.edge) * 25.0))  # crude scale
    conn.execute(
        """
        INSERT INTO signal_events
        (signal_name, market_id, outcome, recommendation, model_p, market_p, edge,
         size_recommendation_usdc, payload_json, generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            SIGNAL_NAME,
            signal.market_id,
            signal.outcome,
            signal.recommendation,
            signal.consensus_p if signal.recommendation != "no_match" else None,
            signal.polymarket_p if signal.recommendation != "no_match" else None,
            signal.edge if signal.recommendation != "no_match" else None,
            size_rec,
            json.dumps(signal.to_dict(), default=str, ensure_ascii=False),
            utc_now_iso(),
        ),
    )


def run_live(edge_threshold: float, verbose: bool, market_limit: int) -> int:
    init_db()
    print(f"=== Odds API divergence shadow (LIVE), edge_threshold={edge_threshold} ===")
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM market_snapshots
            WHERE active = 1 AND accepting_orders = 1 AND closed = 0
            ORDER BY days_to_expiry ASC, volume_24h_usdc DESC
            LIMIT ?
            """,
            (market_limit,),
        ).fetchall()
        results: List[DivergenceSignal] = []
        bets: List[DivergenceSignal] = []
        no_match: List[DivergenceSignal] = []
        skips: List[DivergenceSignal] = []
        # Track which sport_keys we've already fetched so we don't waste budget
        # on duplicate calls. The signal helper hits Odds API on every call;
        # could batch in future iterations.
        for row in rows:
            market = _row_to_market(row)
            try:
                signal = signal_for_market(market, edge_threshold=edge_threshold)
            except Exception as exc:
                print(f"  error on {market.get('market_id')}: {exc}", file=sys.stderr)
                continue
            if signal is None:
                continue
            _persist(conn, signal)
            results.append(signal)
            if signal.recommendation == "bet":
                bets.append(signal)
            elif signal.recommendation == "no_match":
                no_match.append(signal)
            else:
                skips.append(signal)
        conn.commit()

    print(f"\nScanned: {len(rows)} markets")
    print(f"Recognised + evaluable shape: {len(results)}")
    print(f"  → bet candidates:   {len(bets)}")
    print(f"  → skip (edge < threshold): {len(skips)}")
    print(f"  → no_match:         {len(no_match)}")

    if bets:
        print(f"\n--- BET CANDIDATES ---")
        for s in sorted(bets, key=lambda x: -abs(x.edge))[:15]:
            print(f"  mkt={s.market_id} [{s.side}] {s.market_question[:60]}")
            print(f"     consensus_p={s.consensus_p:.3f} (n_books={s.book_count}) "
                  f"polymarket_p={s.polymarket_p:.3f} edge={s.edge:+.3f}")
            print(f"     {s.reasoning}")

    if verbose:
        print(f"\n--- all skips ---")
        for s in sorted(skips, key=lambda x: -abs(x.edge))[:15]:
            print(f"  mkt={s.market_id} edge={s.edge:+.3f} {s.market_question[:60]}")
        print(f"\n--- all no_match ---")
        for s in no_match[:15]:
            print(f"  mkt={s.market_id} {s.reasoning[:90]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["live"], default="live",
                        help="Backtest mode not supported — Odds API has no historical free tier.")
    parser.add_argument("--edge-threshold", type=float, default=DEFAULT_EDGE_THRESHOLD)
    parser.add_argument("--market-limit", type=int, default=80,
                        help="Maximum markets to evaluate per run.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    return run_live(args.edge_threshold, args.verbose, args.market_limit)


if __name__ == "__main__":
    sys.exit(main())
