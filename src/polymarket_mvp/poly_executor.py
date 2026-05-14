from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from typing import Any, Dict, List

import requests

from .common import blocked_market_reason, clamp_order_live_ttl, compute_order_ttl, dump_json, get_env_float, get_env_int, load_repo_env, market_reference_price, parse_iso8601, proposal_id_for, read_proposals, resolve_token_id, utc_now_iso
from .db import connect_db, has_active_execution, has_active_market_outcome_exposure, init_db, latest_execution, list_proposals_by_status, proposal_record, record_execution, update_proposal_status
from .services.kill_switch_service import check_kill_switch
from .services.shadow_service import create_shadow_execution

load_repo_env()
BALANCE_SANITY_REASON = "Balance sanity check failed"
SESSION_SPEND_EXCEEDED_REASON = "Session spend limit exceeded"
ACCOUNT_MODE_NAMES = {
    0: "eoa",
    1: "poly_proxy",
    2: "poly_gnosis_safe",
}
KNOWN_V2_SPENDERS = [
    "0xE111180000d2663C0091e4f400237545B87B996B",  # CTFExchangeV2
    "0xe2222d279d744050d28e00520010520000310F59",  # NegRiskCtfExchangeV2
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",  # NegRiskAdapter (unchanged across V1→V2)
]


def _looks_retryable_request_error(exc: Exception) -> bool:
    # 4xx HTTP errors are permanent — don't retry
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        if status is not None and 400 <= status < 500:
            return False
        return True
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.RequestException):
        return True
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "request exception",
            "connection reset",
            "temporarily unavailable",
            "timed out",
            "timeout",
            "connection aborted",
            "remote end closed",
        )
    )


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
        from py_clob_client_v2 import ClobClient, ApiCreds  # pyright: ignore[reportMissingImports]
    except Exception as exc:
        raise RuntimeError(
            "py-clob-client-v2 is unavailable. Install optional deps with "
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
    # py-clob-client-v2 returns the raw HTTP response as a dict
    # ({'bids': [...], 'asks': [...], 'market': ..., 'tick_size': ..., ...});
    # the V1 SDK wrapped it in an OrderBookSummary dataclass with attributes.
    # Support both so we don't regress if either form is ever surfaced.
    if isinstance(orderbook, dict):
        bids = orderbook.get("bids") or []
        asks = orderbook.get("asks") or []
    else:
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
    # INVALID is what the CLOB returns when an order is aged out / rejected
    # by the matching engine after going LIVE — sometimes after a partial
    # fill (the canonical case behind position 1593's 20-day freeze).
    # Treat it as terminal-failure. Unknown statuses also map to "failed"
    # rather than leaking through as a literal lowercase string, which
    # used to silently corrupt the executions.status enum and freeze
    # downstream reconciliation forever.
    if raw in {"CANCELED", "CANCELLED", "REJECTED", "FAILED", "EXPIRED", "INVALID"}:
        return "failed"
    return "failed"


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


def _real_preflight_check(
    client: Any,
    proposal: Dict[str, Any],
    *,
    token_id: str | None = None,
    is_sell: bool = False,
) -> Dict[str, Any]:
    from py_clob_client_v2 import AssetType, BalanceAllowanceParams  # pyright: ignore[reportMissingImports]

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
    conditional_allowance_info = None
    if token_id:
        try:
            conditional = client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                    signature_type=signature_type,
                )
            )
            conditional_allowance = None
            if isinstance(conditional, dict):
                conditional_allowance = _coerce_float(conditional.get("allowance"))
            conditional_allowance_info = {"token_id": token_id, "raw": conditional, "allowance": conditional_allowance}
            # Only block on missing conditional allowance for SELLs (exit-kind).
            # For BUYs the wallet doesn't own the outcome token yet and the
            # allowance is irrelevant — what we need for BUYs is USDC allowance
            # to the exchange (checked above as COLLATERAL). Treating the BUY
            # case as a hard error misroutes every first-touch order to
            # order_submit_failed; see code-review #1 from the 2026-05-12
            # shadow→real preflight audit.
            if is_sell and conditional_allowance is not None and conditional_allowance == 0.0:
                from .common import polygon_rpc_url
                raise RuntimeError(
                    f"Conditional token allowance is zero for token_id={token_id}. "
                    f"SELL requires setApprovalForAll on the CTF token for the "
                    f"appropriate exchange. "
                    f"Known V2 spenders: {', '.join(KNOWN_V2_SPENDERS)}. "
                    f"Use RPC: {polygon_rpc_url()}"
                )
        except RuntimeError:
            raise
        except Exception:
            pass
    return {
        "api_keys_count": _api_key_count(api_keys),
        "collateral_balance_available": available_balance,
        "collateral_raw": collateral,
        "conditional_allowance": conditional_allowance_info,
        "account_identity": _client_identity_summary(client, signature_type=signature_type, funder=funder),
    }


def _classify_error(reason: str) -> str:
    """Classify error reason into a structured category."""
    r = reason.lower()
    if "service_not_ready" in r or "sdk_unavailable" in r or "client_missing" in r:
        return "service_not_ready"
    if "insufficient_balance" in r or "balance" in r:
        return "insufficient_balance"
    if "slippage" in r:
        return "slippage_exceeded"
    if "order_submit_failed" in r or "allowance" in r or "spender" in r:
        return "order_submit_failed"
    if "preflight_failed" in r or "price_fetch_failed" in r:
        return "request_exception"
    if "duplicate" in r or "already_has_active" in r:
        return "duplicate_blocked"
    if "kill_switch" in r or "not_authorized" in r or "expired" in r or "market_class" in r:
        return "policy_blocked"
    return "unknown"


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
        "error_category": _classify_error(reason),
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }


def _shadow_execute(conn, record: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """Shadow-mode path for ``execute_record``.

    Persists a ``shadow_executions`` row and returns a synthetic execution
    record with ``status='shadow_simulated'``. Never contacts the CLOB.
    """
    proposal = record["proposal_json"]
    try:
        simulated_price = _current_worst_price(record, mode="mock")
    except Exception:
        simulated_price = float(proposal.get("max_entry_price") or 0.0) or 0.5
    try:
        create_shadow_execution(
            conn,
            record,
            simulated_fill_price=simulated_price,
            simulated_status="env_gated_shadow",
        )
    except Exception as exc:
        print(
            f"[poly-executor] shadow_execute persist failed proposal_id={record['proposal_id']} error={exc}",
            file=sys.stderr,
        )
    # Advance the proposal out of `authorized_for_execution` so the execute loop
    # does not re-fire every tick. `executed` is a terminal status.
    try:
        update_proposal_status(conn, record["proposal_id"], "executed")
    except Exception as exc:
        print(
            f"[poly-executor] shadow_execute status bump failed proposal_id={record['proposal_id']} error={exc}",
            file=sys.stderr,
        )
    now = utc_now_iso()
    return {
        "proposal_id": record["proposal_id"],
        "mode": mode,
        "client_order_id": f"shadow-{record['proposal_id']}",
        "order_intent_json": {
            "proposal_id": record["proposal_id"],
            "source": "shadow_gate",
            "simulated_fill_price": simulated_price,
        },
        "requested_price": simulated_price,
        "requested_size_usdc": proposal["recommended_size_usdc"],
        "max_slippage_bps": proposal["max_slippage_bps"],
        "observed_worst_price": simulated_price,
        "slippage_check_status": "skipped",
        "status": "shadow_simulated",
        "filled_size_usdc": 0.0,
        "avg_fill_price": None,
        "txhash_or_order_id": None,
        "slippage_bps": 0,
        "error_message": None,
        "error_category": None,
        "created_at": now,
        "updated_at": now,
    }


def execute_record(conn, record: Dict[str, Any], mode: str, *, session_state: Dict[str, float] | None = None) -> Dict[str, Any]:
    # Shadow-mode gate: when MVP_SHADOW_MODE=1, never hit the CLOB. Instead we
    # persist a shadow_executions row (so downstream reconcile/CLV paths still
    # have structured data) and return a synthetic execution record flagged as
    # `shadow_simulated`. MUST run before any client build / SDK import so that
    # missing wallet credentials in shadow deployments don't crash the loop.
    if os.getenv("MVP_SHADOW_MODE") == "1":
        return _shadow_execute(conn, record, mode)

    proposal = record["proposal_json"]
    session_state = session_state or {"cumulative_spend_usdc": 0.0}
    approval = record.get("approval") or {}
    is_approved = approval.get("decision") == "approved" or record.get("status") in {"approved", "authorized_for_execution"}
    if not is_approved:
        return _failed_execution(record, mode, "proposal_not_authorized")
    # Check approval expiry — a stale approval should not execute
    approval_expires_at = record.get("approval_expires_at")
    if approval_expires_at:
        try:
            if parse_iso8601(approval_expires_at) < parse_iso8601(utc_now_iso()):
                return _failed_execution(record, mode, "approval_expired")
        except Exception:
            pass
    blocked_reason = blocked_market_reason(record.get("market") or {})
    if blocked_reason:
        return _failed_execution(record, mode, blocked_reason)

    kill_check = check_kill_switch(conn, record)
    if kill_check["blocked"]:
        return _failed_execution(record, mode, f"blocked_by_kill_switch: {kill_check['reason']}")

    # Guard: proposal-level active execution uniqueness (any mode)
    if has_active_execution(conn, record["proposal_id"]):
        return _failed_execution(record, mode, "proposal_already_has_active_execution")

    # Guard: block duplicate same-direction live exposure on the same market_id + outcome
    if record.get("proposal_kind", "entry") != "exit":
        market_id = record.get("market_id") or proposal.get("market_id")
        outcome = proposal.get("outcome")
        if market_id and outcome and has_active_market_outcome_exposure(
            conn, market_id, outcome, exclude_proposal_id=record["proposal_id"]
        ):
            return _failed_execution(record, mode, "duplicate_market_outcome_exposure")

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
        import time as _time
        last_preflight_exc: Exception | None = None
        preflight_attempts = max(1, get_env_int("POLY_REAL_PREFLIGHT_ATTEMPTS", 4))
        preflight_backoff_seconds = max(1.0, get_env_float("POLY_REAL_PREFLIGHT_BACKOFF_SECONDS", 2.0))
        for attempt in range(preflight_attempts):
            try:
                client = _build_clob_client()
                token_id_for_preflight = resolve_token_id(record["market"]["market_json"], proposal["outcome"])
                is_sell = record.get("proposal_kind") == "exit"
                preflight = _real_preflight_check(
                    client, proposal, token_id=token_id_for_preflight, is_sell=is_sell
                )
                print(
                    f"[poly-executor] real preflight proposal_id={record['proposal_id']} "
                    f"{json.dumps({'account_identity': preflight['account_identity'], 'api_keys_count': preflight['api_keys_count'], 'collateral_balance_available': preflight['collateral_balance_available']}, sort_keys=True)}",
                    file=sys.stderr,
                )
                last_preflight_exc = None
                break
            except Exception as exc:
                last_preflight_exc = exc
                if attempt + 1 < preflight_attempts and _looks_retryable_request_error(exc):
                    _time.sleep(preflight_backoff_seconds * (2 ** attempt))
                    continue
                break
        if last_preflight_exc is not None:
            return _failed_execution(record, mode, f"real_preflight_failed: {last_preflight_exc}")

    observed_worst_price = None
    last_price_exc: Exception | None = None
    if mode == "real":
        import time as _time
        price_fetch_attempts = max(1, get_env_int("POLY_PRICE_FETCH_ATTEMPTS", 3))
        price_fetch_backoff_seconds = max(1.0, get_env_float("POLY_PRICE_FETCH_BACKOFF_SECONDS", 1.5))
    else:
        price_fetch_attempts = 1
        price_fetch_backoff_seconds = 0.0
    for attempt in range(price_fetch_attempts):
        try:
            observed_worst_price = _current_worst_price(record, mode=mode, client=client)
            last_price_exc = None
            break
        except Exception as exc:
            last_price_exc = exc
            if mode == "real" and attempt + 1 < price_fetch_attempts and _looks_retryable_request_error(exc):
                _time.sleep(price_fetch_backoff_seconds * (2 ** attempt))
                continue
            break
    if last_price_exc is not None or observed_worst_price is None:
        return _failed_execution(record, mode, f"price_fetch_failed: {last_price_exc}", preflight=preflight)

    # Use market snapshot price as slippage reference (detects price drift between scan and execution)
    # Falls back to observed_worst_price if snapshot unavailable (slippage check always passes)
    snapshot_price = market_reference_price(record["market"]["market_json"], proposal["outcome"]) if record.get("market") else None
    reference_price = float(snapshot_price) if snapshot_price is not None else float(observed_worst_price)
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

    # Apply a small fill premium so the limit order is more likely to match immediately
    fill_premium_bps = get_env_int("POLY_FILL_PREMIUM_BPS", 150)
    requested_price = min(1.0, round(observed_worst_price * (1.0 + fill_premium_bps / 10000.0), 4))
    requested_size_usdc = proposal["recommended_size_usdc"]
    share_size = requested_size_usdc / requested_price if requested_price else 0.0
    token_id = resolve_token_id(record["market"]["market_json"], proposal["outcome"])
    # Dynamic TTL aware of the market's actual end_date. Static 1h cap was
    # auto-cancelling every pre-match sports limit order before the match
    # even started; compute_order_ttl extends TTL up to (end_date - now - buffer)
    # for far-future matches, capped at POLY_ORDER_DYNAMIC_TTL_MAX_SECONDS.
    order_live_ttl = compute_order_ttl(
        record.get("market", {}).get("market_json") or {},
        agent_ttl=record.get("order_live_ttl_seconds"),
    )
    idempotency_key = f"{record['proposal_id']}-{mode}-{uuid.uuid4().hex[:8]}"
    order_intent = {
        "proposal_id": record["proposal_id"],
        "market_id": proposal["market_id"],
        "token_id": token_id,
        "outcome": proposal["outcome"],
        "price": requested_price,
        "size_shares": share_size,
        "size_usdc": requested_size_usdc,
        "order_live_ttl_seconds": order_live_ttl,
        "order_posted_at": utc_now_iso(),
    }
    execution = {
        "proposal_id": record["proposal_id"],
        "mode": mode,
        "client_order_id": idempotency_key,
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
        try:
            # Use a dedicated short-lived transaction so network submission does not
            # keep a SQLite write lock open after the shadow record is inserted.
            with connect_db() as shadow_conn:
                create_shadow_execution(
                    shadow_conn,
                    record,
                    simulated_fill_price=requested_price,
                    simulated_status="pretrade_shadow",
                )
                shadow_conn.commit()
        except Exception:
            pass
        if client is None:
            return _failed_execution(record, mode, "real_client_missing", preflight=preflight)
        real_client = client
        try:
            from py_clob_client_v2 import OrderArgs, OrderType  # pyright: ignore[reportMissingImports]
            from py_clob_client_v2.order_builder.constants import BUY, SELL  # pyright: ignore[reportMissingImports]
        except Exception as exc:
            return _failed_execution(record, mode, f"order_sdk_unavailable: {exc}", preflight=preflight)
        try:
            import concurrent.futures
            import time as _time
            side = SELL if record.get("proposal_kind") == "exit" else BUY
            order = OrderArgs(token_id=token_id, price=requested_price, size=share_size, side=side)
            # create_order internally fetches tick_size + negRisk metadata via
            # HTTP. A single 5xx or transient timeout there should not fail the
            # whole trade — it's a pre-network local-signing step, so retrying
            # is safe (no risk of duplicate orders). post_order itself stays
            # single-shot; the lost-response recovery path below handles its
            # transient failures via the idempotency_key.
            create_attempts = max(1, get_env_int("POLY_ORDER_CREATE_ATTEMPTS", 3))
            create_backoff_seconds = max(0.5, get_env_float("POLY_ORDER_CREATE_BACKOFF_SECONDS", 1.0))
            last_create_exc: Exception | None = None
            for attempt in range(create_attempts):
                try:
                    signed = real_client.create_order(order)
                    last_create_exc = None
                    break
                except Exception as exc:
                    last_create_exc = exc
                    if attempt + 1 < create_attempts and _looks_retryable_request_error(exc):
                        _time.sleep(create_backoff_seconds * (2 ** attempt))
                        continue
                    break
            if last_create_exc is not None:
                raise last_create_exc
            order_timeout = get_env_int("POLY_ORDER_SUBMIT_TIMEOUT_SECONDS", 30)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(real_client.post_order, signed, OrderType.GTC)
                response = future.result(timeout=order_timeout)
            # Polymarket sometimes returns HTTP 200 with {success:false,errorMsg:...}
            # for things like tick-size mismatch, allowance issues, or hidden
            # negRisk validation. Without this guard the call below maps a
            # missing/None status to "submitted" and we record a phantom live
            # order that never actually existed on the book.
            success_flag = response.get("success") if isinstance(response, dict) else None
            if success_flag is False:
                err_msg = (response.get("errorMsg") or response.get("error") or "unknown_clob_error")
                raise RuntimeError(f"clob_rejected: {err_msg}")
            execution["txhash_or_order_id"] = str(response.get("orderID") or response.get("id") or "")
            execution["status"] = _normalize_order_status(response.get("status"))
            execution["order_intent_json"] = {"request": order_intent, "response": response, "preflight": preflight}
            execution["updated_at"] = utc_now_iso()
            execution = _reconcile_execution_with_order_snapshot(real_client, execution)
        except Exception as exc:
            # Before declaring failure, check if the order actually went through
            # (handles lost responses, timeouts where the order was accepted)
            try:
                recovered_order = real_client.get_order(idempotency_key)
                if recovered_order:
                    recovered_status = _normalize_order_status(recovered_order.get("status"))
                    if recovered_status in {"submitted", "live", "filled"}:
                        execution["txhash_or_order_id"] = str(recovered_order.get("id") or "")
                        execution["status"] = recovered_status
                        execution["order_intent_json"] = {
                            "request": order_intent,
                            "response": recovered_order,
                            "preflight": preflight,
                            "recovered_after_exception": str(exc),
                        }
                        execution["updated_at"] = utc_now_iso()
                        session_state["cumulative_spend_usdc"] = float(session_state.get("cumulative_spend_usdc", 0.0)) + requested_size_usdc
                        return execution
            except Exception:
                pass  # truly unknown — record as failed, reconciler will catch it
            error_msg = str(exc)
            if "allowance" in error_msg.lower() or "spender" in error_msg.lower():
                from .common import polygon_rpc_url
                error_msg = (
                    f"order_submit_failed (likely missing spender approval): {exc}. "
                    f"Known V2 spenders: {', '.join(KNOWN_V2_SPENDERS)}. "
                    f"RPC: {polygon_rpc_url()}"
                )
            else:
                error_msg = f"order_submit_failed: {exc}"
            return _failed_execution(record, mode, error_msg, preflight=preflight)
        session_state["cumulative_spend_usdc"] = float(session_state.get("cumulative_spend_usdc", 0.0)) + requested_size_usdc
    return execution


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute approved Polymarket proposals in mock or real mode.")
    parser.add_argument("--proposal-file", help="Proposal JSON file.")
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
    parser.add_argument("--source", choices=["file", "authorized_queue"], default="file")
    parser.add_argument("--limit", type=int, default=20, help="Queue execution limit when --source=authorized_queue.")
    parser.add_argument("--output", help="Optional file path for JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    init_db()
    executions: List[Dict[str, Any]] = []
    session_state: Dict[str, float] = {"cumulative_spend_usdc": 0.0}
    fatal_abort = False
    with connect_db() as conn:
        records: List[Dict[str, Any]]
        if args.source == "authorized_queue":
            records = list_proposals_by_status(conn, ["authorized_for_execution"], limit=args.limit)
        else:
            if not args.proposal_file:
                raise RuntimeError("--proposal-file is required when --source=file")
            proposals = read_proposals(args.proposal_file)
            proposal_ids = [proposal_id_for(item) for item in proposals]
            records = []
            for proposal_id in proposal_ids:
                record = proposal_record(conn, proposal_id)
                if record is None:
                    raise RuntimeError(f"Proposal {proposal_id} not found in database.")
                records.append(record)
        for record in records:
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
        "source": args.source,
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
