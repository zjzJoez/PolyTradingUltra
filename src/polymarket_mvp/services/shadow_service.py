from __future__ import annotations

from typing import Any, Dict, Mapping

from ..common import utc_now_iso
from ..db import record_shadow_execution


def create_shadow_execution(conn, record: Mapping[str, Any], *, simulated_fill_price: float, simulated_status: str = "simulated") -> Dict[str, Any]:
    proposal = record["proposal_json"]
    simulated_size = float(proposal["recommended_size_usdc"]) / simulated_fill_price if simulated_fill_price else 0.0
    return record_shadow_execution(
        conn,
        {
            "proposal_id": record["proposal_id"],
            "simulated_fill_price": simulated_fill_price,
            "simulated_size": simulated_size,
            "simulated_notional": proposal["recommended_size_usdc"],
            "simulated_status": simulated_status,
            "context_json": {
                "reference_price": simulated_fill_price,
                "market_id": proposal["market_id"],
                "outcome": proposal["outcome"],
            },
            "created_at": utc_now_iso(),
        },
    )
