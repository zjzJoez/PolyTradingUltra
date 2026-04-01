from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Union

PROPOSAL_KEYS_V2 = (
    "market_id",
    "outcome",
    "confidence_score",
    "recommended_size_usdc",
    "reasoning",
    "max_slippage_bps",
)
LEGACY_PROPOSAL_KEYS = ("market_id", "outcome", "confidence", "size_u", "reason")
VALID_PROPOSAL_STATUSES = {
    "proposed",
    "risk_blocked",
    "pending_approval",
    "approved",
    "rejected",
    "authorized_for_execution",
    "executed",
    "failed",
    "expired",
    "cancelled",
}
VALID_AUTHORIZATION_STATUSES = {
    "none",
    "matched_manual_only",
    "matched_auto_execute",
}
_ENV_LOADED = False

PathLike = Union[str, Path]


def load_repo_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return

    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        _ENV_LOADED = True
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)

    _ENV_LOADED = True


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso8601(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def _to_path(path: PathLike) -> Path:
    return path if isinstance(path, Path) else Path(path)


def load_json(path: PathLike) -> Any:
    resolved = _to_path(path)
    return json.loads(resolved.read_text(encoding="utf-8"))


def dump_json(data: Any, path: PathLike | None = None, pretty: bool = True) -> str:
    indent = 2 if pretty else None
    text = json.dumps(data, indent=indent, sort_keys=False)
    if path is not None:
        resolved = _to_path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(text + "\n", encoding="utf-8")
    return text


def get_state_dir() -> Path:
    load_repo_env()
    configured = os.getenv("POLYMARKET_MVP_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "var"


def get_db_path() -> Path:
    load_repo_env()
    configured = os.getenv("POLYMARKET_MVP_DB_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return get_state_dir() / "polymarket_mvp.sqlite3"


def schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "schema.sql"


def append_jsonl(path: Path, event: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=False) + "\n")


def debug_events_path(name: str) -> Path:
    return get_state_dir() / "events" / f"{name}.jsonl"


def get_env_float(name: str, default: float) -> float:
    load_repo_env()
    raw = os.getenv(name)
    if raw is None or raw == "":
        return float(default)
    return float(raw)


def get_env_int(name: str, default: int) -> int:
    load_repo_env()
    raw = os.getenv(name)
    if raw is None or raw == "":
        return int(default)
    return int(raw)


def get_env_bool(name: str, default: bool) -> bool:
    load_repo_env()
    raw = os.getenv(name)
    if raw is None or raw == "":
        return bool(default)
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean-like value, got: {raw!r}")


def require_env(name: str) -> str:
    load_repo_env()
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required.")
    return value


def json_dumps_compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json_dumps_compact(value).encode("utf-8")).hexdigest()


def normalize_proposal(proposal: Mapping[str, Any], *, default_max_slippage_bps: int = 500) -> Dict[str, Any]:
    keys = set(proposal.keys())
    if keys == set(LEGACY_PROPOSAL_KEYS):
        proposal = {
            "market_id": proposal["market_id"],
            "outcome": proposal["outcome"],
            "confidence_score": proposal["confidence"],
            "recommended_size_usdc": proposal["size_u"],
            "reasoning": proposal["reason"],
            "max_slippage_bps": default_max_slippage_bps,
        }
    elif keys == set(PROPOSAL_KEYS_V2) - {"max_slippage_bps"}:
        proposal = dict(proposal)
        proposal["max_slippage_bps"] = default_max_slippage_bps
    elif keys != set(PROPOSAL_KEYS_V2):
        raise ValueError(
            "proposal must contain these keys: " + ", ".join(PROPOSAL_KEYS_V2)
        )

    normalized = {
        "market_id": str(proposal["market_id"]),
        "outcome": str(proposal["outcome"]).strip(),
        "confidence_score": float(proposal["confidence_score"]),
        "recommended_size_usdc": round(float(proposal["recommended_size_usdc"]), 6),
        "reasoning": str(proposal["reasoning"]),
        "max_slippage_bps": int(proposal["max_slippage_bps"]),
    }
    if not normalized["outcome"]:
        raise ValueError("proposal outcome must be a non-empty string")
    if not 0 <= normalized["confidence_score"] <= 1:
        raise ValueError("confidence_score must be between 0 and 1")
    if normalized["recommended_size_usdc"] <= 0:
        raise ValueError("recommended_size_usdc must be positive")
    if normalized["max_slippage_bps"] <= 0:
        raise ValueError("max_slippage_bps must be positive")
    return normalized


def ensure_proposal_list(payload: Any, *, default_max_slippage_bps: int = 500) -> List[Dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("proposals"), list):
        raw_items = payload["proposals"]
        proposals = [item.get("proposal", item) if isinstance(item, dict) else item for item in raw_items]
    else:
        proposals = payload if isinstance(payload, list) else [payload]
    return [normalize_proposal(item, default_max_slippage_bps=default_max_slippage_bps) for item in proposals]


def proposal_id_for(proposal: Mapping[str, Any], *, default_max_slippage_bps: int = 500) -> str:
    normalized = normalize_proposal(proposal, default_max_slippage_bps=default_max_slippage_bps)
    encoded = json_dumps_compact(normalized).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def read_proposals(path: PathLike, *, default_max_slippage_bps: int = 500) -> List[Dict[str, Any]]:
    return ensure_proposal_list(load_json(path), default_max_slippage_bps=default_max_slippage_bps)


def row_to_dict(row: sqlite3.Row | None) -> Dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def rows_to_dicts(rows: Sequence[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows]


def chunks(values: Sequence[str], size: int = 100) -> Iterable[Sequence[str]]:
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def sanitize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def strip_urls(value: str) -> str:
    return sanitize_text(re.sub(r"https?://\S+|www\.\S+", "", value or ""))


def slugify_text(value: str, *, fallback: str = "item", max_length: int = 80) -> str:
    normalized = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    if not normalized:
        normalized = fallback
    return normalized[:max_length].strip("-") or fallback


def outcome_map(market: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for item in market.get("outcomes", []):
        name = str(item.get("name"))
        result[name] = {
            "name": name,
            "price": item.get("price"),
            "token_id": item.get("token_id"),
        }
    return result


def resolve_token_id(market: Mapping[str, Any], outcome: str) -> str | None:
    return outcome_map(market).get(outcome, {}).get("token_id")


def market_reference_price(market: Mapping[str, Any], outcome: str) -> float | None:
    value = outcome_map(market).get(outcome, {}).get("price")
    if value is None:
        return None
    return float(value)


def polygon_rpc_url() -> str:
    return os.getenv("POLYGON_RPC_URL") or "https://polygon-bor-rpc.publicnode.com"


def clamp_approval_ttl(agent_ttl: int | None, seconds_to_expiry: float | None) -> int:
    """Clamp agent-suggested approval TTL to system bounds."""
    max_ttl = get_env_int("POLY_APPROVAL_MAX_TTL_SECONDS", 300)
    buffer = get_env_int("POLY_APPROVAL_EXPIRY_BUFFER_SECONDS", 120)
    floor = 30
    candidates = [max_ttl]
    if seconds_to_expiry is not None and seconds_to_expiry > 0:
        candidates.append(max(floor, int(0.25 * seconds_to_expiry)))
        candidates.append(max(floor, int(seconds_to_expiry) - buffer))
    system_max = min(candidates)
    effective = min(agent_ttl or system_max, system_max)
    return max(floor, effective)


def clamp_order_live_ttl(agent_ttl: int | None) -> int:
    """Clamp agent-suggested order live TTL to system bounds."""
    max_ttl = get_env_int("POLY_ORDER_MAX_LIVE_TTL_SECONDS", 300)
    floor = 15
    effective = min(agent_ttl or max_ttl, max_ttl)
    return max(floor, effective)


KNOWN_SYMBOLS = {
    "BTC": ("BTC", "BITCOIN"),
    "ETH": ("ETH", "ETHEREUM"),
    "SOL": ("SOL", "SOLANA"),
    "DOGE": ("DOGE", "DOGECOIN"),
    "XRP": ("XRP", "RIPPLE"),
    "ADA": ("ADA", "CARDANO"),
    "TRUMP": ("TRUMP",),
}


def infer_market_symbol(market: Mapping[str, Any]) -> str | None:
    haystack = " ".join(
        [
            str(market.get("question") or ""),
            str(market.get("slug") or ""),
            str(market.get("condition_id") or ""),
        ]
    ).upper()
    for symbol, aliases in KNOWN_SYMBOLS.items():
        if any(alias in haystack for alias in aliases):
            return symbol
    return None


def market_topic(market: Mapping[str, Any]) -> str:
    symbol = infer_market_symbol(market)
    if symbol:
        return symbol
    question = sanitize_text(str(market.get("question") or ""))
    return question[:180] if question else str(market.get("market_id"))


def short_context_line(prefix: str, text: str, limit: int) -> str:
    clean = sanitize_text(text)
    available = max(limit - len(prefix), 0)
    if len(clean) > available:
        clean = clean[: max(available - 3, 0)].rstrip() + "..."
    return prefix + clean
