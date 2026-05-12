# CLOB V2 Runbook

> Operational reference for the V1→V2 migration shipped on branch `feat/clob-v2-migration`.
> Companion docs: [CLOB_V2_MIGRATION_PRD.md](CLOB_V2_MIGRATION_PRD.md), [CLOB_V2_SDK_NOTES.md](CLOB_V2_SDK_NOTES.md).

## 1. First-time deploy (the path we're on now)

```bash
# Local
git push -u origin feat/clob-v2-migration

# EC2 — get the code
ssh polytrade
cd /home/ubuntu/polymarket-mvp
git fetch origin
git checkout feat/clob-v2-migration
.venv/bin/pip install -e '.[real-exec]'   # installs py-clob-client-v2==1.0.1
.venv/bin/python -c "import py_clob_client_v2; print(py_clob_client_v2.__file__)"   # sanity
```

### Step 1 — Verify pre-migration state
```bash
ssh polytrade '/home/ubuntu/polymarket-mvp/.venv/bin/python /home/ubuntu/polymarket-mvp/scripts/check_v2_state.py --probe-sdk'
```
Expected at this point: `RESULT: ✗ NOT READY for V2 trading`. The SDK probe section answers PRD §11 open questions 1, 2, 5 (V1-key acceptance, get_order non-existent shape, balance allowance shape).

### Step 2 — Dry-run the migration
```bash
ssh polytrade '/home/ubuntu/polymarket-mvp/.venv/bin/python /home/ubuntu/polymarket-mvp/scripts/migrate_to_clob_v2.py --dry-run'
```
Expected output: 7 steps total, 1 already-done (`G` for CTF.setApprovalForAll(NegRiskAdapter) — was True from V1 unchanged) plus 6 `[do]` entries. **No txns are broadcast.**

### Step 3 — Get explicit broadcast approval, then run
**Only after the user says "approve, broadcast":**
```bash
ssh polytrade '/home/ubuntu/polymarket-mvp/.venv/bin/python /home/ubuntu/polymarket-mvp/scripts/migrate_to_clob_v2.py'
```
Total cost: ~$0.30 in MATIC across 6 txns (~$0.05 per setApprovalForAll/approve, ~$0.10 for the wrap). The script journals every tx hash to stdout AND to `/home/ubuntu/polymarket-mvp/var/clob_v2_migration.log`.

### Step 4 — Confirm readiness
```bash
ssh polytrade '/home/ubuntu/polymarket-mvp/.venv/bin/python /home/ubuntu/polymarket-mvp/scripts/check_v2_state.py --probe-sdk'
```
Expected: `RESULT: ✓ READY for V2 trading`. SDK probe should show non-zero `balance` in `get_balance_allowance` output.

### Step 5 — Restart autopilot in shadow mode first
Shadow drop-in is currently at `/etc/systemd/system/mvp-autopilot.service.d/shadow-mode.conf`. **Keep it for one tick** to verify the V2 SDK plumbing doesn't blow up on the scan/propose/risk path.
```bash
ssh polytrade 'sudo systemctl daemon-reload && sudo systemctl restart mvp-autopilot.timer'
ssh polytrade 'journalctl -u mvp-autopilot.service -n 50 --no-pager | grep -E "CLOB|risk|balance"'
```

### Step 6 — Flip to real mode
**Only after one clean shadow tick on V2 SDK:**
```bash
ssh polytrade '
  sudo rm /etc/systemd/system/mvp-autopilot.service.d/shadow-mode.conf
  sudo systemctl daemon-reload
  sed -i "s/MVP_SHADOW_MODE=1/MVP_SHADOW_MODE=0/" /home/ubuntu/polymarket-mvp/.env
  sudo systemctl restart mvp-autopilot.timer
'
```

### Step 7 — Watch the first real-mode tick
```bash
ssh polytrade 'journalctl -u mvp-autopilot.service -f --since "5 min ago"'
```
Look for:
- A `[poly-executor] real preflight … collateral_balance_available=` line with a non-zero value
- No `insufficient_balance` decisions on conviction-tier proposals
- Eventually: a `status=submitted` or `status=live` execution row

## 2. Re-running migration on a new wallet

The script is wallet-agnostic — change `POLY_CLOB_SIGNER_KEY` + `POLY_CLOB_FUNDER` in `.env` (they must derive the same address) and run:
```bash
.venv/bin/python scripts/check_v2_state.py   # see what's missing
.venv/bin/python scripts/migrate_to_clob_v2.py --dry-run
.venv/bin/python scripts/migrate_to_clob_v2.py
```
Re-running on an already-migrated wallet prints `[skip]` for every step and exits 0 cleanly.

## 3. Rollback

### 3a. Service-level rollback (no on-chain impact)
Restore shadow mode immediately:
```bash
ssh polytrade '
  echo -e "[Service]\nEnvironment=MVP_SHADOW_MODE=1" | \
    sudo tee /etc/systemd/system/mvp-autopilot.service.d/shadow-mode.conf > /dev/null
  sudo systemctl daemon-reload
  sudo systemctl restart mvp-autopilot.timer
'
```

### 3b. Code-level rollback (back to V1 SDK)
V1 CLOB is dead — but if the V2 swap itself broke something unrelated, you can revert the code:
```bash
git revert --no-edit <merge-commit-or-range>
git push origin feat/clob-v2-migration
ssh polytrade '
  cd /home/ubuntu/polymarket-mvp
  git pull
  .venv/bin/pip install -e .[real-exec]
  sudo systemctl restart mvp-autopilot.service
'
```
Note: V1 SDK can still be installed (`pip install py-clob-client==0.34.6`) but V1 production endpoints no longer accept orders, so this only helps if the autopilot is stuck in shadow mode anyway.

### 3c. On-chain unwrap (only if funds need to return to USDC.e form)
Not normally required — V1 trading is dead either way. If absolutely needed:
```python
# scripts/unwrap_pusd_to_usdc_e.py (write on demand)
# CollateralOfframp at 0x2957922Eb93258b93368531d39fAcCA3B4dC5854
# Call .unwrap(amount) with pUSD balance.
```

## 4. Health-check cheatsheet

```bash
# Bot wallet readiness
ssh polytrade '/home/ubuntu/polymarket-mvp/.venv/bin/python /home/ubuntu/polymarket-mvp/scripts/check_v2_state.py'

# Recent service ticks
ssh polytrade 'journalctl -u mvp-autopilot.service -n 20 --no-pager'

# Migration journal
ssh polytrade 'tail -50 /home/ubuntu/polymarket-mvp/var/clob_v2_migration.log'

# V2 SDK installed?
ssh polytrade '/home/ubuntu/polymarket-mvp/.venv/bin/pip show py-clob-client-v2'

# Shadow drop-in present (yes = shadow mode active)?
ssh polytrade 'ls -la /etc/systemd/system/mvp-autopilot.service.d/'
```

## 5. Version-pin policy

- Hold at `py-clob-client-v2==1.0.1` until Polymarket publishes a behavior-changing patch.
- Read CHANGELOG (or `pip index versions py-clob-client-v2`) before bumping.
- Re-run `pytest -q` + `scripts/check_v2_state.py --probe-sdk` after any bump.

## 6. Known caveats / nuances

- **`CollateralOnramp.wrap(uint256)` ABI assumption.** The migration script bakes a single-function ABI for `wrap(uint256 amount)`. If Polymarket renames or refactors this method, the on-chain tx will fail with an "ABI mismatch" / "function not found" error. In that case: pull the live ABI from Polygonscan for `0x93070a847efEf7F70739046A929D47a521F5B8ee` and update `ONRAMP_ABI` at [scripts/migrate_to_clob_v2.py](../scripts/migrate_to_clob_v2.py).
- **CLOB cache lag.** Between the wrap+approve broadcasts and the first CLOB API tick, there's a ~10s race window where `get_balance_allowance` may still return zero. The migration script calls `client.update_balance_allowance(COLLATERAL)` at the end to force a re-read; even so, give it 10-30s before the first real-mode tick.
- **MATIC floor.** The migration script refuses to start if the wallet has < 0.5 MATIC. Top up via bridge or a Polygon-native source.
- **`POLY_CLOB_FUNDER` must equal the signer key's derived address.** The migration script enforces this — if they diverge it refuses to broadcast, because under SIGNATURE_TYPE=0 (EOA) the two must match.
- **NegRiskAdapter approval is preserved across V1→V2.** No re-approval needed; redeems keep working.
- **Stranded V1 setApprovalForAll txns from 2026-05-12 are harmless.** The check script reports them under "Stranded V1 approvals" for transparency. No revocation needed.

## 7. Where each piece lives

| Concern | File / location |
|---|---|
| SDK swap (constructor / preflight / balance / order construction / get_order) | [src/polymarket_mvp/poly_executor.py](../src/polymarket_mvp/poly_executor.py) |
| Risk-engine balance lookup | [src/polymarket_mvp/risk_engine.py](../src/polymarket_mvp/risk_engine.py) |
| Ops snapshot balance display | [src/polymarket_mvp/ops_snapshot.py](../src/polymarket_mvp/ops_snapshot.py) |
| Reconciler `get_order` calls (uses executor's builder) | [src/polymarket_mvp/services/reconciler.py](../src/polymarket_mvp/services/reconciler.py) |
| Read-only diagnostic | [scripts/check_v2_state.py](../scripts/check_v2_state.py) |
| On-chain migration | [scripts/migrate_to_clob_v2.py](../scripts/migrate_to_clob_v2.py) |
| Regression test for V2 preflight | `tests/test_v03_v04_flow.py::ClobV2PreflightTests` |
| Dependency pin | [pyproject.toml](../pyproject.toml) `[project.optional-dependencies].real-exec` |
| Migration journal log | `var/clob_v2_migration.log` (gitignored) |
