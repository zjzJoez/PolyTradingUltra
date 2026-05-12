# PolyTradingUltra — CLOB V2 Migration PRD

> Last updated: 2026-05-13
> Status: planning
> Trigger: Polymarket CLOB V2 launched 2026-04-28; PolyTradingUltra has been in shadow mode since 2026-04-25 and only discovered the break on 2026-05-12 when shadow was first flipped to real.

---

## 1. Context

### What happened
On **2026-04-28 ~11:00 UTC** Polymarket rolled out CLOB V2: new Exchange smart contracts, a rewritten order book, a new collateral token (pUSD), and a new SDK. The legacy V1 stack stopped accepting orders from external bots at that point. PolyTradingUltra was running in shadow mode (`MVP_SHADOW_MODE=1`, set 2026-04-25 in `/etc/systemd/system/mvp-autopilot.service.d/shadow-mode.conf`) for conviction-tier strategy validation, so no real orders were attempted and the regression was invisible until 2026-05-12.

### What broke
- `py-clob-client 0.34.6` (V1 SDK) still hits the old endpoints. Its `get_balance_allowance` call returns `{"balance":"0","allowances":{<V2 spenders>:"0"}}` for any wallet that hasn't yet been migrated to V2.
- The risk engine reads that as zero collateral → blocks every entry with `insufficient_balance`. Confirmed end-to-end on the first real-mode tick 2026-05-12 15:38 UTC.
- The two `setApprovalForAll` transactions broadcast on 2026-05-12 (tx `0x2451ff6c…` and `0x47b1e85b…`) approved V1 exchange addresses and are **useless** for V2 trading. They are harmless and do not need to be revoked.

### Current verified on-chain state (wallet `0xe65B947Ec589CFDB27292ac1da6eB58AfFE4BdE7`)
| Token / approval | Value |
|---|---|
| USDC.e balance | **37.23** (legacy collateral) |
| pUSD balance | 0.00 |
| MATIC | 5.62 |
| USDC.e → CollateralOnramp allowance | 0 |
| pUSD → CTFExchangeV2 allowance | 0 |
| pUSD → NegRiskCtfExchangeV2 allowance | 0 |
| pUSD → NegRiskAdapter allowance | 0 |
| CTF.setApprovalForAll(CTFExchangeV2) | False |
| CTF.setApprovalForAll(NegRiskCtfExchangeV2) | False |
| CTF.setApprovalForAll(NegRiskAdapter) | True (unchanged across V1→V2) |

### V2 contract addresses (Polygon mainnet)
```
CTFExchangeV2                0xE111180000d2663C0091e4f400237545B87B996B
NegRiskCtfExchangeV2         0xe2222d279d744050d28e00520010520000310F59
NegRiskAdapter (unchanged)   0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296
CTF (unchanged)              0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
pUSD (proxy)                 0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB   # decimals=6, symbol=pUSD
pUSD (implementation)        0x6bBCef9f7ef3B6C592c99e0f206a0DE94Ad0925f
CollateralOnramp (wrap)      0x93070a847efEf7F70739046A929D47a521F5B8ee
CollateralOfframp (unwrap)   0x2957922Eb93258b93368531d39fAcCA3B4dC5854
```

### Reference docs
- [Polymarket V2 Migration Guide](https://docs.polymarket.com/v2-migration)
- [Polymarket Exchange Upgrade: April 28, 2026](https://help.polymarket.com/en/articles/14762452-polymarket-exchange-upgrade-april-28-2026)
- [V2 Contract Addresses](https://docs.polymarket.com/resources/contracts)
- [py-clob-client-v2 PyPI](https://pypi.org/project/py-clob-client-v2/) — latest stable **1.0.1** (2026-05-09), Python ≥3.9.10
- [Polymarket/py-clob-client-v2 GitHub](https://github.com/Polymarket/py-clob-client-v2)

---

## 2. Product Goal

Restore PolyTradingUltra's ability to place real orders against the production Polymarket CLOB, **without regressing any of the existing risk / reconciler / migration-runner / DeepSeek-fallback hardening** that was shipped on branch `fix/polygon-rpc-redeem-loop`.

Concretely:
1. The bot's risk engine sees a non-zero collateral balance.
2. A $1 BUY succeeds end-to-end on a real V2 market (in test conditions).
3. The exit / SELL path is functional (V2 setApprovalForAll on CTF is in place).
4. The redeem flow continues to work (NegRiskAdapter address unchanged → expected to be unaffected, but verify).
5. Conviction-tier sizing, drawdown breaker (50 USDC), CHECK constraints, and all other 2026-05-12 fixes remain wired.

---

## 3. Non-Goals

- **No strategy change.** Keep conviction-tier sizes ($2/$4/$8/$15), confidence thresholds, and risk gates exactly as they are today.
- **No new V2 features.** `builderCode` stays empty / unset. We don't opt into any V2-only options unless required for parity.
- **No historical position rewrite.** The 17 existing positions are V1 conditional tokens, all already resolved and redeemed via NegRiskAdapter (which is V2-compatible). They stay as-is.
- **No DB schema change.** All schema migrations (v01–v11) stay applied; no new migration needed for the V2 swap.
- **No Telegram / control-plane rebuild.** Those layers are untouched.
- **No work on alpha-lab pipeline.** Out of scope.

---

## 4. Scope

### In scope
- SDK swap (`py-clob-client 0.34.6` → `py-clob-client-v2 1.0.1`) in `requirements*.txt` and `.venv`
- Refactor of all 5 V1-SDK call sites:
  - `src/polymarket_mvp/poly_executor.py` (~10 references — order construction, signing, posting, status reading, preflight)
  - `src/polymarket_mvp/risk_engine.py:67-86` (`_real_available_balance_usdc`)
  - `src/polymarket_mvp/ops_snapshot.py:253-255` (status dashboard balance)
  - `src/polymarket_mvp/services/reconciler.py:197` (order snapshot)
  - `tests/test_v03_v04_flow.py` (mocks at L756, L1160, L1219, L1667-1675)
- New on-chain migration script `scripts/migrate_to_clob_v2.py` (idempotent, dry-run flag, journal logging)
- New on-chain probe `scripts/check_v2_state.py` (read-only diagnostic; report wallet vs CLOB consistency)
- New `KNOWN_V2_SPENDERS` constant set (poly_executor.py:25)
- Test-suite update: V2 mock responses, regression assert for non-zero CLOB balance after migration
- One-pager runbook `docs/CLOB_V2_RUNBOOK.md` for operations (how to re-run migration, how to roll back)

### Out of scope
- Strategy / alpha changes (see Non-Goals)
- New tests beyond what's needed for migration coverage
- Multi-wallet / multi-account support
- Listener-mode / WebSocket integration (V2 supports it but we don't use it today)

---

## 5. Functional Requirements

| ID | Requirement | Verification |
|---|---|---|
| F1 | Bot uses `py-clob-client-v2` for all CLOB interactions | grep over `src/` returns no `py_clob_client` import (only `py_clob_client_v2`) |
| F2 | `_real_available_balance_usdc()` returns the wallet's pUSD balance (not USDC.e) | Unit test + live probe on EC2 wallet returns ≥35 after migration |
| F3 | `_real_preflight_check` queries pUSD balance for the COLLATERAL asset type | Same as F2 |
| F4 | Order construction uses V2 schema: no `feeRateBps` / `nonce` / `taker`; includes `timestamp` (ms) | Unit test on `OrderArgs` construction |
| F5 | `KNOWN_V2_SPENDERS` constant includes the three V2 spender addresses (CTFExchangeV2, NegRiskCtfExchangeV2, NegRiskAdapter) | grep / unit test |
| F6 | `scripts/migrate_to_clob_v2.py` is idempotent — re-running on a fully migrated wallet is a no-op with `[skip] already done` logging | Smoke-run twice in dry-run mode |
| F7 | Migration script supports `--dry-run` (read state, print plan, no broadcasts) | CLI flag handling |
| F8 | Redeem flow (`redeemer.py`) still works | Existing redeemer tests pass; smoke-test on a resolved position if one exists post-migration |
| F9 | Reconciler still correctly normalizes V2 order statuses to our internal enum (`submitted/live/filled/failed`) | Existing reconciler tests pass against V2 status fixtures |
| F10 | Conviction-tier sizing, drawdown breaker, all CHECK constraints, all 2026-05-12 commits (e1c841d → 9003c39) remain in effect | All existing tests pass under V2 SDK |

---

## 6. Non-Functional Requirements

- **Reversibility.** Until shadow→real is flipped again, it must be possible to revert the SDK swap with a single `git revert` and have the system function in shadow mode against the old V1 stack (for testing only — V1 production CLOB doesn't accept orders anymore, but shadow mode short-circuits before that matters).
- **Migration script safety.**
  - Idempotent. Reads current state before each tx; skips already-done steps.
  - Journal logs every tx hash + receipt status to stdout AND to `var/clob_v2_migration.log`.
  - Refuses to run if `MATIC < 0.5`.
  - `--dry-run` flag prints the planned tx list with current vs desired state, no broadcasts.
- **Observability.** Add a one-time `[poly-executor] CLOB v2 client initialized` log line on first SDK use per process. Add the V2 SDK version to `ops_snapshot.py` output.
- **No silent fallback.** If the SDK can't be loaded, fail loudly. Do NOT silently fall back to a stub.
- **Backward-compatible env.** All existing env vars (`SIGNATURE_TYPE`, `POLY_CLOB_FUNDER`, `POLY_CLOB_SIGNER_KEY`, `POLY_CLOB_HOST`, `MVP_SHADOW_MODE`, `POLY_MAX_DRAWDOWN_USDC=50`, etc.) keep working with the same semantics.

---

## 7. Migration Plan (Phased)

### Phase 0 — Discovery (no code changes)
- [ ] Read py-clob-client-v2 1.0.1 source end-to-end (`pip install py-clob-client-v2 -t /tmp/v2-readonly` then explore).
- [ ] Document constructor signature, function names, return types in `docs/CLOB_V2_SDK_NOTES.md`.
- [ ] Map every V1 call site to its V2 equivalent (or note "no V2 equivalent — need to find replacement").
- [ ] Confirm `BalanceAllowanceParams` schema; confirm `AssetType.COLLATERAL` returns pUSD (not USDC.e).
- [ ] Confirm whether V2 SDK exposes `get_neg_risk` and `get_tick_size` as before (they're called implicitly inside `create_order` in V1).
- [ ] Confirm whether `client.get_order_book`, `client.get_price`, `client.get_order` have the same signatures.

Deliverable: `docs/CLOB_V2_SDK_NOTES.md` with a mapping table V1 method → V2 method (or replacement strategy).

### Phase 1 — Probe script (no code changes outside `scripts/`)
- [ ] Write `scripts/check_v2_state.py` (read-only). Outputs:
  - Wallet address, MATIC balance, USDC.e balance, pUSD balance
  - All approvals (USDC.e → Onramp, pUSD → 3 V2 spenders, CTF setApprovalForAll → V2 exchanges + NegRiskAdapter)
  - CLOB API response for COLLATERAL via both V1 and V2 SDK side-by-side (if V1 SDK still installable)
  - Pass/fail summary: "ready for V2 trading?" with a checklist
- [ ] Run on EC2; confirm output matches the table in §1 (Current verified on-chain state).

### Phase 2 — On-chain migration script
- [ ] Write `scripts/migrate_to_clob_v2.py`:
  - Step A: `USDC.e.approve(CollateralOnramp, MAX)` if allowance < remaining USDC.e
  - Step B: `CollateralOnramp.wrap(usdc_e_balance)` — wrap full balance 1:1 to pUSD
  - Step C: `pUSD.approve(CTFExchangeV2, MAX)`
  - Step D: `pUSD.approve(NegRiskCtfExchangeV2, MAX)`
  - Step E: `pUSD.approve(NegRiskAdapter, MAX)` (still used in V2 negRisk redeem path)
  - Step F: `CTF.setApprovalForAll(CTFExchangeV2, true)`
  - Step G: `CTF.setApprovalForAll(NegRiskCtfExchangeV2, true)`
  - Each step: check current state; skip if already done; log tx hash + status
  - `--dry-run` prints what would happen
  - Refuses to run if MATIC < 0.5
- [ ] Local syntax test (don't broadcast)
- [ ] Smoke test in `--dry-run` mode on EC2

### Phase 3 — SDK swap + refactor
- [ ] Branch: `feat/clob-v2-migration` off `fix/polygon-rpc-redeem-loop` (current EC2 head)
- [ ] `requirements.txt` / `pyproject.toml`: pin `py-clob-client-v2==1.0.1`; remove `py-clob-client`
- [ ] `pip install -r requirements.txt` in venv
- [ ] Refactor in dependency order:
  1. `src/polymarket_mvp/poly_executor.py`:
     - `_build_clob_client()` to use V2 ClobClient constructor (options object, `chain=137`)
     - `KNOWN_NEG_RISK_SPENDERS` → `KNOWN_V2_SPENDERS` (3 V2 addresses)
     - `_real_preflight_check`: same logic, V2 SDK calls
     - Order construction loop (line ~664+): V2 `OrderArgs` (drop `feeRateBps`/`nonce`/`taker`, add `timestamp`)
     - `client.post_order` signature if changed
     - `_normalize_order_status` mappings for V2-returned statuses (verify whether V2 introduces new status strings)
  2. `src/polymarket_mvp/risk_engine.py:59-86`: same query, V2 SDK
  3. `src/polymarket_mvp/ops_snapshot.py:253-255`: same query, V2 SDK
  4. `src/polymarket_mvp/services/reconciler.py:197`: same call, V2 SDK
- [ ] All `py_clob_client` imports → `py_clob_client_v2`

### Phase 4 — Test-suite update
- [ ] `tests/test_v03_v04_flow.py`:
  - L756, L1160, L1219: update `get_order` mock returns to V2 schema (verify what V2 returns)
  - L1667-1675: update `_balance_allowance` mock to match V2 response shape
  - Add a new regression test: `test_preflight_succeeds_for_buy_with_v2_balance` (mock V2 SDK returning non-zero pUSD balance, verify preflight passes)
- [ ] `tests/test_rate_limit_fallback.py`: verify no V1-specific assumptions
- [ ] `pytest -q` from main checkout — must hit ≥119/119 pass
- [ ] (Optional) Add a live smoke test gated behind an env flag (`POLY_V2_LIVE_TEST=1`) that hits the real CLOB and asserts non-zero balance — manually invoke before flip, never in CI

### Phase 5 — Operational steps (on EC2)
- [ ] `git fetch && git checkout feat/clob-v2-migration && pip install -r requirements.txt`
- [ ] Re-run `scripts/check_v2_state.py` — confirm "not ready yet"
- [ ] Run `scripts/migrate_to_clob_v2.py --dry-run` — review planned txns
- [ ] **Get explicit user authorization** for on-chain broadcasts
- [ ] Run `scripts/migrate_to_clob_v2.py` — broadcast all 7 txns
- [ ] Re-run `scripts/check_v2_state.py` — confirm "ready for V2 trading"
- [ ] Restart service in **shadow mode** first (`MVP_SHADOW_MODE=1` drop-in stays) — let one tick run; confirm scan/propose/risk loops are clean against V2 SDK
- [ ] Remove shadow drop-in + `daemon-reload + restart timer`
- [ ] Watch first real-mode tick: look for `[poly-executor] CLOB v2 client initialized` + a non-`insufficient_balance` execution outcome

### Phase 6 — Acceptance + runbook
- [ ] Confirm acceptance criteria (§8) all pass
- [ ] Write `docs/CLOB_V2_RUNBOOK.md` for ops (re-run migration on new wallet, rollback procedure, version-pin policy)
- [ ] PR `feat/clob-v2-migration` → `main` after one full clean trading cycle
- [ ] Save memory entry confirming migration complete

---

## 8. Acceptance Criteria

| # | Check | How to verify |
|---|---|---|
| A1 | `scripts/check_v2_state.py` reports "ready for V2 trading" | Manual run on EC2 wallet |
| A2 | `client.get_balance_allowance(asset_type=COLLATERAL)` returns `balance > 0` and matches on-chain pUSD balance to within 1 cent | Probe via V2 SDK |
| A3 | `pytest` returns 0 failures on the existing 119 tests + any new regression tests | `pytest -q` |
| A4 | First real-mode propose→risk→execute cycle reaches `execute_record` (i.e., risk engine does NOT return `insufficient_balance` for a $2 conviction-tier proposal) | Journal log inspection after flip |
| A5 | A single $1 BUY (manually crafted via `scripts/poly_test_buy.py` or by waiting for a real propose pass) completes with status in {`filled`, `live`} | Inspect `executions` table |
| A6 | Existing redeem flow still works | Run `redeem_resolved_positions` on a freshly resolved test position OR confirm the existing 14 redeemed positions don't get reverted |
| A7 | Drawdown breaker remains at 50 USDC and active | `grep POLY_MAX_DRAWDOWN_USDC .env` returns 50 |
| A8 | All 5 source files cited in §4 contain zero references to `py_clob_client` (V1) | `grep -r "py_clob_client[^_]" src/` is empty |

---

## 9. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| V2 SDK has different `OrderArgs` validation that rejects our existing price/size combinations | M | High | Test small order in shadow mode + tick-size retry logic from commit `9003c39` already in place |
| `CollateralOnramp.wrap` fails on first try (insufficient gas, wrong arg type) | L | Medium | Migration script catches receipt status; can re-run; only $0.05 MATIC wasted per failed tx |
| V2 returns new status strings we didn't anticipate, mapped to "failed" by current `_normalize_order_status` | M | High | Add status mapping audit during Phase 0; expand the known-status set if needed |
| Reconciler reads V2 order response with new field names; current code silently maps to defaults | M | Medium | Phase 4 tests explicitly assert reconciler against V2 fixtures |
| Dynamic fees (V2 protocol-determined) cause our slippage check to wrongly reject orders | L | Medium | Compute slippage on `requested_price` × `(1 + fee_bps/10000)` if V2 exposes a query; otherwise widen `POLY_FILL_PREMIUM_BPS` from 150 to 250 as a buffer |
| V2 SDK doesn't expose an equivalent of `get_balance_allowance` for the CONDITIONAL asset type per token_id (we use this in preflight) | L | Low | The current `is_sell`-gated preflight (commit `9003c39`) doesn't fire on BUYs; SELLs can fall back to on-chain `balanceOf` check if needed |
| pUSD wrap doesn't preserve full balance (rounding / dust) | L | Low | Migration script wraps `balance - 1` USDC.e (small buffer) and logs the residual dust |
| `update_balance_allowance` race window: between wrap and approve, the CLOB might cache the old zero-balance state for several seconds | L | Low | Run `update_balance_allowance` explicitly after the migration script finishes; sleep 10s before first real tick |
| We need to roll back during testing | M | Medium | The V1 SDK can be reinstalled (`pip install py-clob-client==0.34.6`); the legacy V1 approvals + USDC.e on-chain are untouched if we unwrap (offramp) the pUSD back to USDC.e |

---

## 10. Effort Estimate

| Phase | Estimate | Notes |
|---|---|---|
| Phase 0 — Discovery | 1.5 h | Reading SDK source + writing the V1→V2 mapping table |
| Phase 1 — Probe script | 1 h | Single-file read-only diagnostic |
| Phase 2 — Migration script | 1.5 h | Idempotent on-chain script with dry-run |
| Phase 3 — SDK swap + refactor | 2.5 h | Five files, careful order |
| Phase 4 — Test-suite update | 1.5 h | Mock updates + new regression test |
| Phase 5 — Operational | 1 h | EC2 deploy + on-chain migration + monitored flip |
| Phase 6 — Runbook + PR | 0.5 h | Documentation + cleanup |
| **Total** | **~9.5 h** | Realistic 1-2 focused sessions |

---

## 11. Open Questions (resolve in Phase 0)

1. **Does `py-clob-client-v2` expose `set_api_creds()` / `create_api_key()` with the same semantics?** Our existing API keys were minted under V1 — do they keep working, or do we need to re-mint?
2. **What does the V2 `client.get_order(order_id)` return for a non-existent order?** V1 returned `{}` or raised; we depend on this in the lost-response recovery path.
3. **Does V2 surface a `getClobMarketInfo(conditionID)` Python method, or only over raw HTTP?** Migration guide implies it's required for fee discovery on market buys.
4. **What is the V2 SDK's behavior when MATIC is too low for the wrap tx?** Need to know whether `CollateralOnramp.wrap` reverts cleanly or partially executes.
5. **Are there NEW V2-only error codes from `post_order` we should explicitly map in `_normalize_order_status`?**
6. **Does V2 require explicit `signature_type` per request, or does the client auto-detect from the EOA?** Our SIGNATURE_TYPE=0 path was explicit in V1.

---

## 12. Rollback Plan

If something catastrophic happens during Phase 5:

1. **Service-level (no on-chain impact).** Restore the shadow drop-in:
   ```bash
   ssh polytrade 'echo -e "[Service]\nEnvironment=MVP_SHADOW_MODE=1" | sudo tee /etc/systemd/system/mvp-autopilot.service.d/shadow-mode.conf > /dev/null && sudo systemctl daemon-reload && sudo systemctl restart mvp-autopilot.timer'
   ```
2. **Code-level.** Force-revert the V2 commits:
   ```bash
   git revert --no-edit HEAD~N..HEAD     # exact range depends on commit count
   git push
   ssh polytrade 'cd /home/ubuntu/polymarket-mvp && git pull && pip install -r requirements.txt'
   ```
3. **On-chain unwrap (only if we need the funds back as USDC.e).**
   ```python
   CollateralOfframp.unwrap(pUSD_balance)  # pUSD → USDC.e 1:1
   ```
   Note: this is not necessary for rollback — V1 trading is dead regardless, so leaving funds as pUSD is fine.
4. **Memory hygiene.** Update `memory/project_polymarket_clob_v2_migration.md` with the rollback rationale + remaining work.

---

## 13. Definition of Done

- All Phase 5 steps completed without rollback
- All §8 acceptance criteria pass
- Branch `feat/clob-v2-migration` merged to `main` with a clean commit history (no `wip`/`fixup` left)
- `docs/CLOB_V2_RUNBOOK.md` checked in
- `memory/project_polymarket_clob_v2_migration.md` updated with "migration complete on YYYY-MM-DD, txhash list" footer
- One full 24-hour window where the autopilot ran in real mode with at least one BUY that reached `status=filled` or `status=live` (i.e., the wallet actually traded on V2)
