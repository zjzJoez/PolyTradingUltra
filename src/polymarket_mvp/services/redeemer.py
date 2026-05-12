"""Auto-redeem winning conditional tokens after market resolution.

When a Polymarket market resolves, winning shares (ERC-1155 conditional tokens)
must be redeemed on-chain to convert back to USDC.

Standard markets: redeemed via CTF.redeemPositions(collateral, 0x00, conditionId, indexSets)
NegRisk markets:  redeemed via NegRiskAdapter.redeemPositions(conditionId, [yesAmt, noAmt])
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from typing import Any, Callable, Dict, List, Optional, TypeVar

from web3 import Web3

from ..common import polygon_rpc_url, utc_now_iso

T = TypeVar("T")


def _is_rate_limited(exc: BaseException) -> bool:
    """Heuristic 429 detection — web3.py surfaces RPC errors as raw HTTPError
    or wraps them in generic exceptions, so we match on substring as well.
    """
    msg = str(exc).lower()
    return "429" in msg or "too many requests" in msg or "rate limit" in msg


def _retry_429(fn: Callable[[], T], *, attempts: int = 3, base_sleep: float = 1.0) -> T:
    """Call fn(), retry on 429/timeout with exponential backoff (1s, 2s, 4s).
    Any non-rate-limit exception is re-raised immediately.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if not _is_rate_limited(exc) or attempt == attempts - 1:
                raise
            time.sleep(base_sleep * (2 ** attempt))
    raise RuntimeError("unreachable")  # pragma: no cover


# Contract addresses on Polygon
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ZERO_BYTES32 = b"\x00" * 32

NEG_RISK_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_conditionId", "type": "bytes32"},
            {"name": "_amounts", "type": "uint256[]"},
        ],
        "outputs": [],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "getDetermined",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
]

CTF_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "payoutDenominator",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "getCollectionId",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSet", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "name": "getPositionId",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "collectionId", "type": "bytes32"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


def _signer_key() -> str | None:
    return (os.getenv("POLY_CLOB_SIGNER_KEY") or "").strip() or None


def _build_w3() -> Web3:
    return Web3(Web3.HTTPProvider(polygon_rpc_url()))


def _token_ids_for_market(conn: sqlite3.Connection, market_id: str) -> List[Dict[str, Any]]:
    row = conn.execute(
        "SELECT outcomes_json FROM market_snapshots WHERE market_id = ?", (market_id,)
    ).fetchone()
    if row is None:
        return []
    outcomes = json.loads(row["outcomes_json"]) if isinstance(row["outcomes_json"], str) else row["outcomes_json"]
    return [
        {"name": o["name"], "token_id": o["token_id"]}
        for o in outcomes
        if o.get("token_id")
    ]


def _market_info(conn: sqlite3.Connection, market_id: str) -> Dict[str, Any] | None:
    row = conn.execute(
        "SELECT condition_id, market_json FROM market_snapshots WHERE market_id = ?", (market_id,)
    ).fetchone()
    if row is None:
        return None
    mj = json.loads(row["market_json"]) if isinstance(row["market_json"], str) else row["market_json"]
    raw_neg = mj.get("negRisk") if isinstance(mj, dict) else None
    # negRisk absent from the snapshot is NOT the same as negRisk=false — older
    # markets simply don't have the field. Return None to signal "ask the chain".
    # Defensive parse: bool("false") is True, so a JSON string "false" would
    # wrong-branch us into the standard-CTF path and silently 0-redeem a
    # NegRisk position. Trust real JSON booleans; for anything else, do
    # case-insensitive truthy parsing.
    if raw_neg is None:
        neg_risk: Optional[bool] = None
    elif isinstance(raw_neg, bool):
        neg_risk = raw_neg
    else:
        neg_risk = str(raw_neg).strip().lower() in ("true", "1", "yes")
    return {
        "condition_id": row["condition_id"],
        "neg_risk": neg_risk,
    }


def _is_standard_ctf_token(ctf, wallet: str, condition_id: bytes, token_id: int, usdc_addr: str) -> bool:
    """Check if a token ID matches the standard CTF position structure (parentCollectionId=0)."""
    for idx in [1, 2]:
        coll = ctf.functions.getCollectionId(ZERO_BYTES32, condition_id, idx).call()
        pos = ctf.functions.getPositionId(
            Web3.to_checksum_address(usdc_addr), coll
        ).call()
        if pos == token_id:
            return True
    return False


def redeem_resolved_positions(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Find resolved markets where we hold redeemable tokens and redeem on-chain.

    Automatically detects standard vs NegRisk token structure and uses the
    appropriate redemption path.
    """
    signer_key = _signer_key()
    if not signer_key:
        return []

    w3 = _build_w3()
    if not w3.is_connected():
        return []

    account = w3.eth.account.from_key(signer_key)
    wallet = account.address

    ctf = w3.eth.contract(
        address=w3.to_checksum_address(CTF_ADDRESS),
        abi=CTF_ABI,
    )

    # Only iterate resolved markets where we actually held a real (non-shadow)
    # position that hasn't been redeemed yet. Before this filter we were
    # scanning every market_resolution row (~hundreds), making ~11 RPC calls
    # each, and getting 429'd by the public Polygon RPC every cycle.
    resolved_rows = conn.execute(
        """
        SELECT DISTINCT mr.market_id, mr.resolved_outcome
        FROM market_resolutions mr
        INNER JOIN positions p ON p.market_id = mr.market_id
        WHERE p.status = 'resolved'
          AND p.mode = 'real'
          AND p.is_shadow = 0
          AND p.redeemed_at IS NULL
        """
    ).fetchall()
    if not resolved_rows:
        return []

    results: List[Dict[str, Any]] = []

    for row in resolved_rows:
        market_id = str(row["market_id"])
        resolved_outcome = str(row["resolved_outcome"])

        info = _market_info(conn, market_id)
        if not info or not info["condition_id"]:
            continue

        condition_id = info["condition_id"]
        token_outcomes = _token_ids_for_market(conn, market_id)
        if not token_outcomes:
            continue

        cid_bytes = bytes.fromhex(condition_id[2:]) if condition_id.startswith("0x") else bytes.fromhex(condition_id)

        # Prefer the cached negRisk flag from the gamma snapshot. Only fall
        # back to the 4-call on-chain detection if the snapshot doesn't carry
        # the field (older markets predating the negRisk attribute).
        try:
            if info["neg_risk"] is None:
                first_token = int(token_outcomes[0]["token_id"])
                is_standard = _retry_429(
                    lambda: _is_standard_ctf_token(ctf, wallet, cid_bytes, first_token, USDC_ADDRESS)
                )
            else:
                is_standard = not info["neg_risk"]

            if is_standard:
                result = _redeem_standard(w3, account, wallet, ctf, cid_bytes,
                                          market_id, resolved_outcome, condition_id, token_outcomes)
            else:
                result = _redeem_neg_risk(w3, account, wallet, ctf, cid_bytes,
                                          market_id, resolved_outcome, condition_id, token_outcomes)
        except Exception as exc:
            if _is_rate_limited(exc):
                # RPC is throttling us; abort the rest of this cycle so we
                # don't pile up 429s. Next reconcile tick will retry the
                # remaining markets.
                print(f"[redeemer] rate-limited at {market_id}; aborting cycle", file=sys.stderr)
                break
            print(f"[redeemer] {market_id} failed: {exc}", file=sys.stderr)
            results.append({"market_id": market_id, "condition_id": condition_id,
                            "success": False, "error": str(exc)})
            continue

        if result is None:
            continue

        if result.get("success"):
            try:
                _mark_positions_redeemed(conn, market_id, result["tx_hash"])
            except Exception as db_exc:
                result["db_error"] = str(db_exc)

        results.append(result)

    return results


def _check_balances(ctf, wallet: str, token_outcomes: List[Dict[str, Any]], w3: Web3) -> tuple[int, Dict[str, int]]:
    """Check CTF token balances. Returns (total, {name: balance})."""
    total = 0
    balances: Dict[str, int] = {}
    for outcome in token_outcomes:
        bal = ctf.functions.balanceOf(
            w3.to_checksum_address(wallet), int(outcome["token_id"])
        ).call()
        balances[outcome["name"]] = bal
        total += bal
    return total, balances


def _build_and_send_tx(w3: Web3, account, wallet: str, fn, gas: int = 300_000) -> tuple[str, bool, int]:
    """Build, sign, send a transaction and wait for receipt. Returns (tx_hash, success, gas_used)."""
    tx = fn.build_transaction({
        "from": wallet,
        "nonce": w3.eth.get_transaction_count(wallet),
        "gas": gas,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
        "chainId": 137,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    return tx_hash.hex(), receipt["status"] == 1, receipt["gasUsed"]


def _redeem_standard(w3, account, wallet, ctf, cid_bytes, market_id, resolved_outcome, condition_id, token_outcomes):
    """Redeem standard (non-NegRisk) CTF positions. Caller has already
    confirmed the token structure is standard via the cached negRisk flag
    or _is_standard_ctf_token."""
    total, balances = _check_balances(ctf, wallet, token_outcomes, w3)
    if total == 0:
        return None

    payout_denom = ctf.functions.payoutDenominator(cid_bytes).call()
    if payout_denom == 0:
        return None

    index_sets = [1 << i for i in range(len(token_outcomes))]
    try:
        tx_hash, success, gas_used = _build_and_send_tx(
            w3, account, wallet,
            ctf.functions.redeemPositions(
                w3.to_checksum_address(USDC_ADDRESS), ZERO_BYTES32, cid_bytes, index_sets
            ),
        )
        return {
            "market_id": market_id, "resolved_outcome": resolved_outcome,
            "condition_id": condition_id, "tx_hash": tx_hash, "success": success,
            "gas_used": gas_used, "balances_before": {k: v / 1e6 for k, v in balances.items()},
        }
    except Exception as exc:
        return {
            "market_id": market_id, "condition_id": condition_id,
            "success": False, "error": str(exc),
            "balances_before": {k: v / 1e6 for k, v in balances.items()},
        }


def _redeem_neg_risk(w3, account, wallet, ctf, cid_bytes, market_id, resolved_outcome, condition_id, token_outcomes):
    """Redeem NegRisk positions via NegRiskAdapter.redeemPositions(conditionId, [yesAmt, noAmt])."""
    nr = w3.eth.contract(
        address=w3.to_checksum_address(NEG_RISK_ADAPTER_ADDRESS), abi=NEG_RISK_ABI,
    )

    # Check balances on NegRiskAdapter (wrapped ERC1155)
    total = 0
    balances: Dict[str, int] = {}
    for outcome in token_outcomes:
        bal = nr.functions.balanceOf(
            w3.to_checksum_address(wallet), int(outcome["token_id"])
        ).call()
        balances[outcome["name"]] = bal
        total += bal

    if total == 0:
        return None

    # Amounts array: [yesAmount, noAmount] — always length 2
    amounts = [balances.get("Yes", 0), balances.get("No", 0)]

    # Staticcall to verify before sending
    try:
        nr.functions.redeemPositions(cid_bytes, amounts).call({"from": wallet})
    except Exception as exc:
        print(f"[redeemer] neg_risk staticcall failed for {market_id}: {exc}", file=sys.stderr)
        return {
            "market_id": market_id, "condition_id": condition_id,
            "success": False, "error": f"staticcall_failed: {exc}",
            "balances_before": {k: v / 1e6 for k, v in balances.items()},
        }

    try:
        tx_hash, success, gas_used = _build_and_send_tx(
            w3, account, wallet,
            nr.functions.redeemPositions(cid_bytes, amounts),
            gas=500_000,
        )
        return {
            "market_id": market_id, "resolved_outcome": resolved_outcome,
            "condition_id": condition_id, "tx_hash": tx_hash, "success": success,
            "gas_used": gas_used, "neg_risk": True,
            "balances_before": {k: v / 1e6 for k, v in balances.items()},
        }
    except Exception as exc:
        return {
            "market_id": market_id, "condition_id": condition_id,
            "success": False, "error": str(exc), "neg_risk": True,
            "balances_before": {k: v / 1e6 for k, v in balances.items()},
        }


def _mark_positions_redeemed(conn: sqlite3.Connection, market_id: str, tx_hash: str) -> None:
    """Stamp redeemed_at/redeemed_tx_hash on every resolved real position for
    this market so the SQL filter in redeem_resolved_positions skips it on
    subsequent ticks. (Earlier versions wrote position_events with
    event_type='redeem', but that violates the live CHECK constraint and
    silently rolled back.)"""
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE positions
        SET redeemed_at = ?, redeemed_tx_hash = ?, updated_at = ?
        WHERE market_id = ?
          AND status = 'resolved'
          AND is_shadow = 0
          AND mode = 'real'
          AND redeemed_at IS NULL
        """,
        (now, tx_hash, now, market_id),
    )
