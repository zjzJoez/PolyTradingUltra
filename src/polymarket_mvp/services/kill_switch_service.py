from __future__ import annotations

from typing import Any, Dict, List, Mapping

from ..db import list_kill_switches


def active_blockers(conn, record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    active = list_kill_switches(conn, active_only=True)
    blockers: List[Dict[str, Any]] = []
    market_id = str((record.get("market") or {}).get("market_id") or record.get("market_id") or "")
    strategy_name = str(record.get("strategy_name") or "")
    event_cluster_id = record.get("event_cluster_id")
    for item in active:
        scope_type = item["scope_type"]
        scope_key = str(item["scope_key"])
        if scope_type == "global":
            blockers.append(item)
        elif scope_type == "market" and market_id and scope_key == market_id:
            blockers.append(item)
        elif scope_type == "strategy" and strategy_name and scope_key == strategy_name:
            blockers.append(item)
        elif scope_type == "event_cluster" and event_cluster_id is not None and scope_key == str(event_cluster_id):
            blockers.append(item)
    return blockers


def check_kill_switch(conn, record: Mapping[str, Any]) -> Dict[str, Any]:
    blockers = active_blockers(conn, record)
    return {
        "blocked": bool(blockers),
        "blockers": blockers,
        "reason": blockers[0]["reason"] if blockers else None,
    }
