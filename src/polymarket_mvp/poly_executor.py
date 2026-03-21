from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

from .common import dump_json, get_env_float, load_repo_env, market_reference_price, proposal_id_for, read_proposals, resolve_token_id, utc_now_iso
from .db import connect_db, init_db, latest_execution, proposal_record, record_execution

load_repo_env()
BALANCE_SANITY_REASON = "Balance sanity check failed"
SESSION_SPEND_EXCEEDED_REASON = "Session spend limit exceeded"
ACCOUNT_MODE_NAMES = {
    0: "eoa",
    1: "poly_proxy",
    2: "poly_gnosis_safe",
}


def _env_any(*names: str, required: bool = False, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    if required:
        raise RuntimeError(f"{' or '.join(names)} is required.")
    return default


def _require_signature_type() -> int:
    raw = _env_any("POLY_CLOB_SIGNATURE_TYPE", "SIGNATURE_TYPE", required=True)
    try:
        value = int(raw or "")
    except ValueError as exc:
        raise RuntimeError(f"SIGNATURE_TYPE must be 0, 1, or 2. Got: {raw!r}") from exc
    if value not in ACCOUNT_MODE_NAMES:
        raise RuntimeError(f"SIGNATURE_TYPE must be 0, 1, or 2. Got: {value}")
    return value


def _build_clob_client():
    if sys.version_info < (3, 10):
        raise RuntimeError(
            "Real execution requires Python 3.10+ because py-clob-client dependencies "
            "use syntax unsupported by Python 3.9."
        )
    try:
        from py_clob_client.client import ClobClient  # pyright: ignore[reportMissingImports]
        from py_clob_client.clob_types import ApiCreds  # pyright: ignore[reportMissingImports]
    except Exception as exc:
        raise RuntimeError(
            "py-clob-client is unavailable. Install optional deps with "
            "`pip install -e .[real-exec]` in a Python 3.10+ environment."
        ) from exc

    host = _env_any("POLY_CLOB_HOST", required=True)
    chain_id = int(_env_any("POLY_CLOB_CHAIN_ID", "CHAIN_ID", required=True))
    signature_type = _require_signature_type()
    funder = _env_any("POLY_CLOB_FUNDER", "FUNDER", required=True)
    api_key = _env_any("POLY_API_KEY", required=True)
    api_secret = _env_any("POLY_API_SECRET", required=True)
    api_passphrase = _env_any("POLY_API_PASSPHRASE", required=True)
    signer_key = _env_any("POLY_CLOB_SIGNER_KEY", "POLY_CLOB_PRIVATE_KEY")
    if not signer_key:
        raise RuntimeError(
            "POLY_CLOB_SIGNER_KEY is required for real execution signing. "
            "API credentials alone are not sufficient for py-clob-client order signing."
        )
    creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
    client = ClobClient(
        host,
        key=signer_key,
        creds=creds,
        chain_id=chain_id,
        signature_type=signature_type,
        funder=funder,
    )
    return client


def _api_key_count(api_keys: Any) -> int:
    if isinstance(api_keys, dict):
        values = api_keys.get("apiKeys")
        if isinstance(values, list):
            return len(values)
        return 1 if api_keys else 0
    if isinstance(api_keys, list):
        return len(api_keys)
    return 1 if api_keys else 0


def _client_identity_summary(client: Any, *, signature_type: int, funder: str) -> Dict[str, Any]:
    signer_address = None
    try:
        signer_address = client.get_address()
    except Exception:
        signer_address = None
    summary = {
        "account_mode": ACCOUNT_MODE_NAMES[signature_type],
        "signature_type": signature_type,
        "signer_address": signer_address,
        "funder_address": funder,
        "warnings": [],
    }
    if signature_type == 0 and signer_address and signer_address.lower() != funder.lower():
        raise RuntimeError(
            "EOA mode requires signer_address == funder_address. "
            f"Got signer={signer_address} funder={funder}"
        )
    if signature_type in {1, 2} and signer_address and signer_address.lower() == funder.lower():
        summary["warnings"].append(
            "proxy_mode_signer_equals_funder; verify that the funder address is the actual Polymarket proxy/safe address"
        )
    return summary


def _require_live_orderbook(client: Any, token_id: str) -> Dict[str, Any]:
    orderbook = client.get_order_book(token_id)
    bids = getattr(orderbook, "bids", None) or []
    asks = getattr(orderbook, "asks", None) or []
    if not bids and not asks:
        raise RuntimeError(f"No live orderbook levels returned for token_id={token_id}")
    return {
        "token_id": token_id,
        "bids": len(bids),
        "asks": len(asks),
    }


def _current_worst_price(record: Dict[str, Any], mode: str, *, client: Any | None = None) -> float:
    proposal = record["proposal_json"]
    if mode == "mock":
        value = market_reference_price(record["market"]["market_json"], proposal["outcome"])
        return float(value if value is not None else proposal["confidence_score"])
    if client is None:
        client = _build_clob_client()
    token_id = resolve_token_id(record["market"]["market_json"], proposal["outcome"])
    if not token_id:
        raise RuntimeError(f"Missing token_id for market {proposal['market_id']} outcome {proposal['outcome']}")
    _require_live_orderbook(client, token_id)
    raw_price = client.get_price(token_id, side="BUY")
    if isinstance(raw_price, dict):
        raw_price = raw_price.get("price") or raw_price.get("bestPrice")
    price = _coerce_float(raw_price)
    if price is None:
        raise RuntimeError(f"Unable to parse price response for token_id={token_id}: {raw_price}")
    return float(price)


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _normalize_order_status(value: Any) -> str:
    if value is None:
        return "submitted"
    raw = str(value).strip().upper()
    if raw in {"MATCHED", "FILLED", "COMPLETED"}:
        return "filled"
    if raw in {"LIVE", "OPEN", "PLACED", "SUBMITTED"}:
        return "live"
    if raw in {"CANCELED", "CANCELLED", "REJECTED", "FAILED", "EXPIRED"}:
        return "failed"
    return str(value).strip().lower() or "submitted"


def _reconcile_execution_with_order_snapshot(client: Any, execution: Dict[str, Any]) -> Dict[str, Any]:
    order_id = execution.get("txhash_or_order_id")
    if not order_id:
        return execution
    try:
        order_snapshot = client.get_order(order_id)
    except Exception:
        return execution
    normalized_status = _normalize_order_status((order_snapshot or {}).get("status"))
    execution["status"] = normalized_status
    request = execution.get("order_intent_json", {}).get("request", {})
    unit_price = _coerce_float((order_snapshot or {}).get("price"))
    matched_shares = _coerce_float((order_snapshot or {}).get("size_matched"))
    if normalized_status == "filled":
        if unit_price is not None and matched_shares is not None:
            execution["avg_fill_price"] = unit_price
            execution["filled_size_usdc"] = round(unit_price * matched_shares, 6)
        else:
            execution["avg_fill_price"] = execution.get("requested_price")
            execution["filled_size_usdc"] = execution.get("requested_size_usdc")
    elif normalized_status == "live":
        execution["avg_fill_price"] = None
        execution["filled_size_usdc"] = None
    elif normalized_status == "failed":
        execution["error_message"] = execution.get("error_message") or "order_not_live"
        execution["filled_size_usdc"] = 0.0
        execution["avg_fill_price"] = None
    order_intent = dict(execution.get("order_intent_json") or {})
    order_intent["order_status_snapshot"] = {
        "status": (order_snapshot or {}).get("status"),
        "size_matched": (order_snapshot or {}).get("size_matched"),
        "price": (order_snapshot or {}).get("price"),
    }
    if request:
        order_intent["request"] = request
    execution["order_intent_json"] = order_intent
    execution["updated_at"] = utc_now_iso()
    return execution


def _extract_balance_value(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    for key in ("available", "availableBalance", "balance", "amount", "buyingPower", "total"):
        parsed = _coerce_float(payload.get(key))
        if parsed is not None:
            return parsed
    for value in payload.values():
        if isinstance(value, dict):
            nested = _extract_balance_value(value)
            if nested is not None:
                return nested
    return None


def _real_preflight_check(client: Any, proposal: Dict[str, Any]) -> Dict[str, Any]:
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams  # pyright: ignore[reportMissingImports]

    signature_type = _require_signature_type()
    funder = _env_any("POLY_CLOB_FUNDER", "FUNDER", required=True) or ""
    api_keys = client.get_api_keys()
    if not api_keys:
        raise RuntimeError("No API keys visible for this client. Check API key/secret/passphrase permissions.")

    collateral = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=signature_type)
    )
    raw_collateral_balance = None
    if isinstance(collateral, dict):
        raw_collateral_balance = _coerce_float(collateral.get("balance"))
    if raw_collateral_balance is not None:
        available_balance = raw_collateral_balance / 1_000_000.0
    else:
        available_balance = _extract_balance_value(collateral)
    if available_balance is None:
        raise RuntimeError(f"Unable to parse collateral balance from response: {collateral}")
    session_max_balance = get_env_float("SESSION_MAX_BALANCE_USDC", 1000.0)
    if available_balance > session_max_balance:
        raise RuntimeError(
            f"{BALANCE_SANITY_REASON}: available_usdc={available_balance:.6f} exceeds "
            f"SESSION_MAX_BALANCE_USDC={session_max_balance:.6f}"
        )
    if proposal["recommended_size_usdc"] > available_balance:
        raise RuntimeError(
            f"Insufficient collateral balance for order size: needed={proposal['recommended_size_usdc']:.6f}, "
            f"available={available_balance:.6f}"
        )
    return {
        "api_keys_count": _api_key_count(api_keys),
        "collateral_balance_available": available_balance,
        "collateral_raw": collateral,
        "account_identity": _client_identity_summary(client, signature_type=signature_type, funder=funder),
    }


def _failed_execution(record: Dict[str, Any], mode: str, reason: str, *, preflight: Dict[str, Any] | None = None) -> Dict[str, Any]:
    proposal = record["proposal_json"]
    order_intent = {"proposal_id": record["proposal_id"], "reason": reason}
    if preflight is not None:
        order_intent["preflight"] = preflight
    return {
        "proposal_id": record["proposal_id"],
        "mode": mode,
        "client_order_id": None,
        "order_intent_json": order_intent,
        "requested_price": None,
        "requested_size_usdc": proposal["recommended_size_usdc"],
        "max_slippage_bps": proposal["max_slippage_bps"],
        "observed_worst_price": None,
        "slippage_check_status": "skipped",
        "status": "failed",
        "filled_size_usdc": 0.0,
        "avg_fill_price": None,
        "txhash_or_order_id": None,
        "slippage_bps": None,
        "error_message": reason,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }


def execute_record(conn, record: Dict[str, Any], mode: str, *, session_state: Dict[str, float] | None = None) -> Dict[str, Any]:
    proposal = record["proposal_json"]
    session_state = session_state or {"cumulative_spend_usdc": 0.0}
    approval = record.get("approval") or {}
    is_approved = approval.get("decision") == "approved"
    if not is_approved:
        return _failed_execution(record, mode, "proposal_not_approved")

    prior = latest_execution(conn, record["proposal_id"], mode="real") if mode == "real" else None
    if mode == "real" and prior and prior.get("status") in {"filled", "submitted", "live"}:
        raise RuntimeError(f"Proposal {record['proposal_id']} already has a real execution record.")

    if mode == "mock":
        available_balance = get_env_float("POLYMARKET_AVAILABLE_BALANCE_U", 100.0)
        if proposal["recommended_size_usdc"] > available_balance:
            return _failed_execution(record, mode, "insufficient_balance")
    if mode == "real":
        session_max_spend = get_env_float("SESSION_MAX_SPEND_USDC", 50.0)
        cumulative_spend = float(session_state.get("cumulative_spend_usdc", 0.0))
        requested = float(proposal["recommended_size_usdc"])
        if cumulative_spend + requested > session_max_spend:
            return _failed_execution(record, mode, SESSION_SPEND_EXCEEDED_REASON)

    client = None
    preflight = None
    if mode == "real":
        try:
            client = _build_clob_client()
            preflight = _real_preflight_check(client, proposal)
            print(
                f"[poly-executor] real preflight proposal_id={record['proposal_id']} "
                f"{json.dumps({'account_identity': preflight['account_identity'], 'api_keys_count': preflight['api_keys_count'], 'collateral_balance_available': preflight['collateral_balance_available']}, sort_keys=True)}",
                file=sys.stderr,
            )
        except Exception as exc:
            return _failed_execution(record, mode, f"real_preflight_failed: {exc}")

    reference_price = float(proposal["confidence_score"])
    try:
        observed_worst_price = _current_worst_price(record, mode=mode, client=client)
    except Exception as exc:
        return _failed_execution(record, mode, f"price_fetch_failed: {exc}", preflight=preflight)
    max_allowed_price = min(1.0, reference_price * (1 + proposal["max_slippage_bps"] / 10000.0))
    slippage_bps = ((observed_worst_price - reference_price) / reference_price) * 10000 if reference_price else None
    if observed_worst_price > max_allowed_price:
        return {
            "proposal_id": record["proposal_id"],
            "mode": mode,
            "client_order_id": None,
            "order_intent_json": {
                "proposal_id": record["proposal_id"],
                "reference_price": reference_price,
                "max_allowed_price": max_allowed_price,
                "preflight": preflight,
            },
            "requested_price": max_allowed_price,
            "requested_size_usdc": proposal["recommended_size_usdc"],
            "max_slippage_bps": proposal["max_slippage_bps"],
            "observed_worst_price": observed_worst_price,
            "slippage_check_status": "failed",
            "status": "failed",
            "filled_size_usdc": 0.0,
            "avg_fill_price": None,
            "txhash_or_order_id": None,
            "slippage_bps": slippage_bps,
            "error_message": "slippage_exceeded",
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }

    requested_price = observed_worst_price
    requested_size_usdc = proposal["recommended_size_usdc"]
    share_size = requested_size_usdc / requested_price if requested_price else 0.0
    token_id = resolve_token_id(record["market"]["market_json"], proposal["outcome"])
    order_intent = {
        "proposal_id": record["proposal_id"],
        "market_id": proposal["market_id"],
        "token_id": token_id,
        "outcome": proposal["outcome"],
        "price": requested_price,
        "size_shares": share_size,
        "size_usdc": requested_size_usdc,
    }
    execution = {
        "proposal_id": record["proposal_id"],
        "mode": mode,
        "client_order_id": f"{record['proposal_id']}-{mode}",
        "order_intent_json": order_intent,
        "requested_price": requested_price,
        "requested_size_usdc": requested_size_usdc,
        "max_slippage_bps": proposal["max_slippage_bps"],
        "observed_worst_price": observed_worst_price,
        "slippage_check_status": "passed",
        "status": "filled" if mode == "mock" else "submitted",
        "filled_size_usdc": requested_size_usdc if mode == "mock" else None,
        "avg_fill_price": requested_price if mode == "mock" else None,
        "txhash_or_order_id": None,
        "slippage_bps": slippage_bps,
        "error_message": None,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    if mode == "real":
        if client is None:
            return _failed_execution(record, mode, "real_client_missing", preflight=preflight)
        real_client = client
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType  # pyright: ignore[reportMissingImports]
            from py_clob_client.order_builder.constants import BUY  # pyright: ignore[reportMissingImports]
        except Exception as exc:
            return _failed_execution(record, mode, f"order_sdk_unavailable: {exc}", preflight=preflight)
        try:
            order = OrderArgs(token_id=token_id, price=requested_price, size=share_size, side=BUY)
            signed = real_client.create_order(order)
            response = real_client.post_order(signed, OrderType.GTC)
            execution["txhash_or_order_id"] = str(response.get("orderID") or response.get("id") or "")
            execution["status"] = _normalize_order_status(response.get("status"))
            execution["order_intent_json"] = {"request": order_intent, "response": response, "preflight": preflight}
            execution["updated_at"] = utc_now_iso()
            execution = _reconcile_execution_with_order_snapshot(real_client, execution)
        except Exception as exc:
            return _failed_execution(record, mode, f"order_submit_failed: {exc}", preflight=preflight)
        session_state["cumulative_spend_usdc"] = float(session_state.get("cumulative_spend_usdc", 0.0)) + requested_size_usdc
    return execution


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute approved Polymarket proposals in mock or real mode.")
    parser.add_argument("--proposal-file", required=True, help="Proposal JSON file.")
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    proposals = read_proposals(args.proposal_file)
    proposal_ids = [proposal_id_for(item) for item in proposals]
    executions: List[Dict[str, Any]] = []
    session_state: Dict[str, float] = {"cumulative_spend_usdc": 0.0}
    fatal_abort = False
    with connect_db() as conn:
        for proposal_id in proposal_ids:
            record = proposal_record(conn, proposal_id)
            if record is None:
                raise RuntimeError(f"Proposal {proposal_id} not found in database.")
            try:
                execution = execute_record(conn, record, mode=args.mode, session_state=session_state)
            except Exception as exc:
                execution = _failed_execution(record, args.mode, f"executor_unhandled: {exc}")
            stored = record_execution(conn, execution)
            executions.append(stored)
            if args.mode == "real" and isinstance(stored.get("error_message"), str) and BALANCE_SANITY_REASON in stored["error_message"]:
                fatal_abort = True
                break
        conn.commit()
    summary = {
        "generated_at": utc_now_iso(),
        "mode": args.mode,
        "session_cumulative_spend_usdc": session_state["cumulative_spend_usdc"],
        "session_max_spend_usdc": get_env_float("SESSION_MAX_SPEND_USDC", 50.0),
        "aborted_on_balance_sanity_check": fatal_abort,
        "executions": executions,
        "filled_count": sum(1 for item in executions if item["status"] in {"filled", "submitted", "live"}),
        "failed_count": sum(1 for item in executions if item["status"] == "failed"),
    }
    print(dump_json(summary, path=args.output))
    return 2 if fatal_abort else 0


if __name__ == "__main__":
    raise SystemExit(main())
