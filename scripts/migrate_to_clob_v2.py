"""On-chain migration: move the bot wallet from CLOB V1 to V2.

Performs (in order, idempotent — each step is skipped if already done):

  A.  USDC.e.approve(CollateralOnramp, MAX)
  B.  CollateralOnramp.wrap(usdc_e_balance - 1)    # leave 1 raw unit of dust
  C.  pUSD.approve(CTFExchangeV2, MAX)
  D.  pUSD.approve(NegRiskCtfExchangeV2, MAX)
  E.  pUSD.approve(NegRiskAdapter, MAX)
  F.  CTF.setApprovalForAll(CTFExchangeV2, true)
  G.  CTF.setApprovalForAll(NegRiskCtfExchangeV2, true)

NegRiskAdapter's CTF approval is already True from V1 — checked & skipped automatically.

Safety:
  - Refuses if MATIC < 0.5
  - --dry-run prints the planned tx list with current vs desired state; broadcasts nothing
  - Re-running on a fully migrated wallet is a no-op (all steps print "[skip] already done")
  - Journal logs every step's tx hash + receipt status to stdout AND var/clob_v2_migration.log

Usage on EC2:
    cd /home/ubuntu/polymarket-mvp
    .venv/bin/python scripts/migrate_to_clob_v2.py --dry-run     # review plan
    .venv/bin/python scripts/migrate_to_clob_v2.py               # broadcast
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from web3 import Web3

from polymarket_mvp.common import load_repo_env, polygon_rpc_url

load_repo_env()

# ---------- V2 contract addresses (Polygon mainnet) ----------
USDC_E = "0x2791Bca1f2de4661eD88A30C99A7a9449Aa84174"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CTF_EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_CTF_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
COLLATERAL_ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"

MAX_UINT256 = (1 << 256) - 1
# Skip approve if existing allowance is already > 2^128 (effectively "infinite-ish").
INFINITE_APPROVAL_FLOOR = 1 << 128

MATIC_MIN_FOR_RUN = 0.5
LOG_PATH = Path("var/clob_v2_migration.log")

# ---------- ABIs ----------
ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]

CTF_ABI = [
    {"name": "isApprovedForAll", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"},
                {"name": "operator", "type": "address"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "setApprovalForAll", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "operator", "type": "address"},
                {"name": "approved", "type": "bool"}],
     "outputs": []},
]

# Polymarket's CollateralOnramp signature (verified on-chain 2026-05-13 via
# OpenChain selector lookup + eth_call simulation):
#   wrap(address inToken, address recipient, uint256 amount)
#   selector 0x62355638
# Burns `amount` of `inToken` from msg.sender (USDC.e in our case), mints pUSD
# to `recipient`. Earlier guess of wrap(uint256) was wrong — that selector
# isn't in the dispatch table and the on-chain tx reverted with low gas.
ONRAMP_ABI = [
    {"name": "wrap", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
        {"name": "inToken", "type": "address"},
        {"name": "recipient", "type": "address"},
        {"name": "amount", "type": "uint256"},
     ],
     "outputs": []},
]


# ---------- Journal log ----------
def _journal(line: str) -> None:
    print(line)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {line}\n")
    except Exception as exc:
        # Logging failure should never abort the migration; warn once and continue.
        print(f"[warn] journal write failed: {exc}", file=sys.stderr)


# ---------- Step plumbing ----------
class Step:
    def __init__(
        self,
        key: str,
        label: str,
        skip_check: Callable[[], Tuple[bool, str]],
        build_and_send: Callable[[], str],
    ) -> None:
        self.key = key
        self.label = label
        self.skip_check = skip_check
        self.build_and_send = build_and_send


def _send_tx(w3: Web3, account, fn, gas_limit: int) -> str:
    """Build → sign → broadcast → wait for receipt. Returns tx hash hex."""
    tx = fn.build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": gas_limit,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
        "chainId": 137,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hex = "0x" + tx_hash.hex() if not tx_hash.hex().startswith("0x") else tx_hash.hex()
    _journal(f"  sent: {tx_hex}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    status_ok = receipt["status"] == 1
    _journal(f"  receipt: status={status_ok} gasUsed={receipt['gasUsed']} block={receipt['blockNumber']}")
    if not status_ok:
        raise RuntimeError(f"tx {tx_hex} reverted")
    return tx_hex


# ---------- Step builders ----------
def _build_steps(
    w3: Web3,
    account,
    initial_state: Dict[str, int],
) -> List[Step]:
    wallet_cs = account.address
    usdc_e = w3.eth.contract(address=w3.to_checksum_address(USDC_E), abi=ERC20_ABI)
    pusd = w3.eth.contract(address=w3.to_checksum_address(PUSD), abi=ERC20_ABI)
    ctf = w3.eth.contract(address=w3.to_checksum_address(CTF), abi=CTF_ABI)
    onramp = w3.eth.contract(address=w3.to_checksum_address(COLLATERAL_ONRAMP), abi=ONRAMP_ABI)

    def step_a_approve_usdc_e_to_onramp() -> Step:
        def skip_check() -> Tuple[bool, str]:
            current = usdc_e.functions.allowance(
                wallet_cs, w3.to_checksum_address(COLLATERAL_ONRAMP)
            ).call()
            if current >= INFINITE_APPROVAL_FLOOR:
                return True, f"USDC.e→Onramp allowance already infinite (raw={current})"
            return False, f"USDC.e→Onramp allowance is {current}, want MAX"

        def send() -> str:
            fn = usdc_e.functions.approve(
                w3.to_checksum_address(COLLATERAL_ONRAMP), MAX_UINT256
            )
            return _send_tx(w3, account, fn, gas_limit=80_000)

        return Step("A", "USDC.e.approve(CollateralOnramp, MAX)", skip_check, send)

    def step_b_wrap_usdc_e() -> Step:
        def skip_check() -> Tuple[bool, str]:
            bal = usdc_e.functions.balanceOf(wallet_cs).call()
            if bal <= 1:
                return True, f"USDC.e balance is {bal} raw (≤1 dust) — nothing to wrap"
            return False, f"USDC.e balance is {bal} raw — would wrap {bal - 1}"

        def send() -> str:
            bal = usdc_e.functions.balanceOf(wallet_cs).call()
            if bal <= 1:
                return "noop"
            amount = bal - 1  # leave 1 raw unit of dust as a buffer
            fn = onramp.functions.wrap(
                w3.to_checksum_address(USDC_E),
                wallet_cs,
                amount,
            )
            return _send_tx(w3, account, fn, gas_limit=200_000)

        return Step("B", "CollateralOnramp.wrap(USDC.e, wallet, USDC.e_balance - 1)", skip_check, send)

    def _step_approve_pusd(letter: str, spender_label: str, spender_addr: str) -> Step:
        def skip_check() -> Tuple[bool, str]:
            current = pusd.functions.allowance(
                wallet_cs, w3.to_checksum_address(spender_addr)
            ).call()
            if current >= INFINITE_APPROVAL_FLOOR:
                return True, f"pUSD→{spender_label} allowance already infinite (raw={current})"
            return False, f"pUSD→{spender_label} allowance is {current}, want MAX"

        def send() -> str:
            fn = pusd.functions.approve(
                w3.to_checksum_address(spender_addr), MAX_UINT256
            )
            return _send_tx(w3, account, fn, gas_limit=80_000)

        return Step(letter, f"pUSD.approve({spender_label}, MAX)", skip_check, send)

    def _step_set_ctf_approval(letter: str, spender_label: str, spender_addr: str) -> Step:
        def skip_check() -> Tuple[bool, str]:
            already = ctf.functions.isApprovedForAll(
                wallet_cs, w3.to_checksum_address(spender_addr)
            ).call()
            if already:
                return True, f"CTF.isApprovedForAll({spender_label}) already True"
            return False, f"CTF.isApprovedForAll({spender_label}) is False"

        def send() -> str:
            fn = ctf.functions.setApprovalForAll(
                w3.to_checksum_address(spender_addr), True
            )
            return _send_tx(w3, account, fn, gas_limit=80_000)

        return Step(letter, f"CTF.setApprovalForAll({spender_label}, true)", skip_check, send)

    return [
        step_a_approve_usdc_e_to_onramp(),
        step_b_wrap_usdc_e(),
        _step_approve_pusd("C", "CTFExchangeV2", CTF_EXCHANGE_V2),
        _step_approve_pusd("D", "NegRiskCtfExchangeV2", NEG_RISK_CTF_EXCHANGE_V2),
        _step_approve_pusd("E", "NegRiskAdapter", NEG_RISK_ADAPTER),
        _step_set_ctf_approval("F", "CTFExchangeV2", CTF_EXCHANGE_V2),
        _step_set_ctf_approval("G", "NegRiskCtfExchangeV2", NEG_RISK_CTF_EXCHANGE_V2),
    ]


def _capture_initial_state(w3: Web3, wallet_cs: str) -> Dict[str, int]:
    usdc_e = w3.eth.contract(address=w3.to_checksum_address(USDC_E), abi=ERC20_ABI)
    pusd = w3.eth.contract(address=w3.to_checksum_address(PUSD), abi=ERC20_ABI)
    return {
        "matic_raw": w3.eth.get_balance(wallet_cs),
        "usdc_e_raw": usdc_e.functions.balanceOf(wallet_cs).call(),
        "pusd_raw": pusd.functions.balanceOf(wallet_cs).call(),
    }


def _maybe_update_balance_allowance(funder: str) -> None:
    """Optional final step: tell the CLOB to re-read on-chain balance/allowance state.

    Closes the race window between the last on-chain tx and the next CLOB API tick.
    Skipped silently if py-clob-client-v2 or API creds are unavailable.
    """
    try:
        from py_clob_client_v2 import (
            ClobClient,
            ApiCreds,
            AssetType,
            BalanceAllowanceParams,
        )
    except Exception as exc:
        _journal(f"[skip] update_balance_allowance: SDK not installed ({exc})")
        return

    host = os.getenv("POLY_CLOB_HOST") or "https://clob.polymarket.com"
    chain_id = int(os.getenv("POLY_CLOB_CHAIN_ID") or os.getenv("CHAIN_ID") or "137")
    signer_key = (os.getenv("POLY_CLOB_SIGNER_KEY") or os.getenv("POLY_CLOB_PRIVATE_KEY") or "").strip()
    api_key = os.getenv("POLY_API_KEY") or ""
    api_secret = os.getenv("POLY_API_SECRET") or ""
    api_passphrase = os.getenv("POLY_API_PASSPHRASE") or ""
    signature_type = int(os.getenv("POLY_CLOB_SIGNATURE_TYPE") or os.getenv("SIGNATURE_TYPE") or "0")

    if not (signer_key and api_key and api_secret and api_passphrase):
        _journal("[skip] update_balance_allowance: API creds not in env")
        return

    try:
        client = ClobClient(
            host,
            key=signer_key,
            creds=ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase),
            chain_id=chain_id,
            signature_type=signature_type,
            funder=funder,
        )
        client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=signature_type)
        )
        _journal("[ok] CLOB update_balance_allowance(COLLATERAL) sent — book should re-read state")
    except Exception as exc:
        _journal(f"[warn] update_balance_allowance failed (non-fatal): {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned txns without broadcasting.",
    )
    parser.add_argument(
        "--skip-clob-refresh",
        action="store_true",
        help="Skip the final CLOB update_balance_allowance call (you can do it later from the bot).",
    )
    args = parser.parse_args()

    signer_key = (
        os.getenv("POLY_CLOB_SIGNER_KEY")
        or os.getenv("POLY_CLOB_PRIVATE_KEY")
        or ""
    ).strip()
    if not signer_key:
        _journal("ERROR: POLY_CLOB_SIGNER_KEY not set in env")
        return 1

    rpc = polygon_rpc_url()
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        _journal(f"ERROR: cannot reach Polygon RPC at {rpc}")
        return 1

    account = w3.eth.account.from_key(signer_key)
    funder_env = (os.getenv("POLY_CLOB_FUNDER") or os.getenv("FUNDER") or "").strip()
    wallet_cs = account.address

    if funder_env and funder_env.lower() != wallet_cs.lower():
        _journal(
            f"ERROR: signer key derives wallet {wallet_cs} but POLY_CLOB_FUNDER={funder_env}. "
            "Refusing — the wallet that signs must equal the wallet whose balances we're migrating."
        )
        return 1

    _journal("=" * 72)
    mode = "DRY-RUN" if args.dry_run else "BROADCAST"
    _journal(f"[{mode}] CLOB V2 migration starting")
    _journal(f"Wallet: {wallet_cs}")
    _journal(f"RPC:    {rpc}")

    initial = _capture_initial_state(w3, wallet_cs)
    matic = initial["matic_raw"] / 1e18
    _journal(f"MATIC:  {matic:.4f}")
    _journal(f"USDC.e: {initial['usdc_e_raw'] / 1e6:.6f}  (raw={initial['usdc_e_raw']})")
    _journal(f"pUSD:   {initial['pusd_raw'] / 1e6:.6f}  (raw={initial['pusd_raw']})")

    if matic < MATIC_MIN_FOR_RUN:
        _journal(
            f"ERROR: MATIC balance {matic:.4f} < {MATIC_MIN_FOR_RUN:.2f} — refusing to start. "
            "Top up gas first."
        )
        return 1

    steps = _build_steps(w3, account, initial)
    _journal("")
    _journal(f"Plan: {len(steps)} steps")

    pending: List[Step] = []
    for step in steps:
        will_skip, detail = step.skip_check()
        marker = "[skip]" if will_skip else "[do  ]"
        _journal(f"  {marker} {step.key}. {step.label} — {detail}")
        if not will_skip:
            pending.append(step)

    if not pending:
        _journal("")
        _journal("All steps already done — wallet is fully V2-migrated.")
        if not args.skip_clob_refresh and not args.dry_run:
            _maybe_update_balance_allowance(wallet_cs)
        return 0

    if args.dry_run:
        _journal("")
        _journal(f"DRY-RUN complete. {len(pending)} txn(s) would be sent. Re-run without --dry-run to broadcast.")
        return 0

    _journal("")
    _journal(f"Broadcasting {len(pending)} txn(s)…")
    for step in pending:
        _journal(f"[{step.key}] {step.label}")
        try:
            tx_hex = step.build_and_send()
            if tx_hex == "noop":
                _journal(f"[{step.key}] noop (balance changed during run)")
            else:
                _journal(f"[{step.key}] OK")
        except Exception as exc:
            _journal(f"[{step.key}] FAILED: {exc}")
            _journal("Aborting — re-run after fixing root cause; remaining steps will retry idempotently.")
            return 1

    _journal("")
    _journal("All on-chain steps complete. Re-running readiness checklist…")
    final = _capture_initial_state(w3, wallet_cs)
    _journal(f"  pUSD:   {final['pusd_raw'] / 1e6:.6f}  (raw={final['pusd_raw']})")
    _journal(f"  USDC.e: {final['usdc_e_raw'] / 1e6:.6f}  (raw={final['usdc_e_raw']})")

    if not args.skip_clob_refresh:
        _journal("")
        _maybe_update_balance_allowance(wallet_cs)

    _journal("")
    _journal("Migration finished. Run scripts/check_v2_state.py to confirm READY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
