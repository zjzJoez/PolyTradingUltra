# Polymarket Trading OS — Integrated Improvement Plan

> Sources: deep code audit, review artifacts (`artifacts/review-2026-04/`), trading system best practices research, community patterns from quant/crypto trading bot ecosystems.

## Current System Profile

```
Pipeline:  scanner -> proposer/LLM -> risk_engine -> tg_approver -> executor -> position_manager -> reconciler
DB:        SQLite WAL, single-writer discipline, 60s busy timeout
Loop:      autopilot.py — 8 cadenced loops in a single process, 1-second tick
History:   1545 proposals / 143 executions / 63 positions / +$8.25 realized PnL
Key loss:  20 duplicate_exposure incidents, 5 reconciliation_gaps, 5 execution_failure clusters
```

---

## TIER 1 — Execution Integrity (highest $ impact, lowest complexity)

### 1.1 Idempotency Key for Every Order Submission

**Problem**: The executor currently generates `client_order_id` as `{proposal_id}-{mode}`. If the process crashes between `post_order()` and `record_execution()`, a retry will submit a second order with the same intent but no way to match it back.

**Solution**: Generate a cryptographic idempotency key (`uuid4` or `proposal_id + attempt_number`) and pass it as the CLOB client order ID. On retry, the exchange either returns the existing order or rejects the duplicate.

**Where**: [poly_executor.py:516](src/polymarket_mvp/poly_executor.py#L516) — replace `client_order_id` generation.

```python
import uuid
idempotency_key = f"{record['proposal_id']}-{mode}-{uuid.uuid4().hex[:8]}"
# Store this BEFORE calling post_order() so crash recovery can find it
```

**Impact**: Eliminates the "unknown order state" problem that causes reconciliation_gap incidents.

### 1.2 Pre-Execution Write Fence

**Problem**: `execute_record()` does network calls (preflight, price fetch, order submit) while the caller holds a connection from `connect_db()`. If `post_order()` succeeds but the subsequent `record_execution()` call deadlocks or fails, the order exists on-exchange but not in the DB.

**Solution**: Insert a `pending_submission` sentinel row into `executions` BEFORE calling the exchange API. On success, update to `submitted`. On failure, update to `failed`. On crash recovery, any `pending_submission` row triggers a reconciliation lookup.

**Where**: [poly_executor.py:532-576](src/polymarket_mvp/poly_executor.py#L532-L576) — the real execution block.

```python
# Step 1: write sentinel
sentinel = record_execution(conn, {**execution, "status": "pending_submission"})
conn.commit()
# Step 2: submit to exchange
response = real_client.post_order(signed, OrderType.GTC)
# Step 3: update sentinel -> submitted/filled/failed
update_execution(conn, sentinel["id"], {"status": normalized_status, ...})
```

**Impact**: Closes the crash-window gap between exchange submission and DB write. This is the single most important safety improvement.

### 1.3 Handle "Unknown" Order State After post_order Failure

**Problem**: If `post_order()` succeeds on the exchange but the response is lost (timeout, connection reset), the code falls to the `except Exception` block and records a `failed` execution — but the order is live on the exchange. This is the most dangerous execution integrity gap.

**Solution**: After any `post_order` exception, attempt a single `get_order` lookup using the `client_order_id` before declaring failure:

```python
except Exception as exc:
    # Before declaring failure, check if the order actually went through
    try:
        order = real_client.get_order(idempotency_key)
        if order and _normalize_order_status(order.get("status")) in {"submitted", "live", "filled"}:
            execution["txhash_or_order_id"] = str(order.get("id") or "")
            execution["status"] = _normalize_order_status(order.get("status"))
            # ... update execution with real data
            return execution
    except Exception:
        pass  # truly unknown — record as failed, reconciler will catch it
    return _failed_execution(record, mode, f"order_submit_failed: {exc}")
```

**Where**: [poly_executor.py:563-575](src/polymarket_mvp/poly_executor.py#L563-L575)

### 1.4 Reconciler: Detect Position Drift and Heal Orphaned Submissions

**Problem**: `reconcile_live_orders()` only processes `submitted`/`live` executions. It never looks for `pending_submission` sentinels or for proposals that are `authorized_for_execution` but have no execution record despite being old.

**Solution**: Add a `heal_orphaned_submissions()` pass:
1. Find `pending_submission` executions older than 60s — query exchange by client_order_id
2. Find `authorized_for_execution` proposals older than 5min with no execution — flag as stale
3. Run this at the start of every reconcile tick

**Where**: [reconciler.py](src/polymarket_mvp/services/reconciler.py) — new function, called from `_loop_reconcile`.

### 1.5 Reconciler: Periodic Full Position Drift Detection

**Problem**: `reconcile_live_orders()` only polls orders the DB knows about. If an order exists on the exchange but not in the DB (e.g., write fence failure, or fill confirmation lost), it's invisible.

**Solution**: Add a periodic full position snapshot from the exchange, diff against DB, log discrepancies:

```python
def detect_position_drift(conn):
    """Compare exchange positions against DB positions. Log any drift."""
    client = _build_clob_client()
    exchange_positions = client.get_positions()  # or equivalent API
    db_positions = list_positions(conn, statuses=["open", "open_requested", "partially_filled"])
    # Diff and log
    for pos in exchange_positions:
        if pos not in db_known:
            record_order_reconciliation(conn, {
                "reconciliation_result": "drift_detected",
                "payload_json": {"exchange_position": pos, "source": "full_sync"},
            })
```

Run once per minute in `_loop_reconcile`. Even if you can't auto-fix, surfacing drift is critical.

**Where**: [reconciler.py](src/polymarket_mvp/services/reconciler.py) — new function.

### 1.6 Execution Failure Structured Categories

**Problem**: Failures are stored as free-text `error_message` strings. The review found 5 failure clusters but had to parse raw strings to classify them.

**Solution**: Add an `error_category` column to `executions` with an enum:
- `service_not_ready` — CLOB client construction failed
- `insufficient_balance` — preflight balance check
- `slippage_exceeded` — price moved too far
- `order_submit_failed` — exchange rejected the order
- `request_exception` — network timeout/reset
- `unknown` — catch-all

**Where**: [poly_executor.py `_failed_execution()`](src/polymarket_mvp/poly_executor.py#L356) — add `error_category` field. Schema migration to add the column.

---

## TIER 2 — State Machine Hardening

### 2.1 Formal Proposal State Machine with Transition Validation

**Problem**: Proposal status updates happen via `update_proposal_workflow_fields()` and `update_proposal_status()` with no validation of legal transitions. It's possible to go from `executed` back to `proposed` if code paths are wrong.

**Solution**: Define an explicit transition map and enforce it at the DB layer:

```python
LEGAL_TRANSITIONS = {
    "proposed":                    {"risk_blocked", "pending_approval"},
    "risk_blocked":                {"proposed"},  # re-evaluation only
    "pending_approval":            {"approved", "rejected", "expired", "authorized_for_execution"},
    "approved":                    {"authorized_for_execution", "executed", "failed"},
    "authorized_for_execution":    {"executed", "failed"},
    "executed":                    set(),  # terminal
    "failed":                      {"proposed"},  # explicit retry only
    "expired":                     set(),  # terminal
    "rejected":                    set(),  # terminal
}

def update_proposal_status(conn, proposal_id, new_status):
    current = conn.execute("SELECT status FROM proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
    if current and new_status not in LEGAL_TRANSITIONS.get(current["status"], set()):
        raise ValueError(f"Illegal transition: {current['status']} -> {new_status}")
    ...
```

**Where**: [db.py `update_proposal_status()`](src/polymarket_mvp/db.py) — wrap existing function.

### 2.2 Position Terminal-State Assertions

**Problem**: It's possible for a position to be `open` while its execution is `failed`, or `open_requested` while the market is resolved.

**Solution**: Add a periodic assertion sweep (run at end of reconcile tick):
```sql
-- Positions that should not exist
SELECT * FROM positions p
WHERE p.status IN ('open', 'open_requested')
  AND (
    EXISTS (SELECT 1 FROM executions e WHERE e.id = p.execution_id AND e.status = 'failed')
    OR EXISTS (SELECT 1 FROM market_resolutions mr WHERE mr.market_id = p.market_id)
  )
```
If found, auto-repair and emit a structured alert.

**Where**: [reconciler.py](src/polymarket_mvp/services/reconciler.py) — new `assert_position_consistency()`.

### 2.3 Execution-Position Atomic Pairing

**Problem**: `record_execution()` in `db.py` calls `_sync_position_for_execution()` as a side effect. If the position sync fails, the execution is committed but the position is missing.

**Solution**: Make position creation transactional with execution recording. If `_sync_position_for_execution` raises, roll back the execution insert too.

**Where**: [db.py:785-798](src/polymarket_mvp/db.py#L785-L798)

---

## TIER 3 — Risk Architecture Upgrades

### 3.1 Market-Class Risk Config Layer

**Problem**: All markets share the same risk parameters. `crypto_up_down` lost $28.67 while `sports_winner` earned $34.07.

**Solution**: Define per-class config:

```python
MARKET_CLASS_CONFIG = {
    "sports_winner":  {"live_enabled": True,  "max_order_usdc": 10, "max_daily_gross": 50, "max_open_positions": 5},
    "sports_totals":  {"live_enabled": True,  "max_order_usdc": 5,  "max_daily_gross": 25, "max_open_positions": 3},
    "esports":        {"live_enabled": True,  "max_order_usdc": 5,  "max_daily_gross": 25, "max_open_positions": 3},
    "crypto_up_down": {"live_enabled": False, "max_order_usdc": 0,  "max_daily_gross": 0,  "max_open_positions": 0},
    "other":          {"live_enabled": False, "max_order_usdc": 0,  "max_daily_gross": 0,  "max_open_positions": 0},
}
```

Lookup the class using `event_cluster_service.market_type_for()` and apply limits in `evaluate_portfolio_risk()`.

**Where**: [portfolio_risk_service.py](src/polymarket_mvp/services/portfolio_risk_service.py), [risk_engine.py](src/polymarket_mvp/risk_engine.py)

### 3.2 Drawdown Circuit Breaker

**Problem**: No portfolio-level drawdown protection. On 2026-04-07 the system lost $49.18 in a single day.

**Solution**: Track rolling realized+unrealized PnL. If drawdown from peak exceeds a threshold (e.g., $30), automatically activate a time-limited global kill switch:

```python
def check_drawdown_breaker(conn):
    # Sum realized_pnl from resolved positions + unrealized from open
    peak = ... # rolling peak from position_events
    current = ...
    drawdown = peak - current
    if drawdown > get_env_float("POLY_MAX_DRAWDOWN_USDC", 30.0):
        set_kill_switch(conn, scope_type="global", scope_key="drawdown_breaker",
                       reason=f"drawdown={drawdown:.2f} exceeded limit")
```

**Where**: New function in [portfolio_risk_service.py](src/polymarket_mvp/services/portfolio_risk_service.py), called from `_loop_reconcile`.

### 3.3 Daily Gross Exposure Cap (Portfolio-Level)

**Problem**: On 2026-04-07, gross exposure reached $255 with 51 executions. No portfolio-level daily cap exists.

**Solution**: Add `POLY_RISK_MAX_DAILY_GROSS_USDC` (default: $100) checked in `evaluate_portfolio_risk()`:

```sql
SELECT COALESCE(SUM(e.requested_size_usdc), 0)
FROM executions e
WHERE substr(e.created_at, 1, 10) = ?
  AND e.status NOT IN ('failed')
```

### 3.4 Position Count Cap

**Problem**: No limit on total open positions. System could accumulate unbounded risk.

**Solution**: Add `POLY_RISK_MAX_OPEN_POSITIONS` (default: 10). Check in `evaluate_portfolio_risk()`:

```python
open_count = conn.execute(
    "SELECT COUNT(*) FROM positions WHERE status IN ('open', 'open_requested', 'partially_filled')"
).fetchone()[0]
if open_count >= max_open_positions:
    reasons.append("max_open_positions_reached")
```

---

## TIER 4 — Observability and Attribution

### 4.1 Risk Decision Persistence

**Problem**: 1199 risk_blocked proposals exist but the blocking reason is not stored.

**Solution**: Add `risk_block_reasons_json` column to `proposals`. Populate it in `evaluate_full_record()` before calling `update_proposal_workflow_fields()`.

**Where**: [risk_engine.py:198-205](src/polymarket_mvp/risk_engine.py#L198-L205)

### 4.2 Proposal-Time Feature Snapshot

**Problem**: Cannot replay the decision context at proposal time.

**Solution**: When persisting a proposal, also persist a compact feature snapshot:

```python
{
    "snapshot_price": 0.62,
    "clob_price": 0.63,
    "liquidity_usdc": 100000,
    "spread_bps": 150,
    "seconds_to_expiry": 3600,
    "open_positions_count": 3,
    "daily_gross_usdc": 45.0,
}
```

Store in `context_payload_json` or a new `proposal_features_json` column.

### 4.3 Loop Lag Alert (Emergency Detection)

**Problem**: If the `reconcile` loop stalls (network hang, deadlock), there's no alert. Stale orders accumulate, positions drift.

**Solution**: In `_tick()`, check if the loop is overdue by 3x its cadence. If so, log a CRITICAL warning and optionally send a Telegram alert:

```python
def _tick(self, conn, name: str) -> None:
    lag = time.time() - self.last_run.get(name, 0)
    if lag > 3 * self.cadences[name]:
        _log(f"CRITICAL: {name} loop lagging {lag:.0f}s (cadence={self.cadences[name]}s)")
        # Optional: send Telegram alert
    ...
```

**Where**: [autopilot.py:96-112](src/polymarket_mvp/autopilot.py#L96-L112)

### 4.4 Structured Heartbeat Metrics

**Problem**: Heartbeat records exist but don't capture latency, queue depth, or error rate.

**Solution**: Enrich `record_heartbeat()` with structured metrics:

```python
record_heartbeat(conn, name, started, ended, count, error_msg,
    metrics_json={
        "duration_ms": (ended - started) * 1000,
        "queue_depth": len(authorized),
        "error_count": error_count,
        "positions_open": open_position_count,
    })
```

---

## TIER 5 — Code-Level Quality and Testing

### 5.1 Property-Based Testing for State Machine

**Problem**: Current tests are example-based. They cover specific paths but miss edge case combinations.

**Solution**: Use `hypothesis` to generate random sequences of proposal/execution/position state transitions and verify invariants:

```python
from hypothesis import given, strategies as st

@given(st.lists(st.sampled_from(["propose", "risk_block", "approve", "execute", "fail", "resolve"])))
def test_state_machine_invariants(actions):
    # Run actions against a fresh DB
    # Assert: no proposal in terminal state has a non-terminal position
    # Assert: no executed proposal has zero execution records
    # Assert: every position has a valid execution parent
```

### 5.2 Chaos Testing for Exchange API

**Problem**: No tests simulate network failures during order submission.

**Solution**: Create a `FlakyMockClient` that randomly fails on `post_order()`, `get_order()`, etc. Run the full autopilot loop against it and verify no state corruption occurs.

### 5.3 Replay Testing from Production Data

**Problem**: Tests use synthetic data. Real production patterns (timing, market shapes, failure modes) aren't captured.

**Solution**: Export the `review_snapshot.sqlite3` as test fixtures. Replay the exact proposal sequence through the system with the new guards enabled and verify zero duplicate exposure.

### 5.4 Integration Test: Full Autopilot Tick

**Problem**: Individual components are tested but the full `autopilot._tick()` flow is only tested with `max_iterations=1` in `test_autopilot_single_tick`.

**Solution**: Add a multi-tick test that runs scan->propose->risk->approve->execute->reconcile->resolve and verifies the complete lifecycle.

---

## TIER 6 — Architecture Evolution (medium-term)

### 6.1 Separate Writer Process for Execution

**Problem**: The autopilot loop does everything — scanning, proposing, executing, reconciling — in one process. Long network calls during execution block the reconcile loop.

**Solution**: Split into two processes:
1. **Supervisor** (scan, propose, risk, approve, review) — runs every 10-30s
2. **Executor** (execute, reconcile, redeem) — runs every 5-10s

Both use the same SQLite DB with WAL mode. The `authorized_for_execution` status is the handoff point.

### 6.2 Event Sourcing for Positions

**Problem**: Position state is mutable (UPDATE in place). History is partially captured via `position_events`, but the position table itself is the source of truth.

**Solution**: Make `position_events` the source of truth. Compute current position state by replaying events. This makes it impossible to have state drift — the event log is append-only and the derived state is always recomputable.

### 6.3 SQLite Transaction Discipline Fixes (from best practices research)

**6.3a. Use `BEGIN IMMEDIATE` for write transactions (CRITICAL)**

The most dangerous SQLite pattern for a trading system: `connect_db()` uses the default `BEGIN` (deferred mode), which means a read-then-write transaction can get `SQLITE_BUSY` *without triggering the busy timeout handler*. This is likely the root cause of the 56 `ops_lock` heartbeat incidents.

Fix: For any operation that will write (execution recording, position sync, proposal updates), use `BEGIN IMMEDIATE` so the lock is acquired upfront and `busy_timeout` actually applies:

```python
conn.execute("BEGIN IMMEDIATE")
# ... write operations ...
conn.commit()
```

Or better: add a `connect_db_writer()` helper that automatically uses IMMEDIATE mode.

Ref: [SQLite BUSY despite timeout](https://berthub.eu/articles/posts/a-brief-post-on-sqlite3-database-locked-despite-timeout/)

**6.3b. Set `PRAGMA synchronous = NORMAL`**

In WAL mode this is safe and significantly faster (SQLite's own docs recommend it). Currently using default `FULL`, which forces an fsync on every commit. Add to `connect_db()`:

```python
conn.execute("PRAGMA synchronous = NORMAL")
```

**6.3c. UNIQUE constraint on `client_order_id`**

`client_order_id` is generated but not enforced at the DB level. Add a UNIQUE constraint so crash-and-retry cannot double-insert. Use `INSERT OR IGNORE` or catch `IntegrityError` and return the existing row.

**6.3d. Execution Events Table (append-only audit trail)**

Currently execution status is overwritten in place. Add an `execution_events` table that logs every state transition:

```sql
CREATE TABLE execution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id INTEGER NOT NULL REFERENCES executions(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    trigger TEXT,  -- 'submit', 'reconcile', 'cancel', 'fill'
    payload_json TEXT,
    created_at TEXT NOT NULL
);
```

This gives post-mortem forensics without losing intermediate states.

### 6.4 SQLite -> PostgreSQL Migration Path

**Problem**: SQLite's single-writer model becomes a bottleneck as execution volume grows.

**Timing**: Not needed now (143 executions over 2 weeks), but plan the abstraction layer:
- Keep all SQL in `db.py` (already done)
- Use parameterized queries (already done)
- Avoid SQLite-specific syntax (`last_insert_rowid()`, `PRAGMA`)
- Add a `DB_BACKEND` env var that selects connection factory

### 6.4 Backtesting Harness

**Problem**: No way to test strategy changes against historical data without live deployment.

**Solution**: Build a lightweight backtester that:
1. Reads market_snapshots in chronological order
2. Feeds them through the proposal pipeline with a mock executor
3. Simulates fills at snapshot prices
4. Computes PnL, drawdown, win rate by market class

This would catch the `crypto_up_down` problem before it costs real money.

---

## Implementation Priority Matrix

| # | Item | Effort | $ Impact | Risk Reduction | Phase |
|---|------|--------|----------|----------------|-------|
| 1 | 6.3a BEGIN IMMEDIATE for writes | S | Critical | Critical | v0.6 P1 |
| 2 | 7.1 Kill switch race condition fix | S | Critical | Critical | v0.6 P1 |
| 3 | 1.2 Pre-execution write fence | M | Critical | Critical | v0.6 P1 |
| 4 | 7.3 Decouple position sync from exec | S | High | Critical | v0.6 P1 |
| 5 | 1.1 Idempotency key + UNIQUE constraint | S | High | High | v0.6 P1 |
| 6 | 1.3 Unknown order state recovery | S | High | High | v0.6 P1 |
| 7 | 3.2 Drawdown circuit breaker | S | High | Critical | v0.6 P2 |
| 8 | 6.3b PRAGMA synchronous=NORMAL | S | Low | Medium | v0.6 P1 |
| 9 | 3.1 Market-class risk config | M | High | High | v0.6 P3 |
| 10 | 7.4 Classify permanent vs transient errors | S | Medium | High | v0.6 P2 |
| 11 | 2.1 Proposal state machine validation | S | Medium | High | v0.6 P1 |
| 12 | 3.3 Daily gross cap | S | Medium | High | v0.6 P2 |
| 13 | 3.4 Position count cap | S | Medium | Medium | v0.6 P2 |
| 14 | 2.2 Position consistency assertions | S | Medium | High | v0.6 P2 |
| 15 | 4.3 Loop lag alert | S | Medium | High | v0.6 P2 |
| 16 | 1.5 Position drift detection | M | High | High | v0.6 P2 |
| 17 | 1.6 Structured error categories | S | Low | Medium | v0.6 P2 |
| 18 | 6.3c UNIQUE(client_order_id) constraint | S | Medium | High | v0.6 P1 |
| 19 | 6.3d Execution events audit table | M | Low | Medium | v0.6 P4 |
| 20 | 4.1 Risk decision persistence | S | Medium | Medium | v0.6 P4 |
| 21 | 7.5 Correlation/trace ID | M | Low | Medium | v0.6 P4 |
| 22 | 7.2 Batch proposal_record() queries | M | Low | Medium | v0.7 |
| 23 | 5.1 Property-based state tests | M | Low | High | v0.6 P1 |
| 24 | 5.3 Replay testing | M | Medium | Medium | v0.6 P4 |
| 25 | 8.1 Enforce max_daily_loss_usdc | S | Critical | Critical | v0.6 P2 |
| 26 | 8.3 Order submission timeout | S | Critical | Critical | v0.6 P1 |
| 27 | 8.2 Enforce max_open_positions | S | High | High | v0.6 P2 |
| 28 | 8.4 Check approval expiry at execution | S | Medium | High | v0.6 P1 |
| 29 | 8.5 Execution latency timestamps | S | Low | Medium | v0.6 P4 |
| 30 | 8.6 Retry order cancellation | S | Medium | Medium | v0.6 P2 |
| 31 | 6.1 Separate executor process | L | Medium | Medium | v0.7 |
| 32 | 6.5 Backtesting harness | L | High | Medium | v0.7 |

**S** = small (<2h), **M** = medium (2-8h), **L** = large (1-3d)

### Recommended Sprint Batches

**Sprint 1 — Execution Safety (1 day)**: Items 1-6, 8, 11, 18, 26, 28
- `BEGIN IMMEDIATE`, kill switch fix, write fence, idempotency key, unknown order recovery
- Order submission timeout, approval expiry check at execution
- These make the system crash-safe and eliminate the "unknown state" class of bugs

**Sprint 2 — Risk Controls (1 day)**: Items 7, 10, 12-15, 25, 27, 30
- Drawdown circuit breaker, daily gross cap, position count cap
- **Enforce `max_daily_loss_usdc` and `max_open_positions`** (already in schema, never checked!)
- Retry order cancellation, classify permanent vs transient errors
- This is where you stop the $49/day loss scenarios

**Sprint 3 — Market Segmentation (2 days)**: Item 9 + item 16
- Per-class risk config, disable `crypto_up_down` live
- Position drift detection from exchange
- This is where you stop trading the worst category entirely

**Sprint 4 — Observability (1 day)**: Items 17, 19-21, 24, 29
- Structured error categories, execution events audit table, risk decision persistence
- Execution latency timestamps, correlation IDs, replay testing
- Makes every future debugging session 10x faster

---

---

## TIER 7 — Concurrency and Race Condition Fixes (from deep code audit)

### 7.1 Kill Switch Check Races with Loop Execution (CRITICAL)

**Problem**: In `autopilot.py:76-88`, the global kill switch is checked on one connection (`conn` at line 76), but each loop tick opens a NEW connection (line 86). Between the kill switch check and the loop execution, the switch could activate — and the loop proceeds on a different connection that never saw the update.

**Fix**: Move the kill switch check INSIDE each `_tick()` call, using the same connection:

```python
def _tick(self, conn, name: str) -> None:
    if _global_kill_switch_active(conn):
        _log(f"{name} skipped: global kill switch active")
        return
    # ... existing tick logic
```

**Where**: [autopilot.py:96-112](src/polymarket_mvp/autopilot.py#L96-L112)

### 7.2 proposal_record() Is Not Atomic (HIGH)

**Problem**: `proposal_record()` in `db.py:483-506` runs 6+ separate SELECT queries (proposal → market → contexts → cluster → memo → approval) without atomicity. In `list_proposals_by_status()` (line 528), this is called in a loop — 25 proposals × 6 queries = 150+ queries, creating a long read window that blocks reconciler writes.

**Fix**: Batch the lookups into fewer queries, or at minimum document that `proposal_record()` is only safe when called within a single-writer context. For the hot path (`_loop_execute`), pre-filter to only fetch proposals that actually need execution.

**Where**: [db.py:483-528](src/polymarket_mvp/db.py#L483-L528)

### 7.3 Position Sync Failure Rolls Back Execution (HIGH)

**Problem**: In `record_execution()` at `db.py:796-797`, `_sync_position_for_execution()` is called as a side effect inside the same transaction. If position sync raises, the entire execution INSERT is lost — even though the order is already live on the exchange.

**Fix**: Isolate position sync from execution recording. Commit the execution first, then sync positions in a separate try/except:

```python
row = conn.execute("SELECT * FROM executions WHERE rowid = last_insert_rowid()").fetchone()
result = row_to_dict(row) or {}
# Commit execution record FIRST
conn.commit()
# Then sync position (failure here is recoverable by reconciler)
if result.get("id") and result.get("status") in {"filled", "submitted", "live"}:
    try:
        _sync_position_for_execution(conn, int(result["id"]))
    except Exception:
        pass  # reconciler will heal this
```

**Where**: [db.py:785-798](src/polymarket_mvp/db.py#L785-L798)

### 7.4 Retry Logic Doesn't Classify Permanent vs Transient Failures

**Problem**: `_looks_retryable_request_error()` in `poly_executor.py:31-46` retries on ANY `requests.RequestException`, including 404 (not found) and 400 (bad request). This wastes 5+ seconds on guaranteed failures.

**Fix**: Add HTTP status code classification:

```python
def _looks_retryable_request_error(exc: Exception) -> bool:
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response else None
        if status and status < 500:
            return False  # 4xx errors are permanent
    if isinstance(exc, requests.ConnectionError):
        return True
    if isinstance(exc, requests.Timeout):
        return True
    # ... existing text-based checks for non-HTTP errors
```

**Where**: [poly_executor.py:31-46](src/polymarket_mvp/poly_executor.py#L31-L46)

### 7.5 Add Correlation ID Across the Trade Lifecycle

**Problem**: No way to trace a single trade through proposal → execution → position → reconciliation. Debug requires manual SQL joins.

**Fix**: Generate a `trace_id` at proposal creation time. Propagate it through execution and position records:

```python
trace_id = f"t-{uuid.uuid4().hex[:12]}"
# Stored in proposals, executions, positions, position_events
```

This enables: `SELECT * FROM position_events WHERE trace_id = 't-abc123' ORDER BY id` to see the full lifecycle.

**Where**: Schema migration + [db.py](src/polymarket_mvp/db.py) upsert functions.

---

## TIER 8 — Gaps Found in Schema-vs-Code Audit

### 8.1 Daily Loss Limit Is Defined in Schema But Never Enforced (CRITICAL)

**Problem**: `strategy_authorizations` table has a `max_daily_loss_usdc` column (schema.sql:201-220). But `evaluate_authorization()` in [authorization_service.py:15-58](src/polymarket_mvp/services/authorization_service.py#L15-L58) **never checks it**. The system can accumulate unlimited daily losses.

**Solution**: Calculate realized P&L from resolved positions for today and block if daily loss exceeds the authorization limit:

```python
def _daily_realized_loss(conn, strategy_name: str) -> float:
    today = parse_iso8601(utc_now_iso()).date().isoformat()
    row = conn.execute("""
        SELECT COALESCE(SUM(CASE WHEN p.realized_pnl < 0 THEN p.realized_pnl ELSE 0 END), 0)
        FROM positions p
        WHERE p.strategy_name = ? AND substr(p.updated_at, 1, 10) = ? AND p.status = 'resolved'
    """, (strategy_name, today)).fetchone()
    return abs(float(row[0]))
```

Check in `evaluate_authorization()` before returning `matched_auto_execute`.

### 8.2 Max Open Positions Defined But Never Enforced (HIGH)

**Problem**: `strategy_authorizations.max_open_positions` exists in schema but `evaluate_authorization()` never checks it. Combined with no portfolio-level position cap (item 3.4), unbounded position accumulation is possible.

**Solution**: Query open positions for the strategy and compare against the authorization limit:

```python
open_count = conn.execute("""
    SELECT COUNT(*) FROM positions
    WHERE strategy_name = ? AND status IN ('open', 'open_requested', 'partially_filled')
""", (auth["strategy_name"],)).fetchone()[0]
if open_count >= int(auth["max_open_positions"]):
    result["authorization_status"] = "position_limit_reached"
    result["reason"] = f"open_positions={open_count} >= max={auth['max_open_positions']}"
    return result
```

**Where**: [authorization_service.py:28-57](src/polymarket_mvp/services/authorization_service.py#L28-L57)

### 8.3 No Timeout on Order Submission (CRITICAL)

**Problem**: `client.post_order()` at [poly_executor.py:567](src/polymarket_mvp/poly_executor.py#L567) has **no explicit timeout**. If the Polymarket API hangs, the operation blocks forever. The autopilot's 10s execute cadence will pile up stale operations.

**Solution**: Wrap the order submission in a timeout. Since `py-clob-client` doesn't expose a timeout parameter, use a signal-based or thread-based timeout:

```python
import signal

def _timeout_handler(signum, frame):
    raise TimeoutError("Order submission timed out")

signal.signal(signal.SIGALRM, _timeout_handler)
signal.alarm(30)  # 30-second hard limit
try:
    response = real_client.post_order(signed, OrderType.GTC)
finally:
    signal.alarm(0)
```

Or use `concurrent.futures.ThreadPoolExecutor` with a timeout for cross-platform support.

### 8.4 Approval Expiry Not Checked at Execution Time

**Problem**: `execute_record()` at [poly_executor.py:386-388](src/polymarket_mvp/poly_executor.py#L386-L388) checks `approval.decision == "approved"` but ignores `approval_expires_at`. A stale approval (expired but not yet cleaned by the expiry loop) will execute.

**Solution**: Add an expiry check:

```python
approval = record.get("approval") or {}
if approval.get("expires_at"):
    if parse_iso8601(approval["expires_at"]) < parse_iso8601(utc_now_iso()):
        return _failed_execution(record, mode, "approval_expired")
```

### 8.5 No Execution Latency Tracking

**Problem**: `executions` table has `created_at` and `updated_at` but no `submitted_at` or `filled_at`. Cannot measure market impact, fill time, or API latency.

**Solution**: Add `submitted_at` and `filled_at` columns. Set `submitted_at` when the order is posted, `filled_at` when reconciler detects fill. Enables latency analysis:

```sql
ALTER TABLE executions ADD COLUMN submitted_at TEXT;
ALTER TABLE executions ADD COLUMN filled_at TEXT;
```

### 8.6 No Retry on Order Cancellation in Reconciler

**Problem**: `client.cancel(order_id)` in [reconciler.py:80](src/polymarket_mvp/services/reconciler.py#L80) has no retry. If the cancel API call fails (network timeout), the order stays live on the exchange but is marked as failed in the DB.

**Solution**: Wrap cancel in a 2-attempt retry:

```python
for attempt in range(2):
    try:
        client.cancel(order_id)
        break
    except Exception:
        if attempt == 0:
            time.sleep(1)
```

---

## Key Insights

**From code audit**: The biggest hidden risk isn't duplicate exposure (now fixed) — it's the **kill switch race condition** (7.1) and the **position sync coupling** (7.3). Both can cause the system to proceed with live orders while safety mechanisms think they're in control.

**From architecture analysis**: Every side-effecting operation needs a write-ahead record and a reconciliation path. The pre-execution write fence (1.2) and idempotency key (1.1) are the architectural foundation. The kill switch fix (7.1) ensures the safety layer actually works.

**From performance data**: The system isn't "bad at alpha" — it's mechanically amplifying losses through duplicate exposure ($28.67 lost on crypto_up_down alone from repeat entries). The v0.6 guards we shipped today directly address this. The next biggest lever is market-class segmentation (3.1) to stop trading the worst category entirely.
