from __future__ import annotations

from typing import Dict

from ..db import market_cluster_link
from ..services.memo_service import build_and_store_memo


def run_research_agent(conn, market_id: str) -> Dict:
    cluster = market_cluster_link(conn, market_id)
    return build_and_store_memo(conn, market_id, cluster=cluster)
