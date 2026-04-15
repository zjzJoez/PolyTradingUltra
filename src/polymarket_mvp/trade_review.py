from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .common import dump_json, parse_iso8601, utc_now_iso

ISSUE_TAXONOMY = (
    "duplicate_exposure",
    "stale_state",
    "split_resolution",
    "execution_failure",
    "ops_lock",
    "market_type_concentration",
    "reconciliation_gap",
)

ESPORT_TERMS = (
    "counter-strike",
    "cs2",
    "valorant",
    "dota",
    "league of legends",
    "lol:",
    "esports",
    "map ",
    "bo3",
    "bo5",
    "lck",
    "fokus",
    "eyeballers",
    "hanwha life",
    "dplus kia",
)

MARKET_CATEGORY_LABELS = {
    "crypto_up_down": "Crypto Up/Down",
    "sports_totals": "Sports Totals",
    "sports_winner": "Sports Winner",
    "esports": "Esports",
    "other": "Other",
}


def _parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return parse_iso8601(text).isoformat().replace("+00:00", "Z")
    except Exception:
        return text


def _safe_seconds_between(later: str | None, earlier: str | None) -> float | None:
    if not later or not earlier:
        return None
    try:
        return (parse_iso8601(later) - parse_iso8601(earlier)).total_seconds()
    except Exception:
        return None


def _safe_day(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return parse_iso8601(value).date().isoformat()
    except Exception:
        return value[:10]


def _round_money(value: float | None) -> float:
    return round(float(value or 0.0), 6)


def _format_money(value: float | None) -> str:
    amount = float(value or 0.0)
    sign = "+" if amount > 0 else ""
    return f"{sign}${amount:,.2f}"


def _format_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _bucket_confidence(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 0.40:
        return "<0.40"
    if value < 0.50:
        return "0.40-0.49"
    if value < 0.60:
        return "0.50-0.59"
    if value < 0.70:
        return "0.60-0.69"
    return "0.70+"


def _bucket_size(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value <= 5.0:
        return "<=5"
    if value <= 10.0:
        return "5-10"
    return ">10"


def _severity_rank(value: str) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(value, 4)


def _resolution_effective_at(resolution: Mapping[str, Any] | None) -> str | None:
    if not resolution:
        return None
    payload = resolution.get("source_payload_json") or {}
    for key in ("closedTime", "umaEndDate", "updatedAt", "resolved_at"):
        candidate = payload.get(key) if isinstance(payload, Mapping) else None
        normalized = _iso_or_none(candidate)
        if normalized:
            return normalized
    return _iso_or_none(resolution.get("resolved_at"))


def _resolution_payout_map(resolution: Mapping[str, Any] | None) -> dict[str, float]:
    if not resolution:
        return {}
    payload = resolution.get("source_payload_json") or {}
    raw_outcomes = payload.get("outcomes")
    raw_prices = payload.get("outcomePrices")
    raw_outcomes = _parse_json(raw_outcomes)
    raw_prices = _parse_json(raw_prices)
    if isinstance(raw_outcomes, list) and isinstance(raw_prices, list) and len(raw_outcomes) == len(raw_prices):
        result: dict[str, float] = {}
        for name, price in zip(raw_outcomes, raw_prices):
            parsed = _to_float(price)
            if parsed is None:
                continue
            result[str(name)] = parsed
        if result:
            return result
    resolved_outcome = resolution.get("resolved_outcome")
    if resolved_outcome:
        return {str(resolved_outcome): 1.0}
    return {}


def _is_split_resolution(resolution: Mapping[str, Any] | None) -> bool:
    payout_map = _resolution_payout_map(resolution)
    if not payout_map:
        return False
    values = list(payout_map.values())
    if sum(1 for value in values if value >= 0.99) == 1 and all(value in {0.0, 1.0} for value in values):
        return False
    return True


def classify_market(market: Mapping[str, Any] | None) -> str:
    if not market:
        return "other"
    question = str(market.get("question") or "").lower()
    slug = str(market.get("slug") or "").lower()
    market_json = market.get("market_json") or {}
    sports_type = str(market_json.get("sportsMarketType") or market.get("sportsMarketType") or "").lower()
    resolution_source = str(market_json.get("resolutionSource") or market.get("resolutionSource") or "").lower()
    if "up or down" in question or "updown" in slug or "data.chain.link/streams" in resolution_source:
        return "crypto_up_down"
    if any(term in question for term in ESPORT_TERMS):
        return "esports"
    if sports_type == "totals" or "o/u" in question or "total:" in question or "games total" in question:
        return "sports_totals"
    if " will " in f" {question} " and " win " in f" {question} ":
        return "sports_winner"
    if sports_type in {"moneyline", "winner"}:
        return "sports_winner"
    return "other"


def _is_short_horizon_crypto(market: Mapping[str, Any] | None) -> bool:
    if not market:
        return False
    question = str(market.get("question") or "").lower()
    return classify_market(market) == "crypto_up_down" and " - " in question


def _execution_failure_category(message: str | None) -> str:
    text = (message or "").lower()
    if "real_preflight_failed" in text:
        return "real_preflight_failed"
    if "service not ready" in text:
        return "service_not_ready"
    if "request exception" in text:
        return "request_exception"
    if "order_submit_failed" in text:
        return "order_submit_failed"
    if "slippage_exceeded" in text:
        return "slippage_exceeded"
    if "insufficient_balance" in text or "insufficient collateral" in text:
        return "insufficient_balance"
    return "other_failure"


def _estimate_status_path(proposal: Mapping[str, Any], executions: Sequence[Mapping[str, Any]], positions: Sequence[Mapping[str, Any]]) -> str:
    steps = ["proposed"]
    status = str(proposal.get("status") or "")
    if status == "risk_blocked":
        steps.append("risk_blocked")
    if proposal.get("approval_requested_at"):
        steps.append("pending_approval")
    approval = proposal.get("approval") or {}
    decision = approval.get("decision")
    if decision:
        steps.append(str(decision))
    if proposal.get("authorization_status") == "matched_auto_execute":
        steps.append("authorized_for_execution")
    for execution in executions:
        steps.append(str(execution.get("status") or "execution"))
    if positions:
        final_statuses = sorted({str(position.get("status") or "") for position in positions})
        steps.extend(final_statuses)
    if not executions and status not in {"risk_blocked", "expired", "failed"} and status:
        steps.append(status)
    deduped: list[str] = []
    for step in steps:
        if not deduped or deduped[-1] != step:
            deduped.append(step)
    return ">".join(deduped)


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})


def _write_svg_bar_chart(path: Path, title: str, rows: Sequence[Mapping[str, Any]], *, label_key: str, value_key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text(
            "<svg xmlns='http://www.w3.org/2000/svg' width='640' height='120'><text x='20' y='40'>No data</text></svg>\n",
            encoding="utf-8",
        )
        return
    width = 960
    margin_left = 240
    row_height = 28
    chart_height = max(160, 80 + row_height * len(rows))
    values = [float(row.get(value_key) or 0.0) for row in rows]
    max_abs = max(1.0, max(abs(value) for value in values))
    zero_x = margin_left + 300
    bar_span = 320
    lines = [
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{chart_height}'>",
        "<style>text{font-family:Menlo,monospace;font-size:12px;} .title{font-size:16px;font-weight:bold;} .axis{stroke:#999;stroke-width:1;} .pos{fill:#2c7a4b;} .neg{fill:#b33a3a;}</style>",
        f"<text class='title' x='20' y='28'>{title}</text>",
        f"<line class='axis' x1='{zero_x}' y1='48' x2='{zero_x}' y2='{chart_height - 16}' />",
    ]
    for idx, row in enumerate(rows):
        value = float(row.get(value_key) or 0.0)
        label = str(row.get(label_key) or "")
        y = 60 + idx * row_height
        bar_width = max(1.0, abs(value) / max_abs * bar_span)
        if value >= 0:
            x = zero_x
            cls = "pos"
        else:
            x = zero_x - bar_width
            cls = "neg"
        lines.append(f"<text x='20' y='{y + 12}'>{label}</text>")
        lines.append(f"<rect class='{cls}' x='{x:.1f}' y='{y}' width='{bar_width:.1f}' height='16' rx='2' ry='2' />")
        lines.append(f"<text x='{zero_x + bar_span + 20}' y='{y + 12}'>{value:.2f}</text>")
    lines.append("</svg>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _copy_snapshot(source_db: Path, snapshot_db: Path, *, attempts: int = 5, sleep_seconds: float = 0.5) -> None:
    snapshot_db.parent.mkdir(parents=True, exist_ok=True)
    if snapshot_db.exists():
        snapshot_db.unlink()
    last_error: Exception | None = None
    for attempt in range(attempts):
        source_conn = None
        dest_conn = None
        try:
            source_conn = sqlite3.connect(f"file:{source_db.resolve()}?mode=ro", uri=True, timeout=30.0)
            dest_conn = sqlite3.connect(snapshot_db, timeout=30.0)
            source_conn.backup(dest_conn)
            dest_conn.commit()
            return
        except sqlite3.Error as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(sleep_seconds * (attempt + 1))
                continue
            raise
        finally:
            if dest_conn is not None:
                dest_conn.close()
            if source_conn is not None:
                source_conn.close()
    if last_error is not None:
        raise last_error


def _load_table(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if not exists:
        return []
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return [dict(row) for row in rows]


def _load_snapshot(snapshot_db: Path) -> dict[str, Any]:
    conn = sqlite3.connect(snapshot_db, timeout=60.0)
    conn.row_factory = sqlite3.Row
    try:
        markets = _load_table(conn, "market_snapshots")
        proposals = _load_table(conn, "proposals")
        executions = _load_table(conn, "executions")
        positions = _load_table(conn, "positions")
        approvals = _load_table(conn, "approvals")
        resolutions = _load_table(conn, "market_resolutions")
        reconciliations = _load_table(conn, "order_reconciliations")
        heartbeats = _load_table(conn, "autopilot_heartbeats")
        position_events = _load_table(conn, "position_events")
        agent_reviews = _load_table(conn, "agent_reviews")
        shadow_executions = _load_table(conn, "shadow_executions")
    finally:
        conn.close()

    for market in markets:
        market["outcomes_json"] = _parse_json(market.get("outcomes_json"))
        market["market_json"] = _parse_json(market.get("market_json"))
        market["market_category"] = classify_market(market)
        market["is_short_horizon_crypto"] = _is_short_horizon_crypto(market)
    for proposal in proposals:
        proposal["proposal_json"] = _parse_json(proposal.get("proposal_json"))
        proposal["context_payload_json"] = _parse_json(proposal.get("context_payload_json"))
    for execution in executions:
        execution["order_intent_json"] = _parse_json(execution.get("order_intent_json"))
    for approval in approvals:
        approval["raw_callback_json"] = _parse_json(approval.get("raw_callback_json"))
    for resolution in resolutions:
        resolution["source_payload_json"] = _parse_json(resolution.get("source_payload_json"))
        resolution["resolution_effective_at"] = _resolution_effective_at(resolution)
        resolution["payout_map"] = _resolution_payout_map(resolution)
        resolution["is_split_resolution"] = _is_split_resolution(resolution)
    for reconciliation in reconciliations:
        reconciliation["payload_json"] = _parse_json(reconciliation.get("payload_json"))
    for event in position_events:
        event["payload_json"] = _parse_json(event.get("payload_json"))
    for review in agent_reviews:
        review["payload_json"] = _parse_json(review.get("payload_json"))
        review["what_worked"] = _parse_json(review.get("what_worked"))
        review["what_failed"] = _parse_json(review.get("what_failed"))
    for shadow_execution in shadow_executions:
        shadow_execution["context_json"] = _parse_json(shadow_execution.get("context_json"))

    markets_by_id = {str(market["market_id"]): market for market in markets}
    approvals_by_proposal = {str(approval["proposal_id"]): approval for approval in approvals}
    proposals_by_id = {str(proposal["proposal_id"]): proposal for proposal in proposals}
    resolutions_by_market = {str(resolution["market_id"]): resolution for resolution in resolutions}
    executions_by_proposal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    positions_by_execution: dict[int, list[dict[str, Any]]] = defaultdict(list)
    reconciliations_by_execution: dict[int, list[dict[str, Any]]] = defaultdict(list)
    events_by_position: dict[int, list[dict[str, Any]]] = defaultdict(list)
    reviews_by_position: dict[int, list[dict[str, Any]]] = defaultdict(list)

    for execution in executions:
        executions_by_proposal[str(execution["proposal_id"])].append(execution)
    for execution_list in executions_by_proposal.values():
        execution_list.sort(key=lambda item: (item.get("created_at") or "", int(item.get("id") or 0)))
    for position in positions:
        positions_by_execution[int(position["execution_id"])].append(position)
    for reconciliation in reconciliations:
        reconciliations_by_execution[int(reconciliation["execution_id"])].append(reconciliation)
    for event in position_events:
        events_by_position[int(event["position_id"])].append(event)
    for review in agent_reviews:
        position_id = review.get("position_id")
        if position_id is not None:
            reviews_by_position[int(position_id)].append(review)

    for proposal in proposals:
        proposal_id = str(proposal["proposal_id"])
        market = markets_by_id.get(str(proposal["market_id"]))
        proposal["market"] = market
        proposal["approval"] = approvals_by_proposal.get(proposal_id)
        proposal["executions"] = executions_by_proposal.get(proposal_id, [])
        proposal_positions: list[dict[str, Any]] = []
        for execution in proposal["executions"]:
            proposal_positions.extend(positions_by_execution.get(int(execution["id"]), []))
        proposal["positions"] = sorted(proposal_positions, key=lambda item: (item.get("entry_time") or "", int(item.get("id") or 0)))
        proposal["market_category"] = classify_market(market)
        proposal["resolved_outcome"] = (resolutions_by_market.get(str(proposal["market_id"])) or {}).get("resolved_outcome")
        proposal["risk_block_reason"] = "not_persisted" if proposal.get("status") == "risk_blocked" else ""
        proposal["status_path_estimate"] = _estimate_status_path(proposal, proposal["executions"], proposal["positions"])

    position_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for position in positions:
        position_groups[(str(position["market_id"]), str(position["outcome"]))].append(position)
    for group in position_groups.values():
        group.sort(key=lambda item: (item.get("entry_time") or "", int(item.get("id") or 0)))

    for execution in executions:
        execution["proposal"] = proposals_by_id.get(str(execution["proposal_id"]))
        execution["market"] = markets_by_id.get(str((execution["proposal"] or {}).get("market_id") or ""))
        execution["positions"] = positions_by_execution.get(int(execution["id"]), [])
        execution["reconciliations"] = reconciliations_by_execution.get(int(execution["id"]), [])
        execution["failure_category"] = _execution_failure_category(execution.get("error_message"))

    positions_by_id = {int(position["id"]): position for position in positions}
    for position in positions:
        proposal = proposals_by_id.get(str(position["proposal_id"]))
        resolution = resolutions_by_market.get(str(position["market_id"]))
        duplicates = position_groups[(str(position["market_id"]), str(position["outcome"]))]
        position["proposal"] = proposal
        position["market"] = markets_by_id.get(str(position["market_id"]))
        position["resolution"] = resolution
        position["events"] = events_by_position.get(int(position["id"]), [])
        position["reviews"] = reviews_by_position.get(int(position["id"]), [])
        position["market_category"] = classify_market(position.get("market"))
        position["is_split_resolution"] = _is_split_resolution(resolution)
        position["duplicate_entry_group_size"] = len(duplicates)
        position["is_duplicate_entry"] = len(duplicates) > 1
        position["duplicate_entry_rank"] = duplicates.index(position) + 1
        position["resolution_effective_at"] = _resolution_effective_at(resolution)
        position["holding_minutes"] = (
            _safe_seconds_between(position.get("resolution_effective_at"), position.get("entry_time")) or 0.0
        ) / 60.0 if position.get("resolution_effective_at") else None
        payout_map = _resolution_payout_map(resolution)
        position["resolution_payout"] = payout_map.get(str(position["outcome"])) if payout_map else None

    return {
        "markets": markets,
        "proposals": proposals,
        "executions": executions,
        "positions": positions,
        "approvals": approvals,
        "resolutions": resolutions,
        "reconciliations": reconciliations,
        "heartbeats": heartbeats,
        "position_events": position_events,
        "agent_reviews": agent_reviews,
        "shadow_executions": shadow_executions,
        "positions_by_id": positions_by_id,
    }


def _build_proposal_funnel_rows(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for proposal in data["proposals"]:
        market = proposal.get("market") or {}
        approval = proposal.get("approval") or {}
        executions = proposal.get("executions") or []
        positions = proposal.get("positions") or []
        latest_execution = executions[-1] if executions else None
        rows.append(
            {
                "proposal_id": proposal["proposal_id"],
                "created_at": proposal.get("created_at"),
                "updated_at": proposal.get("updated_at"),
                "market_id": proposal.get("market_id"),
                "question": market.get("question"),
                "market_category": proposal.get("market_category"),
                "outcome": proposal.get("outcome"),
                "strategy_name": proposal.get("strategy_name"),
                "decision_engine": proposal.get("decision_engine"),
                "proposal_kind": proposal.get("proposal_kind"),
                "confidence_score": proposal.get("confidence_score"),
                "confidence_bucket": _bucket_confidence(_to_float(proposal.get("confidence_score"))),
                "recommended_size_usdc": proposal.get("recommended_size_usdc"),
                "size_bucket": _bucket_size(_to_float(proposal.get("recommended_size_usdc"))),
                "status": proposal.get("status"),
                "status_path_estimate": proposal.get("status_path_estimate"),
                "risk_block_reason": proposal.get("risk_block_reason"),
                "authorization_status": proposal.get("authorization_status"),
                "supervisor_decision": proposal.get("supervisor_decision"),
                "approval_requested_at": proposal.get("approval_requested_at"),
                "approval_expires_at": proposal.get("approval_expires_at"),
                "approval_decision": approval.get("decision"),
                "approval_decided_at": approval.get("decided_at"),
                "execution_count": len(executions),
                "executed": 1 if executions else 0,
                "latest_execution_id": latest_execution.get("id") if latest_execution else None,
                "latest_execution_status": latest_execution.get("status") if latest_execution else None,
                "position_count": len(positions),
                "final_position_status": "|".join(sorted({str(position.get("status") or "") for position in positions})),
                "resolved_outcome": proposal.get("resolved_outcome"),
                "is_split_resolution": 1 if _is_split_resolution((positions[0].get("resolution") if positions else None)) else 0,
            }
        )
    return rows


def _build_execution_rows(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for execution in data["executions"]:
        proposal = execution.get("proposal") or {}
        market = execution.get("market") or {}
        resolution = None
        if execution.get("positions"):
            resolution = execution["positions"][0].get("resolution")
        rows.append(
            {
                "execution_id": execution.get("id"),
                "proposal_id": execution.get("proposal_id"),
                "created_at": execution.get("created_at"),
                "updated_at": execution.get("updated_at"),
                "mode": execution.get("mode"),
                "status": execution.get("status"),
                "failure_category": execution.get("failure_category"),
                "error_message": execution.get("error_message"),
                "client_order_id": execution.get("client_order_id"),
                "order_id": execution.get("txhash_or_order_id"),
                "requested_price": execution.get("requested_price"),
                "avg_fill_price": execution.get("avg_fill_price"),
                "requested_size_usdc": execution.get("requested_size_usdc"),
                "filled_size_usdc": execution.get("filled_size_usdc"),
                "market_id": proposal.get("market_id"),
                "question": market.get("question"),
                "market_category": classify_market(market),
                "outcome": proposal.get("outcome"),
                "strategy_name": proposal.get("strategy_name"),
                "decision_engine": proposal.get("decision_engine"),
                "confidence_score": proposal.get("confidence_score"),
                "proposal_status": proposal.get("status"),
                "position_count": len(execution.get("positions") or []),
                "reconciliation_count": len(execution.get("reconciliations") or []),
                "resolved_outcome": (resolution or {}).get("resolved_outcome"),
                "is_split_resolution": 1 if _is_split_resolution(resolution) else 0,
            }
        )
    return rows


def _build_position_rows(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for position in data["positions"]:
        market = position.get("market") or {}
        proposal = position.get("proposal") or {}
        resolution = position.get("resolution") or {}
        rows.append(
            {
                "position_id": position.get("id"),
                "proposal_id": position.get("proposal_id"),
                "execution_id": position.get("execution_id"),
                "market_id": position.get("market_id"),
                "question": market.get("question"),
                "market_category": position.get("market_category"),
                "outcome": position.get("outcome"),
                "status": position.get("status"),
                "entry_time": position.get("entry_time"),
                "resolution_effective_at": position.get("resolution_effective_at"),
                "holding_minutes": round(float(position["holding_minutes"]), 2) if position.get("holding_minutes") is not None else None,
                "entry_price": position.get("entry_price"),
                "last_mark_price": position.get("last_mark_price"),
                "resolution_payout": position.get("resolution_payout"),
                "size_usdc": position.get("size_usdc"),
                "filled_qty": position.get("filled_qty"),
                "unrealized_pnl": position.get("unrealized_pnl"),
                "realized_pnl": position.get("realized_pnl"),
                "resolved_outcome": resolution.get("resolved_outcome"),
                "is_split_resolution": 1 if position.get("is_split_resolution") else 0,
                "is_duplicate_entry": 1 if position.get("is_duplicate_entry") else 0,
                "duplicate_entry_group_size": position.get("duplicate_entry_group_size"),
                "duplicate_entry_rank": position.get("duplicate_entry_rank"),
                "strategy_name": position.get("strategy_name") or proposal.get("strategy_name"),
                "decision_engine": proposal.get("decision_engine"),
                "confidence_score": proposal.get("confidence_score"),
                "confidence_bucket": _bucket_confidence(_to_float(proposal.get("confidence_score"))),
                "size_bucket": _bucket_size(_to_float(position.get("size_usdc"))),
            }
        )
    return rows


def _build_market_rows(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    proposals_by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    executions_by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    positions_by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for proposal in data["proposals"]:
        proposals_by_market[str(proposal["market_id"])].append(proposal)
    for execution in data["executions"]:
        market_id = str((execution.get("proposal") or {}).get("market_id") or "")
        if market_id:
            executions_by_market[market_id].append(execution)
    for position in data["positions"]:
        positions_by_market[str(position["market_id"])].append(position)
    rows: list[dict[str, Any]] = []
    for market in data["markets"]:
        market_id = str(market["market_id"])
        resolution = next((item for item in data["resolutions"] if str(item["market_id"]) == market_id), None)
        market_positions = positions_by_market.get(market_id, [])
        realized_pnl = sum(_to_float(position.get("realized_pnl")) or 0.0 for position in market_positions)
        duplicate_groups = Counter((str(position.get("market_id")), str(position.get("outcome"))) for position in market_positions)
        rows.append(
            {
                "market_id": market_id,
                "question": market.get("question"),
                "slug": market.get("slug"),
                "market_category": market.get("market_category"),
                "market_category_label": MARKET_CATEGORY_LABELS.get(str(market.get("market_category") or "other"), "Other"),
                "is_short_horizon_crypto": 1 if market.get("is_short_horizon_crypto") else 0,
                "active": market.get("active"),
                "closed": market.get("closed"),
                "accepting_orders": market.get("accepting_orders"),
                "end_date": market.get("end_date"),
                "liquidity_usdc": market.get("liquidity_usdc"),
                "volume_usdc": market.get("volume_usdc"),
                "volume_24h_usdc": market.get("volume_24h_usdc"),
                "proposal_count": len(proposals_by_market.get(market_id, [])),
                "execution_count": len(executions_by_market.get(market_id, [])),
                "position_count": len(market_positions),
                "duplicate_outcome_groups": sum(1 for count in duplicate_groups.values() if count > 1),
                "resolved_outcome": (resolution or {}).get("resolved_outcome"),
                "is_split_resolution": 1 if _is_split_resolution(resolution) else 0,
                "resolution_effective_at": _resolution_effective_at(resolution),
                "total_realized_pnl": _round_money(realized_pnl),
            }
        )
    return rows


def _build_ops_timeline_rows(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for heartbeat in data["heartbeats"]:
        rows.append(
            {
                "event_time": heartbeat.get("started_at"),
                "event_kind": "heartbeat",
                "source": "autopilot_heartbeats",
                "severity": "high" if "database is locked" in str(heartbeat.get("error_message") or "").lower() else ("medium" if heartbeat.get("error_message") else "low"),
                "loop_name": heartbeat.get("loop_name"),
                "proposal_id": "",
                "execution_id": "",
                "market_id": "",
                "duration_seconds": _safe_seconds_between(heartbeat.get("finished_at"), heartbeat.get("started_at")),
                "items_processed": heartbeat.get("items_processed"),
                "detail": heartbeat.get("error_message") or "",
            }
        )
    for execution in data["executions"]:
        if execution.get("status") != "failed":
            continue
        proposal = execution.get("proposal") or {}
        rows.append(
            {
                "event_time": execution.get("created_at"),
                "event_kind": "execution_failure",
                "source": "executions",
                "severity": "high",
                "loop_name": "",
                "proposal_id": execution.get("proposal_id"),
                "execution_id": execution.get("id"),
                "market_id": proposal.get("market_id"),
                "duration_seconds": "",
                "items_processed": "",
                "detail": execution.get("error_message") or "",
            }
        )
    for reconciliation in data["reconciliations"]:
        rows.append(
            {
                "event_time": reconciliation.get("created_at"),
                "event_kind": "reconciliation",
                "source": "order_reconciliations",
                "severity": "medium" if reconciliation.get("reconciliation_result") == "error" else "low",
                "loop_name": "",
                "proposal_id": "",
                "execution_id": reconciliation.get("execution_id"),
                "market_id": "",
                "duration_seconds": "",
                "items_processed": "",
                "detail": reconciliation.get("reconciliation_result"),
            }
        )
    for resolution in data["resolutions"]:
        rows.append(
            {
                "event_time": _resolution_effective_at(resolution),
                "event_kind": "market_resolution",
                "source": "market_resolutions",
                "severity": "medium" if _is_split_resolution(resolution) else "low",
                "loop_name": "",
                "proposal_id": "",
                "execution_id": "",
                "market_id": resolution.get("market_id"),
                "duration_seconds": "",
                "items_processed": "",
                "detail": resolution.get("resolved_outcome"),
            }
        )
    rows.sort(key=lambda item: (item.get("event_time") or "", item.get("event_kind") or "", str(item.get("execution_id") or "")))
    return rows


def _build_daily_stats(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "day": "",
        "proposal_count": 0,
        "execution_count": 0,
        "gross_exposure_usdc": 0.0,
        "resolved_positions": 0,
        "winning_positions": 0,
        "losing_positions": 0,
        "realized_pnl": 0.0,
    })
    for proposal in data["proposals"]:
        day = _safe_day(proposal.get("created_at"))
        if not day:
            continue
        stats[day]["day"] = day
        stats[day]["proposal_count"] += 1
    for execution in data["executions"]:
        day = _safe_day(execution.get("created_at"))
        if not day:
            continue
        stats[day]["day"] = day
        stats[day]["execution_count"] += 1
        stats[day]["gross_exposure_usdc"] += _to_float(execution.get("requested_size_usdc")) or 0.0
    for position in data["positions"]:
        if position.get("status") != "resolved":
            continue
        day = _safe_day(position.get("resolution_effective_at") or position.get("updated_at"))
        if not day:
            continue
        realized = _to_float(position.get("realized_pnl")) or 0.0
        stats[day]["day"] = day
        stats[day]["resolved_positions"] += 1
        stats[day]["realized_pnl"] += realized
        if realized > 0:
            stats[day]["winning_positions"] += 1
        elif realized < 0:
            stats[day]["losing_positions"] += 1
    rows = []
    for day in sorted(stats):
        item = stats[day]
        item["gross_exposure_usdc"] = _round_money(item["gross_exposure_usdc"])
        item["realized_pnl"] = _round_money(item["realized_pnl"])
        rows.append(item)
    return rows


def _aggregate_group(rows: Sequence[Mapping[str, Any]], *, group_key: str, amount_key: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"group": "", "count": 0, "amount": 0.0})
    for row in rows:
        group = str(row.get(group_key) or "unknown")
        grouped[group]["group"] = group
        grouped[group]["count"] += 1
        grouped[group]["amount"] += _to_float(row.get(amount_key)) or 0.0
    items = list(grouped.values())
    items.sort(key=lambda item: (-abs(item["amount"]), item["group"]))
    for item in items:
        item["amount"] = _round_money(item["amount"])
    return items


def _build_strategy_stats(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "strategy_name": "",
        "proposal_count": 0,
        "executed_proposals": 0,
        "resolved_positions": 0,
        "winning_positions": 0,
        "losing_positions": 0,
        "realized_pnl": 0.0,
    })
    for proposal in data["proposals"]:
        name = str(proposal.get("strategy_name") or "unknown")
        grouped[name]["strategy_name"] = name
        grouped[name]["proposal_count"] += 1
        if proposal.get("executions"):
            grouped[name]["executed_proposals"] += 1
    for position in data["positions"]:
        name = str(position.get("strategy_name") or "unknown")
        grouped[name]["strategy_name"] = name
        if position.get("status") == "resolved":
            grouped[name]["resolved_positions"] += 1
            realized = _to_float(position.get("realized_pnl")) or 0.0
            grouped[name]["realized_pnl"] += realized
            if realized > 0:
                grouped[name]["winning_positions"] += 1
            elif realized < 0:
                grouped[name]["losing_positions"] += 1
    rows = list(grouped.values())
    for row in rows:
        resolved = row["resolved_positions"]
        row["realized_pnl"] = _round_money(row["realized_pnl"])
        row["win_rate"] = (row["winning_positions"] / resolved) if resolved else None
    rows.sort(key=lambda item: (-item["proposal_count"], item["strategy_name"]))
    return rows


def _build_market_category_stats(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "market_category": "",
        "proposal_count": 0,
        "execution_count": 0,
        "resolved_positions": 0,
        "gross_exposure_usdc": 0.0,
        "realized_pnl": 0.0,
        "gross_losses": 0.0,
    })
    for proposal in data["proposals"]:
        category = str(proposal.get("market_category") or "other")
        grouped[category]["market_category"] = category
        grouped[category]["proposal_count"] += 1
    for execution in data["executions"]:
        category = classify_market(execution.get("market"))
        grouped[category]["market_category"] = category
        grouped[category]["execution_count"] += 1
        grouped[category]["gross_exposure_usdc"] += _to_float(execution.get("requested_size_usdc")) or 0.0
    for position in data["positions"]:
        category = str(position.get("market_category") or "other")
        grouped[category]["market_category"] = category
        if position.get("status") == "resolved":
            realized = _to_float(position.get("realized_pnl")) or 0.0
            grouped[category]["resolved_positions"] += 1
            grouped[category]["realized_pnl"] += realized
            if realized < 0:
                grouped[category]["gross_losses"] += abs(realized)
    rows = list(grouped.values())
    rows.sort(key=lambda item: (-item["proposal_count"], item["market_category"]))
    for row in rows:
        row["gross_exposure_usdc"] = _round_money(row["gross_exposure_usdc"])
        row["realized_pnl"] = _round_money(row["realized_pnl"])
        row["gross_losses"] = _round_money(row["gross_losses"])
    return rows


def _build_bucket_stats(data: Mapping[str, Any], *, bucket_key: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "bucket": "",
        "resolved_positions": 0,
        "winning_positions": 0,
        "losing_positions": 0,
        "realized_pnl": 0.0,
    })
    for position in data["positions"]:
        if position.get("status") != "resolved":
            continue
        proposal = position.get("proposal") or {}
        if bucket_key == "confidence":
            bucket = _bucket_confidence(_to_float(proposal.get("confidence_score")))
        else:
            bucket = _bucket_size(_to_float(position.get("size_usdc")))
        grouped[bucket]["bucket"] = bucket
        grouped[bucket]["resolved_positions"] += 1
        realized = _to_float(position.get("realized_pnl")) or 0.0
        grouped[bucket]["realized_pnl"] += realized
        if realized > 0:
            grouped[bucket]["winning_positions"] += 1
        elif realized < 0:
            grouped[bucket]["losing_positions"] += 1
    rows = list(grouped.values())
    order = {"<0.40": 0, "0.40-0.49": 1, "0.50-0.59": 2, "0.60-0.69": 3, "0.70+": 4, "<=5": 0, "5-10": 1, ">10": 2}
    rows.sort(key=lambda item: order.get(item["bucket"], 99))
    for row in rows:
        resolved = row["resolved_positions"]
        row["realized_pnl"] = _round_money(row["realized_pnl"])
        row["win_rate"] = (row["winning_positions"] / resolved) if resolved else None
    return rows


def _build_market_outcome_pnl(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {
        "market_id": "",
        "question": "",
        "outcome": "",
        "positions": 0,
        "realized_pnl": 0.0,
        "gross_size": 0.0,
        "duplicate_entries": 0,
        "market_category": "",
    })
    for position in data["positions"]:
        key = (str(position["market_id"]), str(position["outcome"]))
        market = position.get("market") or {}
        grouped[key]["market_id"] = key[0]
        grouped[key]["question"] = market.get("question")
        grouped[key]["outcome"] = key[1]
        grouped[key]["positions"] += 1
        grouped[key]["realized_pnl"] += _to_float(position.get("realized_pnl")) or 0.0
        grouped[key]["gross_size"] += _to_float(position.get("size_usdc")) or 0.0
        grouped[key]["market_category"] = classify_market(market)
        if position.get("is_duplicate_entry"):
            grouped[key]["duplicate_entries"] = max(grouped[key]["duplicate_entries"], int(position.get("duplicate_entry_group_size") or 0))
    rows = list(grouped.values())
    rows.sort(key=lambda item: (item["realized_pnl"], -item["gross_size"]))
    for row in rows:
        row["realized_pnl"] = _round_money(row["realized_pnl"])
        row["gross_size"] = _round_money(row["gross_size"])
    return rows


def _build_incidents(data: Mapping[str, Any], market_outcome_pnl: Sequence[Mapping[str, Any]], market_category_stats: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    incidents: list[dict[str, Any]] = []

    for item in market_outcome_pnl:
        duplicate_entries = int(item.get("duplicate_entries") or 0)
        if duplicate_entries <= 1:
            continue
        realized = _to_float(item.get("realized_pnl")) or 0.0
        severity = "critical" if realized < 0 else "high"
        incidents.append(
            {
                "issue_type": "duplicate_exposure",
                "severity": severity,
                "first_seen": "",
                "last_seen": "",
                "affected_rows": duplicate_entries,
                "evidence": json.dumps({
                    "market_id": item.get("market_id"),
                    "outcome": item.get("outcome"),
                    "positions": item.get("positions"),
                    "realized_pnl": item.get("realized_pnl"),
                }, ensure_ascii=False, sort_keys=False),
                "hypothesis": "Same market/outcome was entered multiple times, amplifying exposure without a differentiated thesis.",
                "recommendation": "Block repeated entry on the same market/outcome unless an explicit scale-in policy exists.",
            }
        )

    for resolution in data["resolutions"]:
        if not _is_split_resolution(resolution):
            continue
        related_positions = [position for position in data["positions"] if str(position["market_id"]) == str(resolution["market_id"])]
        incidents.append(
            {
                "issue_type": "split_resolution",
                "severity": "high" if related_positions else "medium",
                "first_seen": _resolution_effective_at(resolution),
                "last_seen": _resolution_effective_at(resolution),
                "affected_rows": len(related_positions),
                "evidence": json.dumps({
                    "market_id": resolution.get("market_id"),
                    "resolved_outcome": resolution.get("resolved_outcome"),
                    "payout_map": resolution.get("payout_map"),
                }, ensure_ascii=False, sort_keys=False),
                "hypothesis": "Binary-only resolution handling can misprice 50-50 or split-settlement markets.",
                "recommendation": "Preserve payout vectors and mark positions from payout maps instead of winner/loser booleans only.",
            }
        )

    failed_executions = [execution for execution in data["executions"] if execution.get("status") == "failed"]
    failure_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for execution in failed_executions:
        failure_groups[str(execution.get("failure_category") or "other_failure")].append(execution)
    for category, items in sorted(failure_groups.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        incidents.append(
            {
                "issue_type": "execution_failure",
                "severity": "high" if len(items) >= 3 else "medium",
                "first_seen": min(item.get("created_at") or "" for item in items),
                "last_seen": max(item.get("created_at") or "" for item in items),
                "affected_rows": len(items),
                "evidence": json.dumps({
                    "category": category,
                    "sample_errors": sorted({str(item.get("error_message") or "") for item in items})[:3],
                }, ensure_ascii=False, sort_keys=False),
                "hypothesis": "Execution failures cluster around a small number of external dependency and readiness failure modes.",
                "recommendation": "Add per-category retry/backoff, readiness gating, and category-specific alerting for execution failures.",
            }
        )

    locked_heartbeats = [heartbeat for heartbeat in data["heartbeats"] if "database is locked" in str(heartbeat.get("error_message") or "").lower()]
    if locked_heartbeats:
        incidents.append(
            {
                "issue_type": "ops_lock",
                "severity": "high",
                "first_seen": min(item.get("started_at") or "" for item in locked_heartbeats),
                "last_seen": max(item.get("started_at") or "" for item in locked_heartbeats),
                "affected_rows": len(locked_heartbeats),
                "evidence": json.dumps({
                    "loops": sorted({item.get("loop_name") for item in locked_heartbeats}),
                    "count": len(locked_heartbeats),
                }, ensure_ascii=False, sort_keys=False),
                "hypothesis": "Concurrent writers and long transactions intermittently blocked SQLite loop progress.",
                "recommendation": "Keep using a snapshot-first review flow and further shorten online write transactions where possible.",
            }
        )

    stale_positions = [
        position for position in data["positions"]
        if position.get("status") not in {"resolved", "cancelled"}
        and (
            (position.get("resolution") is not None)
            or any(
                (execution.get("status") or "").lower().startswith("canceled") or (execution.get("status") or "").lower().startswith("cancelled")
                for execution in (position.get("proposal") or {}).get("executions", [])
            )
        )
    ]
    if stale_positions:
        incidents.append(
            {
                "issue_type": "stale_state",
                "severity": "high",
                "first_seen": min(item.get("entry_time") or "" for item in stale_positions),
                "last_seen": max(item.get("updated_at") or "" for item in stale_positions),
                "affected_rows": len(stale_positions),
                "evidence": json.dumps({
                    "position_ids": [item.get("id") for item in stale_positions[:10]],
                }, ensure_ascii=False, sort_keys=False),
                "hypothesis": "Position state can lag execution or resolution state when reconciliation is incomplete.",
                "recommendation": "Run explicit stale-state reconciliation and assert terminal execution states cannot leave non-terminal positions.",
            }
        )

    duplicate_execution_groups: list[dict[str, Any]] = []
    executions_by_proposal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for execution in data["executions"]:
        executions_by_proposal[str(execution["proposal_id"])].append(execution)
    for proposal_id, items in executions_by_proposal.items():
        if len(items) <= 1:
            continue
        duplicate_execution_groups.append({"proposal_id": proposal_id, "count": len(items), "statuses": sorted({item.get("status") for item in items})})
    for group in duplicate_execution_groups:
        incidents.append(
            {
                "issue_type": "reconciliation_gap",
                "severity": "high",
                "first_seen": min(item.get("created_at") or "" for item in executions_by_proposal[group["proposal_id"]]),
                "last_seen": max(item.get("updated_at") or "" for item in executions_by_proposal[group["proposal_id"]]),
                "affected_rows": group["count"],
                "evidence": json.dumps(group, ensure_ascii=False, sort_keys=False),
                "hypothesis": "Proposal-level execution idempotency was not strict enough, leaving multiple execution records for one proposal.",
                "recommendation": "Enforce one active execution per proposal and add reconciliation checks for duplicate execution lineage.",
            }
        )

    total_losses = sum(abs(_to_float(item.get("gross_losses")) or 0.0) for item in market_category_stats)
    if total_losses > 0:
        for category in market_category_stats:
            category_losses = abs(_to_float(category.get("gross_losses")) or 0.0)
            share = category_losses / total_losses if total_losses else 0.0
            if share < 0.35:
                continue
            incidents.append(
                {
                    "issue_type": "market_type_concentration",
                    "severity": "high" if share >= 0.5 else "medium",
                    "first_seen": "",
                    "last_seen": "",
                    "affected_rows": int(category.get("resolved_positions") or 0),
                    "evidence": json.dumps({
                        "market_category": category.get("market_category"),
                        "gross_losses": category.get("gross_losses"),
                        "loss_share": round(share, 4),
                    }, ensure_ascii=False, sort_keys=False),
                    "hypothesis": "Losses are concentrated in a narrow market class, which can disguise strategy-level fragility as broad coverage.",
                    "recommendation": "Segment strategy rules and limits by market class, especially for short-horizon directional products.",
                }
            )

    incidents.sort(key=lambda item: (_severity_rank(str(item.get("severity") or "low")), -(int(item.get("affected_rows") or 0)), str(item.get("issue_type") or "")))
    return incidents


def _build_known_unknowns(data: Mapping[str, Any], metrics: Mapping[str, Any]) -> list[dict[str, Any]]:
    risk_blocked = int(metrics["funnel_counts"].get("risk_blocked", 0))
    approvals = len(data["approvals"])
    shadow_count = len(data["shadow_executions"])
    return [
        {
            "title": "Risk-block reasons are not persisted per proposal.",
            "detail": f"{risk_blocked} risk_blocked proposals exist, but the blocking reason is not stored in the proposals table.",
        },
        {
            "title": "Proposal-time microstructure is only partially preserved.",
            "detail": "Market snapshots and execution intents exist, but there is no durable per-proposal order book history to fully replay the trade decision context.",
        },
        {
            "title": "Execution failure attribution is incomplete.",
            "detail": "Failed executions keep error strings, but network, service readiness, balance, and market-data causes are not normalized into structured telemetry.",
        },
        {
            "title": "LLM contribution cannot be isolated from current history alone.",
            "detail": "The observed range is dominated by openclaw_llm decisions without a durable counterfactual control group or experiment tag.",
        },
        {
            "title": "Shadow/live comparison is too thin for confident causal analysis.",
            "detail": f"Only {shadow_count} shadow executions are present, which is not enough to treat shadow-vs-live divergence as statistically reliable.",
        },
        {
            "title": "Manual approval path has limited sample size.",
            "detail": f"Only {approvals} approvals were recorded, so operator-gated behavior cannot be robustly compared with auto-authorized behavior.",
        },
    ]


def _build_hidden_risk_hypotheses(data: Mapping[str, Any], incidents: Sequence[Mapping[str, Any]], market_category_stats: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    duplicate_count = sum(1 for item in incidents if item.get("issue_type") == "duplicate_exposure")
    split_count = sum(1 for item in incidents if item.get("issue_type") == "split_resolution")
    lock_count = sum(1 for item in incidents if item.get("issue_type") == "ops_lock")
    failure_count = sum(1 for item in incidents if item.get("issue_type") == "execution_failure")
    concentrated = [item for item in incidents if item.get("issue_type") == "market_type_concentration"]
    top_category = market_category_stats[0]["market_category"] if market_category_stats else "other"
    return [
        {
            "hypothesis": "Incomplete resolution semantics can still distort historical PnL if future non-binary settlement types appear.",
            "evidence_grade": "high" if split_count else "medium",
            "evidence": f"Split-resolution incidents detected: {split_count}.",
        },
        {
            "hypothesis": "Repeated entry on the same thesis can create a false sense of alpha while actually concentrating tail risk.",
            "evidence_grade": "high" if duplicate_count else "medium",
            "evidence": f"Duplicate exposure incidents detected: {duplicate_count}.",
        },
        {
            "hypothesis": "Execution and position state can diverge under cancellation or reconciliation edge cases.",
            "evidence_grade": "high" if any(item.get("issue_type") == "reconciliation_gap" for item in incidents) else "medium",
            "evidence": "Duplicate execution lineage and canceled-market edge cases are present in history.",
        },
        {
            "hypothesis": "A small number of market classes may dominate both exposure and post-mortem learning.",
            "evidence_grade": "high" if concentrated else "medium",
            "evidence": f"Most exposed market category in history: {top_category}.",
        },
        {
            "hypothesis": "External dependency fragility remains a hidden limiter on autonomous execution quality.",
            "evidence_grade": "high" if failure_count or lock_count else "medium",
            "evidence": f"Execution failure incident groups: {failure_count}; DB lock incidents: {lock_count}.",
        },
    ]


def _build_metrics(data: Mapping[str, Any]) -> dict[str, Any]:
    proposals = data["proposals"]
    executions = data["executions"]
    positions = data["positions"]
    resolved_positions = [position for position in positions if position.get("status") == "resolved"]
    realized_values = [_to_float(position.get("realized_pnl")) or 0.0 for position in resolved_positions]
    gross_wins = sum(value for value in realized_values if value > 0)
    gross_losses = sum(value for value in realized_values if value < 0)
    total_realized = sum(realized_values)
    market_outcome_pnl = _build_market_outcome_pnl(data)
    daily_stats = _build_daily_stats(data)
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for item in sorted(daily_stats, key=lambda row: row["day"]):
        cumulative += _to_float(item.get("realized_pnl")) or 0.0
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    funnel_counts = Counter(str(proposal.get("status") or "unknown") for proposal in proposals)
    funnel_counts["executed_with_execution_record"] = sum(1 for proposal in proposals if proposal.get("executions"))
    funnel_counts["resolved_positions"] = len(resolved_positions)
    strategy_stats = _build_strategy_stats(data)
    market_category_stats = _build_market_category_stats(data)
    confidence_buckets = _build_bucket_stats(data, bucket_key="confidence")
    size_buckets = _build_bucket_stats(data, bucket_key="size")
    incidents = _build_incidents(data, market_outcome_pnl, market_category_stats)
    known_unknowns = _build_known_unknowns(data, {"funnel_counts": funnel_counts})
    hidden_risks = _build_hidden_risk_hypotheses(data, incidents, market_category_stats)
    min_time_candidates = [value for value in (proposal.get("created_at") for proposal in proposals) if value]
    max_time_candidates = [value for value in (position.get("resolution_effective_at") or position.get("updated_at") for position in resolved_positions) if value]
    if not max_time_candidates:
        max_time_candidates = [value for value in (proposal.get("updated_at") for proposal in proposals) if value]
    return {
        "generated_at": utc_now_iso(),
        "time_range": {
            "start": min(min_time_candidates) if min_time_candidates else None,
            "end": max(max_time_candidates) if max_time_candidates else None,
        },
        "row_counts": {
            "proposals": len(proposals),
            "executions": len(executions),
            "positions": len(positions),
            "approvals": len(data["approvals"]),
            "autopilot_heartbeats": len(data["heartbeats"]),
            "order_reconciliations": len(data["reconciliations"]),
            "market_resolutions": len(data["resolutions"]),
            "agent_reviews": len(data["agent_reviews"]),
        },
        "headline": {
            "total_realized_pnl": _round_money(total_realized),
            "gross_wins": _round_money(gross_wins),
            "gross_losses": _round_money(gross_losses),
            "resolved_positions": len(resolved_positions),
            "winning_positions": sum(1 for value in realized_values if value > 0),
            "losing_positions": sum(1 for value in realized_values if value < 0),
            "approx_max_drawdown": _round_money(max_drawdown),
        },
        "funnel_counts": dict(funnel_counts),
        "daily_stats": daily_stats,
        "strategy_stats": strategy_stats,
        "market_category_stats": market_category_stats,
        "confidence_buckets": confidence_buckets,
        "size_buckets": size_buckets,
        "market_outcome_pnl": market_outcome_pnl,
        "incidents": incidents,
        "known_unknowns": known_unknowns,
        "hidden_risk_hypotheses": hidden_risks,
    }


def _top_issue_bullets(incidents: Sequence[Mapping[str, Any]], limit: int = 5) -> list[str]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in incidents:
        issue_type = str(item.get("issue_type") or "unknown")
        current = grouped.setdefault(
            issue_type,
            {
                "issue_type": issue_type,
                "severity": item.get("severity") or "low",
                "instances": 0,
                "max_affected_rows": 0,
                "hypothesis": item.get("hypothesis") or "",
            },
        )
        current["instances"] += 1
        current["max_affected_rows"] = max(current["max_affected_rows"], int(item.get("affected_rows") or 0))
        if _severity_rank(str(item.get("severity") or "low")) < _severity_rank(str(current["severity"] or "low")):
            current["severity"] = item.get("severity") or current["severity"]
            current["hypothesis"] = item.get("hypothesis") or current["hypothesis"]
    items = list(grouped.values())
    items.sort(
        key=lambda item: (
            _severity_rank(str(item.get("severity") or "low")),
            -int(item.get("instances") or 0),
            -int(item.get("max_affected_rows") or 0),
            str(item.get("issue_type") or ""),
        )
    )
    bullets = []
    for item in items[:limit]:
        bullets.append(
            f"- `{item['issue_type']}` / `{item['severity']}`: instances={item['instances']}, max_affected_rows={item['max_affected_rows']} | {item['hypothesis']}"
        )
    return bullets


def _markdown_table(rows: Sequence[Mapping[str, Any]], headers: Sequence[tuple[str, str]]) -> str:
    if not rows:
        return "_No data._"
    head = "| " + " | ".join(label for _, label in headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = []
    for row in rows:
        values = []
        for key, _label in headers:
            value = row.get(key)
            if isinstance(value, float):
                values.append(f"{value:.2f}")
            elif value is None:
                values.append("")
            else:
                values.append(str(value))
        body.append("| " + " | ".join(values) + " |")
    return "\n".join([head, sep, *body])


def _build_summary_markdown(metrics: Mapping[str, Any], output_dir: Path) -> str:
    headline = metrics["headline"]
    daily_rows = metrics["daily_stats"]
    strategy_rows = metrics["strategy_stats"]
    category_rows = metrics["market_category_stats"]
    loss_rows = [row for row in metrics["market_outcome_pnl"] if (_to_float(row.get("realized_pnl")) or 0.0) < 0][:8]
    win_rows = [row for row in reversed(metrics["market_outcome_pnl"]) if (_to_float(row.get("realized_pnl")) or 0.0) > 0][:8]
    funnel_rows = [{"stage": key, "count": value} for key, value in metrics["funnel_counts"].items()]
    funnel_rows.sort(key=lambda item: item["stage"])
    known_unknowns = metrics["known_unknowns"]
    hidden_risks = metrics["hidden_risk_hypotheses"]
    incidents = metrics["incidents"]

    lines = [
        "# Polymarket Trading 全量复盘",
        "",
        "## Executive Summary",
        "",
        f"- 复盘区间: `{metrics['time_range']['start']}` 到 `{metrics['time_range']['end']}`",
        f"- Proposal / Execution / Position 总数: `{metrics['row_counts']['proposals']}` / `{metrics['row_counts']['executions']}` / `{metrics['row_counts']['positions']}`",
        f"- 已实现 PnL: `{_format_money(headline['total_realized_pnl'])}`",
        f"- Gross wins / gross losses: `{_format_money(headline['gross_wins'])}` / `{_format_money(headline['gross_losses'])}`",
        f"- 近似最大回撤: `{_format_money(headline['approx_max_drawdown'])}`",
        f"- 已结算胜 / 负笔数: `{headline['winning_positions']}` / `{headline['losing_positions']}`",
        "",
        "最关键的系统问题:",
        *_top_issue_bullets(incidents, limit=5),
        "",
        "图表:",
        "- [按天已实现 PnL](plots/daily_realized_pnl.svg)",
        "- [按策略已实现 PnL](plots/strategy_realized_pnl.svg)",
        "",
        "## Trading Performance Review",
        "",
        "### 按天拆分",
        _markdown_table(
            daily_rows,
            (
                ("day", "Day"),
                ("proposal_count", "Proposals"),
                ("execution_count", "Executions"),
                ("resolved_positions", "Resolved"),
                ("winning_positions", "Wins"),
                ("losing_positions", "Losses"),
                ("gross_exposure_usdc", "Gross Exposure"),
                ("realized_pnl", "Realized PnL"),
            ),
        ),
        "",
        "### 按策略拆分",
        _markdown_table(
            strategy_rows,
            (
                ("strategy_name", "Strategy"),
                ("proposal_count", "Proposals"),
                ("executed_proposals", "Executed"),
                ("resolved_positions", "Resolved"),
                ("win_rate", "Win Rate"),
                ("realized_pnl", "Realized PnL"),
            ),
        ),
        "",
        "### 按市场类型拆分",
        _markdown_table(
            category_rows,
            (
                ("market_category", "Category"),
                ("proposal_count", "Proposals"),
                ("execution_count", "Executions"),
                ("resolved_positions", "Resolved"),
                ("gross_exposure_usdc", "Gross Exposure"),
                ("realized_pnl", "Realized PnL"),
                ("gross_losses", "Gross Losses"),
            ),
        ),
        "",
        "### 最大亏损组",
        _markdown_table(
            loss_rows,
            (
                ("market_id", "Market ID"),
                ("question", "Question"),
                ("outcome", "Outcome"),
                ("positions", "Positions"),
                ("duplicate_entries", "Dup Entries"),
                ("realized_pnl", "Realized PnL"),
            ),
        ),
        "",
        "### 最大盈利组",
        _markdown_table(
            win_rows,
            (
                ("market_id", "Market ID"),
                ("question", "Question"),
                ("outcome", "Outcome"),
                ("positions", "Positions"),
                ("realized_pnl", "Realized PnL"),
            ),
        ),
        "",
        "### Confidence Bucket",
        _markdown_table(
            metrics["confidence_buckets"],
            (
                ("bucket", "Bucket"),
                ("resolved_positions", "Resolved"),
                ("winning_positions", "Wins"),
                ("losing_positions", "Losses"),
                ("win_rate", "Win Rate"),
                ("realized_pnl", "Realized PnL"),
            ),
        ),
        "",
        "### Size Bucket",
        _markdown_table(
            metrics["size_buckets"],
            (
                ("bucket", "Bucket"),
                ("resolved_positions", "Resolved"),
                ("winning_positions", "Wins"),
                ("losing_positions", "Losses"),
                ("win_rate", "Win Rate"),
                ("realized_pnl", "Realized PnL"),
            ),
        ),
        "",
        "### Proposal Funnel",
        _markdown_table(
            funnel_rows,
            (
                ("stage", "Stage"),
                ("count", "Count"),
            ),
        ),
        "",
        "## Risk And Incident Review",
        "",
        _markdown_table(
            incidents,
            (
                ("issue_type", "Issue Type"),
                ("severity", "Severity"),
                ("first_seen", "First Seen"),
                ("last_seen", "Last Seen"),
                ("affected_rows", "Affected Rows"),
                ("recommendation", "Recommendation"),
            ),
        ),
        "",
        "## Known Unknowns",
        "",
    ]
    for item in known_unknowns:
        lines.extend([f"- **{item['title']}**", f"  {item['detail']}"])
    lines.extend(["", "## Hidden Risk Hypotheses", ""])
    for item in hidden_risks:
        lines.extend([
            f"- **{item['hypothesis']}**",
            f"  Evidence grade: `{item['evidence_grade']}` | {item['evidence']}",
        ])
    lines.extend(
        [
            "",
            "## Iteration Plan",
            "",
            "### P0",
            "- 继续在 `market_id + outcome` 维度封堵重复暴露，禁止无显式 scale-in 策略的重复开仓。",
            "- 保留 split resolution 的 payout vector，并把 position terminal state 的同步校验收紧。",
            "- 保持 snapshot-first 的复盘流程，避免分析过程受在线 DB lock 干扰。",
            "",
            "### P1",
            "- 按 market class 重切策略规则，特别是短周期 crypto directional 市场。",
            "- 用真实历史结果重估 confidence 到 size 的映射，而不是继续依赖静态启发式。",
            "- 补齐结构化 execution failure telemetry，支持事后归因和运营告警。",
            "",
            "### P2",
            "- 在 proposal 时刻落更多 order-book 和 execution telemetry，提升可复盘性。",
            "- 扩大 shadow/live 对照样本，并引入更明确的实验标签。",
            "- 在这次一次性复盘 CLI 之上，继续演进成可复用的周报自动化。",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_outputs(output_dir: Path, metrics: Mapping[str, Any], facts: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    facts_dir = output_dir / "facts"
    plots_dir = output_dir / "plots"
    facts_dir.mkdir(parents=True, exist_ok=True)
    plots_dir.mkdir(parents=True, exist_ok=True)

    _write_csv(
        facts_dir / "proposal_funnel.csv",
        facts["proposal_funnel"],
        (
            "proposal_id", "created_at", "updated_at", "market_id", "question", "market_category", "outcome",
            "strategy_name", "decision_engine", "proposal_kind", "confidence_score", "confidence_bucket",
            "recommended_size_usdc", "size_bucket", "status", "status_path_estimate", "risk_block_reason",
            "authorization_status", "supervisor_decision", "approval_requested_at", "approval_expires_at",
            "approval_decision", "approval_decided_at", "execution_count", "executed", "latest_execution_id",
            "latest_execution_status", "position_count", "final_position_status", "resolved_outcome", "is_split_resolution",
        ),
    )
    _write_csv(
        facts_dir / "execution_facts.csv",
        facts["execution_facts"],
        (
            "execution_id", "proposal_id", "created_at", "updated_at", "mode", "status", "failure_category",
            "error_message", "client_order_id", "order_id", "requested_price", "avg_fill_price",
            "requested_size_usdc", "filled_size_usdc", "market_id", "question", "market_category", "outcome",
            "strategy_name", "decision_engine", "confidence_score", "proposal_status", "position_count",
            "reconciliation_count", "resolved_outcome", "is_split_resolution",
        ),
    )
    _write_csv(
        facts_dir / "position_facts.csv",
        facts["position_facts"],
        (
            "position_id", "proposal_id", "execution_id", "market_id", "question", "market_category", "outcome",
            "status", "entry_time", "resolution_effective_at", "holding_minutes", "entry_price", "last_mark_price",
            "resolution_payout", "size_usdc", "filled_qty", "unrealized_pnl", "realized_pnl", "resolved_outcome",
            "is_split_resolution", "is_duplicate_entry", "duplicate_entry_group_size", "duplicate_entry_rank",
            "strategy_name", "decision_engine", "confidence_score", "confidence_bucket", "size_bucket",
        ),
    )
    _write_csv(
        facts_dir / "market_facts.csv",
        facts["market_facts"],
        (
            "market_id", "question", "slug", "market_category", "is_short_horizon_crypto", "active", "closed",
            "accepting_orders", "end_date", "liquidity_usdc", "volume_usdc", "volume_24h_usdc", "proposal_count",
            "execution_count", "position_count", "duplicate_outcome_groups", "resolved_outcome", "is_split_resolution",
            "resolution_effective_at", "total_realized_pnl", "market_category_label",
        ),
    )
    _write_csv(
        facts_dir / "ops_timeline.csv",
        facts["ops_timeline"],
        (
            "event_time", "event_kind", "source", "severity", "loop_name", "proposal_id", "execution_id",
            "market_id", "duration_seconds", "items_processed", "detail",
        ),
    )
    _write_csv(
        facts_dir / "incident_register.csv",
        facts["incidents"],
        (
            "issue_type", "severity", "first_seen", "last_seen", "affected_rows", "evidence", "hypothesis", "recommendation",
        ),
    )

    dump_json(metrics, output_dir / "metrics.json")
    summary = _build_summary_markdown(metrics, output_dir)
    (output_dir / "summary.md").write_text(summary, encoding="utf-8")
    _write_svg_bar_chart(plots_dir / "daily_realized_pnl.svg", "Daily Realized PnL", metrics["daily_stats"], label_key="day", value_key="realized_pnl")
    _write_svg_bar_chart(plots_dir / "strategy_realized_pnl.svg", "Strategy Realized PnL", metrics["strategy_stats"], label_key="strategy_name", value_key="realized_pnl")


def generate_trade_review(*, db_path: Path, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_db = output_dir / "review_snapshot.sqlite3"
    _copy_snapshot(db_path, snapshot_db)
    data = _load_snapshot(snapshot_db)
    facts = {
        "proposal_funnel": _build_proposal_funnel_rows(data),
        "execution_facts": _build_execution_rows(data),
        "position_facts": _build_position_rows(data),
        "market_facts": _build_market_rows(data),
        "ops_timeline": _build_ops_timeline_rows(data),
    }
    metrics = _build_metrics(data)
    facts["incidents"] = metrics["incidents"]
    _write_outputs(output_dir, metrics, facts)
    return {
        "generated_at": utc_now_iso(),
        "db_path": str(db_path),
        "snapshot_db": str(snapshot_db),
        "output_dir": str(output_dir),
        "row_counts": metrics["row_counts"],
        "headline": metrics["headline"],
        "files": {
            "summary": str(output_dir / "summary.md"),
            "metrics": str(output_dir / "metrics.json"),
            "facts_dir": str(output_dir / "facts"),
            "plots_dir": str(output_dir / "plots"),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate an offline Polymarket trading history review package.")
    parser.add_argument("--db", required=True, help="Path to the SQLite database.")
    parser.add_argument("--output-dir", required=True, help="Directory for review outputs.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = generate_trade_review(
        db_path=Path(args.db).expanduser().resolve(),
        output_dir=Path(args.output_dir).expanduser().resolve(),
    )
    print(dump_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
