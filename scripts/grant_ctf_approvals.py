"""Grant ERC-1155 setApprovalForAll on the CTF contract to the two CLOB
exchanges that need it for SELL flows.

The wallet at `0xe65B947Ec589CFDB27292ac1da6eB58AfFE4BdE7` has infinite USDC
approval to all three exchanges but `setApprovalForAll` is False for both
the non-negRisk CTFExchange and the NegRiskCTFExchange — so any exit-side
SELL would fail on-chain. NegRiskAdapter is already approved, so redeems
keep working.

Run once before flipping shadow→real:

    ssh polytrade 'cd /home/ubuntu/polymarket-mvp && \\
      .venv/bin/python scripts/grant_ctf_approvals.py'

Costs roughly $0.05 in MATIC per transaction.
"""
from __future__ import annotations

import os
import sys

from web3 import Web3

from polymarket_mvp.common import load_repo_env, polygon_rpc_url

load_repo_env()

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

SPENDERS_NEEDING_APPROVAL = [
    ("CTFExchange (non-negRisk)", CTF_EXCHANGE),
    ("NegRiskCTFExchange", NEG_RISK_CTF_EXCHANGE),
]

CTF_ABI = [
    {
        "name": "setApprovalForAll",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "outputs": [],
    },
    {
        "name": "isApprovedForAll",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]


def main() -> int:
    signer_key = (os.getenv("POLY_CLOB_SIGNER_KEY") or "").strip()
    if not signer_key:
        print("ERROR: POLY_CLOB_SIGNER_KEY not set in env", file=sys.stderr)
        return 1

    w3 = Web3(Web3.HTTPProvider(polygon_rpc_url()))
    if not w3.is_connected():
        print(f"ERROR: cannot reach Polygon RPC at {polygon_rpc_url()}", file=sys.stderr)
        return 1

    account = w3.eth.account.from_key(signer_key)
    wallet = account.address
    print(f"Wallet: {wallet}")
    print(f"RPC:    {polygon_rpc_url()}")
    matic_balance = w3.eth.get_balance(wallet) / 1e18
    print(f"MATIC:  {matic_balance:.4f}")
    if matic_balance < 0.01:
        print("WARN: MATIC balance is very low; transactions may fail.")

    ctf = w3.eth.contract(
        address=w3.to_checksum_address(CTF_ADDRESS),
        abi=CTF_ABI,
    )

    # Always print all four current approvals for full context.
    for label, addr in [
        ("CTFExchange", CTF_EXCHANGE),
        ("NegRiskCTFExchange", NEG_RISK_CTF_EXCHANGE),
        ("NegRiskAdapter", NEG_RISK_ADAPTER),
    ]:
        approved = ctf.functions.isApprovedForAll(
            w3.to_checksum_address(wallet),
            w3.to_checksum_address(addr),
        ).call()
        print(f"  before: setApprovalForAll({label}, {addr[:10]}…) = {approved}")

    print()

    for label, spender in SPENDERS_NEEDING_APPROVAL:
        spender_cs = w3.to_checksum_address(spender)
        already = ctf.functions.isApprovedForAll(
            w3.to_checksum_address(wallet), spender_cs
        ).call()
        if already:
            print(f"[skip] {label}: already approved")
            continue

        fn = ctf.functions.setApprovalForAll(spender_cs, True)
        # Polygon EIP-1559: maxFeePerGas = base * 2, priority = 30 gwei
        # mirroring the redeemer's gas pricing.
        tx = fn.build_transaction({
            "from": wallet,
            "nonce": w3.eth.get_transaction_count(wallet),
            "gas": 80_000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
            "chainId": 137,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"[{label}] sent: 0x{tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        ok = receipt["status"] == 1
        print(f"[{label}] status={ok} gasUsed={receipt['gasUsed']}")
        if not ok:
            print(f"ERROR: tx 0x{tx_hash.hex()} reverted", file=sys.stderr)
            return 1

    print()
    print("After:")
    for label, spender in SPENDERS_NEEDING_APPROVAL:
        approved = ctf.functions.isApprovedForAll(
            w3.to_checksum_address(wallet),
            w3.to_checksum_address(spender),
        ).call()
        print(f"  {label}: setApprovalForAll = {approved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
