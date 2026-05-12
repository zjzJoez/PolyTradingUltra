# CLOB V2 SDK Notes — V1 → V2 API Mapping

> Source of truth: `py-clob-client-v2==1.0.1` installed to `/tmp/v2-sdk` on 2026-05-13.
> Cross-reference: this codebase's V1 call sites at poly_executor.py:86, poly_executor.py:281, poly_executor.py:658, risk_engine.py:67, ops_snapshot.py:253, services/reconciler.py:197, tests/test_v03_v04_flow.py:1667.

## TL;DR for the impatient

1. Constructor is **backwards-compatible** at the keyword level — `ClobClient(host, key=..., creds=..., chain_id=..., signature_type=..., funder=...)` works in V2 as-is. No options-object refactor needed.
2. `OrderArgs` is now an **alias for `OrderArgsV2`**. New fields: `builder_code`, `metadata`, `user_usdc_balance`. **Dropped fields:** `fee_rate_bps`, `nonce`, `taker`. Our current call `OrderArgs(token_id=…, price=…, size=…, side=…)` keeps working — the dropped fields had defaults we never set.
3. `timestamp` is set by the V2 order builder internally (`time.time_ns() // 1_000_000`), **not** a field on `OrderArgs`. The PRD §1 note "add `timestamp` (ms)" referred to the wire format, not the Python input.
4. `BalanceAllowanceParams`, `AssetType`, `get_balance_allowance`, `post_order`, `get_order` signatures are unchanged at the SDK level. **What changes is what the API returns:** V2's `ContractConfig.collateral` = pUSD (`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`), not USDC.e, so once the wallet has pUSD + V2 spender approvals, `AssetType.COLLATERAL` returns a non-zero balance.
5. Order routing (V1 vs V2 exchange contract) is decided by the server via `/version`; the client calls `__resolve_version()` automatically. We can build a V2 order today and it will be routed correctly.

## V1 → V2 method-by-method mapping (the 5 we use)

| # | V1 call site | V1 import + call | V2 equivalent | Notes |
|---|---|---|---|---|
| 1 | poly_executor.py:79-116 (`_build_clob_client`) | `from py_clob_client.client import ClobClient` + `from py_clob_client.clob_types import ApiCreds`; `ClobClient(host, key=…, creds=…, chain_id=…, signature_type=…, funder=…)` | `from py_clob_client_v2 import ClobClient, ApiCreds`; same call, same kwargs | V2 adds optional kwargs `builder_config`, `use_server_time`, `retry_on_error`, `fee_slippage` — all default-safe. **No options object.** No `chain → chain_id` rename — both were already `chain_id`. |
| 2 | risk_engine.py:67 + poly_executor.py:281 + ops_snapshot.py:253 (preflight / balance) | `from py_clob_client.clob_types import AssetType, BalanceAllowanceParams`; `client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0))` | Identical call, just swap the import to `py_clob_client_v2`. | Returns `{"balance": str, "allowances": {<V2 spenders>: str}}`. Balance is in pUSD raw units (6 decimals), same divisor as V1. |
| 3 | poly_executor.py:658-678 (order construction) | `from py_clob_client.clob_types import OrderArgs, OrderType`; `from py_clob_client.order_builder.constants import BUY, SELL`; `OrderArgs(token_id=…, price=…, size=…, side=BUY)` → `client.create_order(order)` | Same imports, swap to `py_clob_client_v2`. `OrderArgs` resolves to `OrderArgsV2`. Signed-order dataclass changes from `SignedOrderV1` to `SignedOrderV2` but our code never touches its internals — just passes it to `post_order`. | `create_order` internally calls `__ensure_market_info_cached → get_clob_market_info` to pull tick_size + neg_risk + fee_details. No manual fee discovery needed. **Same tick-size errors** still surface as `PolyException("invalid price (…), min: …")` — our retry logic at executor:683 still applies. |
| 4 | poly_executor.py:691 (`post_order`) | `client.post_order(signed, OrderType.GTC)` | Identical. | New optional `defer_exec: bool = False` param; we ignore it. New `FAK` order type exists; we keep using GTC. Internal `_is_order_version_mismatch` retry path handles the rare server-side V1/V2 cutover hiccup automatically. |
| 5 | poly_executor.py:711 + services/reconciler.py:197 (`get_order`) | `client.get_order(order_id)` | Identical. | Endpoint changes from `/order/{id}` to `/data/order/{id}` internally — no caller impact. Return shape for "not found" needs a live probe (see Open Q2 below); our existing `if recovered_order: ...` truthiness check is safe for either `{}` or HTTP-404-raise. |

## Auxiliary methods we touch indirectly

| Method | V1 → V2 status | Notes |
|---|---|---|
| `client.get_api_keys()` (preflight at poly_executor.py:285) | unchanged | Returns same shape. |
| `client.get_neg_risk(token_id)` | unchanged | Used internally by `create_order`. We don't call it. |
| `client.get_tick_size(token_id)` | unchanged | Same. |
| `client.get_order_book(token_id)` | unchanged | We use this via `_current_worst_price`. Returns same shape (`asks/bids` arrays). |
| `client.set_api_creds(creds)` | unchanged | Confirms Open Q1: yes, V2 exposes this. |
| `client.create_api_key(nonce=None)` / `derive_api_key()` | unchanged | Confirms Open Q1: yes, V2 exposes both. |
| `client.get_clob_market_info(condition_id)` | **new in V2** | Returns `{"t": [...tokens], "mts": "0.01", "nr": false, "fd": {"r": 0.0, "e": 0.0}}`. Auto-called by `create_order`. We don't have to plumb this in. |
| `client.update_balance_allowance(params)` | unchanged | Useful to force CLOB to re-read on-chain state after the wrap+approve migration. Should be called once post-migration. |

## V2 contract config (baked into SDK at `py_clob_client_v2/config.py` for `chain_id=137`)

```python
ContractConfig(
    exchange="0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",         # V1 — legacy, not used by V2 order builder
    neg_risk_adapter="0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296", # unchanged across V1→V2
    neg_risk_exchange="0xC5d563A36AE78145C45a50134d48A1215220f80a", # V1 — legacy
    collateral="0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",       # pUSD proxy (V2 collateral)
    conditional_tokens="0x4D97DCd97eC945f40cF65F87097ACe5EA0476045", # CTF — unchanged
    exchange_v2="0xE111180000d2663C0091e4f400237545B87B996B",      # V2 CTFExchangeV2
    neg_risk_exchange_v2="0xe2222d279d744050d28e00520010520000310F59", # V2 NegRiskCtfExchangeV2
)
```

These match the PRD §1 table exactly.

## SignatureType — backwards-compatible

`SignatureTypeV2` (IntEnum) values in V2:
```
EOA = 0              ← our SIGNATURE_TYPE=0 maps here unchanged
POLY_PROXY = 1
POLY_GNOSIS_SAFE = 2
POLY_1271 = 3        ← new, EIP-1271 smart-wallet path
```

Our `_require_signature_type()` returns 0 — no change needed.

## OrderType enum

V1 had `GTC | FOK | GTD`. V2 adds `FAK` (Fill-And-Kill). We use `GTC` only.

## Side enum

V2 exposes `Side` as an IntEnum (`BUY=0, SELL=1`) from `py_clob_client_v2.order_utils`. The plain strings `"BUY"` / `"SELL"` from `py_clob_client_v2.order_builder.constants` still work the same way. Our existing import path stays valid.

## V2-only optional features (we ignore)

- `builder_config: BuilderConfig` — for fee attribution. We pass `None`, never set `order_args.builder_code`. Stays at `BYTES32_ZERO`.
- `user_usdc_balance` on `OrderArgs` — for fee-adjusted market buys. We use limit orders only, so this stays `None`.
- `metadata` on `OrderArgs` — opaque bytes32. We don't use it.
- `RfqClient` (request-for-quote) — not used.
- `get_pre_migration_orders()` — for retrieving orders placed before V1→V2 cutover. We have zero open orders pre-cutover (all were filled/redeemed long ago), so we don't call it.

## Test-suite touchpoints

`tests/test_v03_v04_flow.py:1667` imports `AssetType` only — a one-line module path swap. The mocked `get_balance_allowance` returns at L756, L1160, L1219, L1667-1675 use the V1 wire shape (`{"balance": str, "allowances": {<v1 spender>: str}}`). Phase 4 needs to update those to V2 wire shape (same JSON keys, but with V2 spender addresses inside `allowances`).

## PRD §11 Open Questions — answers from SDK source

| # | Question | Answer | Confidence |
|---|---|---|---|
| 1 | V2 SDK expose `set_api_creds()` / `create_api_key()` with same semantics? | **Yes.** Both methods present at client.py:214 and client.py:490 with unchanged signatures. HMAC signing in `headers/headers.py` is the same algorithm. Likely existing V1 API keys keep working (the credential is server-side, not version-bound) — but verify on EC2 in Phase 1 by calling `client.get_api_keys()`. If they reject, fall back to `derive_api_key()` which re-derives deterministically from the EOA signature. | High (source-confirmed); live probe still required to be 100% |
| 2 | `get_order(order_id)` return shape for non-existent order? | **Cannot determine from SDK source.** The SDK just does `_get(host + /data/order/{id})` and returns whatever the API returns. The endpoint changed from V1's `/order/{id}` to V2's `/data/order/{id}`. Our existing recovery path (executor:711) uses `if recovered_order:` which is safe for either `{}` (returns false) or HTTP error (raises and falls through to `except Exception: pass`). **Need a live probe in Phase 1** with a known-bad order_id to confirm. | Medium — defensive code already handles both shapes |
| 3 | `getClobMarketInfo(conditionID)` available via Python? | **Yes** — `client.get_clob_market_info(condition_id)` at client.py:309. But **we don't need to call it explicitly**: `create_order` invokes `__ensure_market_info_cached → get_clob_market_info` automatically, caching tick_size + neg_risk + fee_details per token. PRD note "required for fee discovery on market buys" only matters if we use `MarketOrderArgs` — we don't. | High (source-confirmed) |
| 4 | V2 SDK behavior when MATIC too low for `CollateralOnramp.wrap`? | **Not an SDK issue** — this is a web3.py / RPC concern. EVM-standard behavior: insufficient gas means the tx is rejected pre-broadcast by the RPC's `eth_estimateGas`. Our migration script will guard with `if w3.eth.get_balance(addr) < min_wei: refuse` per PRD §6 ("Refuses to run if MATIC < 0.5"). Worst case a single tx wastes ~$0.01 in pending gas and is unrecoverable, which is acceptable. | High (general EVM knowledge + PRD already has the guard) |
| 5 | New V2-only error codes from `post_order` needing `_normalize_order_status` mapping? | **One new SDK-level code:** `ORDER_VERSION_MISMATCH_ERROR = "order_version_mismatch"`, handled internally by `_is_order_version_mismatch + _retry_on_version_update` — never bubbles to our code. The success-flag check at executor:698 (`if response.get("success") is False`) catches everything else. Order status strings (LIVE/FILLED/CANCELED/etc.) come from the API, not the SDK — **Phase 1 live probe** should sniff one real order's `status` field to confirm `_normalize_order_status` covers it. Existing reconciler logic handles partial-fill-then-terminal robustly (commit 0345dcb). | Medium — defensive but probe-able |
| 6 | V2 require explicit `signature_type` per request, or auto-detect? | **Explicit per request** — `BalanceAllowanceParams.signature_type` is still passed, defaulting to `-1` ("use builder.signature_type"). `client.get_balance_allowance` does `int(self.builder.signature_type)` when params is `-1`. Our SIGNATURE_TYPE=0 (EOA) path is unchanged. **No code change needed.** | High (source-confirmed) |

## What changes in our code (preview — actual edits in Phase 3)

| File:line | V1 | V2 |
|---|---|---|
| poly_executor.py:25-29 | `KNOWN_NEG_RISK_SPENDERS = [V1 exchange, V1 neg-risk exchange, NegRiskAdapter]` | `KNOWN_V2_SPENDERS = [CTFExchangeV2, NegRiskCtfExchangeV2, NegRiskAdapter]` |
| poly_executor.py:86-87 | `from py_clob_client.client import ClobClient` / `from py_clob_client.clob_types import ApiCreds` | `from py_clob_client_v2 import ClobClient, ApiCreds` |
| poly_executor.py:108-115 (constructor) | unchanged call shape | unchanged call shape — keep all kwargs |
| poly_executor.py:281 | `from py_clob_client.clob_types import AssetType, BalanceAllowanceParams` | `from py_clob_client_v2 import AssetType, BalanceAllowanceParams` |
| poly_executor.py:658-659 | `from py_clob_client.clob_types import OrderArgs, OrderType` / `from py_clob_client.order_builder.constants import BUY, SELL` | `from py_clob_client_v2 import OrderArgs, OrderType` / `from py_clob_client_v2.order_builder.constants import BUY, SELL` |
| poly_executor.py:666 | `OrderArgs(token_id=…, price=…, size=…, side=side)` | identical (V2 alias) |
| poly_executor.py:691 | `real_client.post_order, signed, OrderType.GTC` | identical |
| poly_executor.py:711 | `real_client.get_order(idempotency_key)` | identical |
| poly_executor.py:733 | error message references `KNOWN_NEG_RISK_SPENDERS` | reference `KNOWN_V2_SPENDERS` instead |
| risk_engine.py:67 | `from py_clob_client.clob_types import AssetType, BalanceAllowanceParams` | `from py_clob_client_v2 import AssetType, BalanceAllowanceParams` |
| ops_snapshot.py:253 | `from py_clob_client.clob_types import AssetType, BalanceAllowanceParams` | `from py_clob_client_v2 import AssetType, BalanceAllowanceParams` |
| services/reconciler.py | (uses `_build_clob_client()` indirectly via import; no direct V1 SDK import) | no source change required — wire is automatic via the executor's builder |
| tests/test_v03_v04_flow.py:1667 | `from py_clob_client.clob_types import AssetType` | `from py_clob_client_v2 import AssetType` |
| requirements.txt / pyproject.toml | `py-clob-client==0.34.6` | `py-clob-client-v2==1.0.1` |
| `_build_clob_client` Python-version guard at poly_executor.py:80-84 | "requires Python 3.10+" | tighten to "Python ≥3.9.10" per V2 SDK requirement — but our EC2 venv already runs Python 3.11, so functionally no change; **keep the 3.10 guard** to avoid loosening |

## Risks / surprises that surfaced during source reading

1. **`_resolve_version` round-trips to `/version`.** First-touch order creation will issue an extra GET. Worst-case adds ~200ms. Cached after first call. Not a problem in our 10-min tick cadence.
2. **`__ensure_market_info_cached` calls `/markets-by-token/{id}` THEN `/clob-markets/{condition_id}`.** That's 2 extra HTTP calls per first-touch token. Cached per-token thereafter. Same low impact.
3. **`OrderArgsV2.builder_code` defaults to `BYTES32_ZERO`** — we never set it, never used, no fee attribution risk.
4. **The `Side` IntEnum import location changed** — but the string constants `BUY = "BUY"` / `SELL = "SELL"` at `py_clob_client_v2.order_builder.constants` are unchanged. Our existing `from py_clob_client.order_builder.constants import BUY, SELL` → swap the module path only.
5. **`get_pre_migration_orders()` exists** but we have nothing to retrieve (all V1 orders were already filled or canceled before the cutover, and we have zero open V1 orders).

---

End of Phase 0 SDK notes. Next: Phase 1 probe script `scripts/check_v2_state.py`.
