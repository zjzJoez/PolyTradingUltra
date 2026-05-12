"""Read-only diagnostic that reports whether the bot wallet is ready to trade on
Polymarket CLOB V2.

Outputs:
  - MATIC / USDC.e / pUSD on-chain balances
  - All allowances needed for V2 (USDC.e -> CollateralOnramp, pUSD -> 3 V2 spenders)
  - All CTF setApprovalForAll bits needed for V2 (CTFExchangeV2, NegRiskCtfExchangeV2,
    NegRiskAdapter)
  - Optional V2-SDK probe (--probe-sdk): exercises get_api_keys / get_balance_allowance /
    get_order against the live CLOB so we can confirm V1-minted credentials still
    authenticate and what get_order returns for a non-existent id

Does not broadcast anything. Safe to run any time.

Usage on EC2:
    cd /home/ubuntu/polymarket-mvp
    .venv/bin/python scripts/check_v2_state.py
    .venv/bin/python scripts/check_v2_state.py --probe-sdk
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Tuple

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

# Legacy V1 spenders — useless for V2 but worth reporting so the operator sees
# the stranded approvals from the 2026-05-12 V1 setApprovalForAll txns.
CTF_EXCHANGE_V1 = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE_V1 = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

# ---------- ABIs ----------
ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "name": "symbol",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
]

CTF_ABI = [
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


# ---------- Helpers ----------
def _bool_str(v: bool) -> str:
    return "✓" if v else "✗"


def _fmt_balance(raw: int, decimals: int) -> str:
    return f"{raw / (10 ** decimals):,.6f}"


def _erc20(w3: Web3, addr: str):
    return w3.eth.contract(address=w3.to_checksum_address(addr), abi=ERC20_ABI)


def _read_wallet(w3: Web3, wallet: str) -> Dict[str, Any]:
    """Read on-chain wallet state. Pure RPC reads; no signing required."""
    wallet_cs = w3.to_checksum_address(wallet)
    matic_raw = w3.eth.get_balance(wallet_cs)
    usdc_e = _erc20(w3, USDC_E)
    pusd = _erc20(w3, PUSD)
    ctf = w3.eth.contract(address=w3.to_checksum_address(CTF), abi=CTF_ABI)

    usdc_e_balance = usdc_e.functions.balanceOf(wallet_cs).call()
    usdc_e_to_onramp = usdc_e.functions.allowance(
        wallet_cs, w3.to_checksum_address(COLLATERAL_ONRAMP)
    ).call()

    pusd_balance = pusd.functions.balanceOf(wallet_cs).call()
    pusd_to_v2 = pusd.functions.allowance(
        wallet_cs, w3.to_checksum_address(CTF_EXCHANGE_V2)
    ).call()
    pusd_to_neg_risk_v2 = pusd.functions.allowance(
        wallet_cs, w3.to_checksum_address(NEG_RISK_CTF_EXCHANGE_V2)
    ).call()
    pusd_to_neg_risk_adapter = pusd.functions.allowance(
        wallet_cs, w3.to_checksum_address(NEG_RISK_ADAPTER)
    ).call()

    ctf_to_v2 = ctf.functions.isApprovedForAll(
        wallet_cs, w3.to_checksum_address(CTF_EXCHANGE_V2)
    ).call()
    ctf_to_neg_risk_v2 = ctf.functions.isApprovedForAll(
        wallet_cs, w3.to_checksum_address(NEG_RISK_CTF_EXCHANGE_V2)
    ).call()
    ctf_to_neg_risk_adapter = ctf.functions.isApprovedForAll(
        wallet_cs, w3.to_checksum_address(NEG_RISK_ADAPTER)
    ).call()

    ctf_to_v1_exchange = ctf.functions.isApprovedForAll(
        wallet_cs, w3.to_checksum_address(CTF_EXCHANGE_V1)
    ).call()
    ctf_to_v1_neg_risk = ctf.functions.isApprovedForAll(
        wallet_cs, w3.to_checksum_address(NEG_RISK_CTF_EXCHANGE_V1)
    ).call()

    return {
        "wallet": wallet_cs,
        "matic_raw": matic_raw,
        "matic": matic_raw / 1e18,
        "usdc_e_balance_raw": usdc_e_balance,
        "usdc_e_balance": usdc_e_balance / 1e6,
        "usdc_e_to_collateral_onramp_raw": usdc_e_to_onramp,
        "pusd_balance_raw": pusd_balance,
        "pusd_balance": pusd_balance / 1e6,
        "pusd_to_ctf_exchange_v2_raw": pusd_to_v2,
        "pusd_to_neg_risk_ctf_exchange_v2_raw": pusd_to_neg_risk_v2,
        "pusd_to_neg_risk_adapter_raw": pusd_to_neg_risk_adapter,
        "ctf_setApprovalForAll_ctf_exchange_v2": ctf_to_v2,
        "ctf_setApprovalForAll_neg_risk_ctf_exchange_v2": ctf_to_neg_risk_v2,
        "ctf_setApprovalForAll_neg_risk_adapter": ctf_to_neg_risk_adapter,
        "ctf_setApprovalForAll_v1_exchange": ctf_to_v1_exchange,
        "ctf_setApprovalForAll_v1_neg_risk_exchange": ctf_to_v1_neg_risk,
    }


def _readiness_checklist(state: Dict[str, Any]) -> Tuple[List[Tuple[str, bool, str]], bool]:
    """Compute per-check pass/fail and overall readiness for V2 trading."""
    checks: List[Tuple[str, bool, str]] = []

    matic_ok = state["matic"] >= 0.5
    checks.append((
        "MATIC ≥ 0.5 for gas",
        matic_ok,
        f"have {state['matic']:.4f} MATIC",
    ))

    has_collateral_path = state["pusd_balance_raw"] > 0 or state["usdc_e_balance_raw"] > 0
    checks.append((
        "Some collateral (pUSD or pre-wrap USDC.e) present",
        has_collateral_path,
        f"pUSD={state['pusd_balance']:.6f}, USDC.e={state['usdc_e_balance']:.6f}",
    ))

    pusd_to_v2_ok = state["pusd_to_ctf_exchange_v2_raw"] > 0
    pusd_to_neg_risk_v2_ok = state["pusd_to_neg_risk_ctf_exchange_v2_raw"] > 0
    pusd_to_neg_risk_adapter_ok = state["pusd_to_neg_risk_adapter_raw"] > 0
    checks.append((
        "pUSD allowance → CTFExchangeV2 > 0",
        pusd_to_v2_ok,
        f"raw={state['pusd_to_ctf_exchange_v2_raw']}",
    ))
    checks.append((
        "pUSD allowance → NegRiskCtfExchangeV2 > 0",
        pusd_to_neg_risk_v2_ok,
        f"raw={state['pusd_to_neg_risk_ctf_exchange_v2_raw']}",
    ))
    checks.append((
        "pUSD allowance → NegRiskAdapter > 0",
        pusd_to_neg_risk_adapter_ok,
        f"raw={state['pusd_to_neg_risk_adapter_raw']}",
    ))

    checks.append((
        "CTF.setApprovalForAll(CTFExchangeV2) == true",
        state["ctf_setApprovalForAll_ctf_exchange_v2"],
        f"{state['ctf_setApprovalForAll_ctf_exchange_v2']}",
    ))
    checks.append((
        "CTF.setApprovalForAll(NegRiskCtfExchangeV2) == true",
        state["ctf_setApprovalForAll_neg_risk_ctf_exchange_v2"],
        f"{state['ctf_setApprovalForAll_neg_risk_ctf_exchange_v2']}",
    ))
    checks.append((
        "CTF.setApprovalForAll(NegRiskAdapter) == true",
        state["ctf_setApprovalForAll_neg_risk_adapter"],
        f"{state['ctf_setApprovalForAll_neg_risk_adapter']}",
    ))

    has_actual_pusd = state["pusd_balance_raw"] > 0
    spender_approvals_ok = pusd_to_v2_ok and pusd_to_neg_risk_v2_ok and pusd_to_neg_risk_adapter_ok
    ctf_ok = (
        state["ctf_setApprovalForAll_ctf_exchange_v2"]
        and state["ctf_setApprovalForAll_neg_risk_ctf_exchange_v2"]
        and state["ctf_setApprovalForAll_neg_risk_adapter"]
    )
    ready = bool(matic_ok and has_actual_pusd and spender_approvals_ok and ctf_ok)
    return checks, ready


def _probe_sdk(funder: str) -> Dict[str, Any]:
    """Hit the live CLOB via the V2 SDK and report three things the offline analysis can't:

    1. Does our existing V1-minted API key authenticate? (get_api_keys)
    2. What does get_order return for an obviously-nonexistent order id?
    3. What does get_balance_allowance(COLLATERAL) report? Compare against the on-chain pUSD balance.
    """
    out: Dict[str, Any] = {}
    try:
        from py_clob_client_v2 import (
            ClobClient,
            ApiCreds,
            AssetType,
            BalanceAllowanceParams,
        )
    except Exception as exc:
        out["import_error"] = f"{type(exc).__name__}: {exc}"
        return out

    host = os.getenv("POLY_CLOB_HOST") or "https://clob.polymarket.com"
    chain_id = int(os.getenv("POLY_CLOB_CHAIN_ID") or os.getenv("CHAIN_ID") or "137")
    signer_key = (
        os.getenv("POLY_CLOB_SIGNER_KEY")
        or os.getenv("POLY_CLOB_PRIVATE_KEY")
        or ""
    ).strip()
    api_key = os.getenv("POLY_API_KEY") or ""
    api_secret = os.getenv("POLY_API_SECRET") or ""
    api_passphrase = os.getenv("POLY_API_PASSPHRASE") or ""
    signature_type = int(os.getenv("POLY_CLOB_SIGNATURE_TYPE") or os.getenv("SIGNATURE_TYPE") or "0")

    if not (signer_key and api_key and api_secret and api_passphrase):
        out["skipped"] = "missing one of POLY_CLOB_SIGNER_KEY / POLY_API_KEY / POLY_API_SECRET / POLY_API_PASSPHRASE"
        return out

    try:
        creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
        client = ClobClient(
            host,
            key=signer_key,
            creds=creds,
            chain_id=chain_id,
            signature_type=signature_type,
            funder=funder,
        )
        out["host"] = host
        out["chain_id"] = chain_id
        out["client_address"] = client.get_address()
    except Exception as exc:
        out["constructor_error"] = f"{type(exc).__name__}: {exc}"
        return out

    try:
        keys = client.get_api_keys()
        if isinstance(keys, dict):
            key_list = keys.get("apiKeys") or keys.get("api_keys") or []
        elif isinstance(keys, list):
            key_list = keys
        else:
            key_list = []
        out["get_api_keys_ok"] = True
        out["get_api_keys_count"] = len(key_list)
    except Exception as exc:
        out["get_api_keys_ok"] = False
        out["get_api_keys_error"] = f"{type(exc).__name__}: {exc}"

    try:
        ba = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=signature_type)
        )
        out["get_balance_allowance_ok"] = True
        out["get_balance_allowance"] = ba
    except Exception as exc:
        out["get_balance_allowance_ok"] = False
        out["get_balance_allowance_error"] = f"{type(exc).__name__}: {exc}"

    bogus = "0xdeadbeef-not-a-real-order-id"
    try:
        result = client.get_order(bogus)
        out["get_order_nonexistent_ok"] = True
        out["get_order_nonexistent_response"] = result
    except Exception as exc:
        out["get_order_nonexistent_ok"] = False
        out["get_order_nonexistent_error"] = f"{type(exc).__name__}: {exc}"

    return out


def _print_state(state: Dict[str, Any]) -> None:
    print(f"Wallet:                                  {state['wallet']}")
    print(f"RPC:                                     {polygon_rpc_url()}")
    print(f"MATIC:                                   {state['matic']:.4f}")
    print(f"USDC.e balance:                          {state['usdc_e_balance']:.6f}  (raw={state['usdc_e_balance_raw']})")
    print(f"pUSD balance:                            {state['pusd_balance']:.6f}  (raw={state['pusd_balance_raw']})")
    print()
    print("Approvals (ERC-20 allowance):")
    print(f"  USDC.e → CollateralOnramp:             raw={state['usdc_e_to_collateral_onramp_raw']}")
    print(f"  pUSD   → CTFExchangeV2:                raw={state['pusd_to_ctf_exchange_v2_raw']}")
    print(f"  pUSD   → NegRiskCtfExchangeV2:         raw={state['pusd_to_neg_risk_ctf_exchange_v2_raw']}")
    print(f"  pUSD   → NegRiskAdapter:               raw={state['pusd_to_neg_risk_adapter_raw']}")
    print()
    print("Approvals (CTF.setApprovalForAll):")
    print(f"  CTFExchangeV2:                         {_bool_str(state['ctf_setApprovalForAll_ctf_exchange_v2'])}  {state['ctf_setApprovalForAll_ctf_exchange_v2']}")
    print(f"  NegRiskCtfExchangeV2:                  {_bool_str(state['ctf_setApprovalForAll_neg_risk_ctf_exchange_v2'])}  {state['ctf_setApprovalForAll_neg_risk_ctf_exchange_v2']}")
    print(f"  NegRiskAdapter:                        {_bool_str(state['ctf_setApprovalForAll_neg_risk_adapter'])}  {state['ctf_setApprovalForAll_neg_risk_adapter']}")
    print()
    print("Stranded V1 approvals (informational; harmless):")
    print(f"  CTFExchange (V1):                      {_bool_str(state['ctf_setApprovalForAll_v1_exchange'])}  {state['ctf_setApprovalForAll_v1_exchange']}")
    print(f"  NegRiskCTFExchange (V1):               {_bool_str(state['ctf_setApprovalForAll_v1_neg_risk_exchange'])}  {state['ctf_setApprovalForAll_v1_neg_risk_exchange']}")


def _print_checklist(checks: List[Tuple[str, bool, str]], ready: bool) -> None:
    print("Readiness checklist:")
    for label, ok, detail in checks:
        print(f"  [{_bool_str(ok)}] {label}  ({detail})")
    print()
    if ready:
        print("RESULT: ✓ READY for V2 trading.")
    else:
        print("RESULT: ✗ NOT READY for V2 trading — run scripts/migrate_to_clob_v2.py.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe-sdk",
        action="store_true",
        help="Also exercise py-clob-client-v2 against the live CLOB (requires API creds + signer key).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON payload after the human-readable report.",
    )
    args = parser.parse_args()

    funder = (os.getenv("POLY_CLOB_FUNDER") or os.getenv("FUNDER") or "").strip()
    if not funder:
        print("ERROR: POLY_CLOB_FUNDER (or FUNDER) must be set in env", file=sys.stderr)
        return 1

    rpc = polygon_rpc_url()
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        print(f"ERROR: cannot reach Polygon RPC at {rpc}", file=sys.stderr)
        return 1

    state = _read_wallet(w3, funder)
    _print_state(state)
    print()
    checks, ready = _readiness_checklist(state)
    _print_checklist(checks, ready)

    sdk_report: Dict[str, Any] = {}
    if args.probe_sdk:
        print()
        print("─" * 72)
        print("SDK probe (py-clob-client-v2 → live CLOB):")
        sdk_report = _probe_sdk(funder)
        print(json.dumps(sdk_report, indent=2, default=str))

    if args.json:
        print()
        print("─" * 72)
        print("JSON payload:")
        print(json.dumps({
            "state": state,
            "checks": [{"label": l, "ok": ok, "detail": d} for l, ok, d in checks],
            "ready_for_v2_trading": ready,
            "sdk_probe": sdk_report,
        }, indent=2, default=str))

    return 0 if ready else 2


if __name__ == "__main__":
    sys.exit(main())
