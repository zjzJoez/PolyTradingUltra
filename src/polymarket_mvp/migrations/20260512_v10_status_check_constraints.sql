-- 2026-05-12: tighten CHECK constraints on executions.status and
-- position_events.event_type. Both columns previously accepted any string,
-- which let typos and bugs leak through and froze production state
-- (position 1593's INVALID-leak cascade lived in this gap for 20 days).
--
-- SQLite cannot ALTER a CHECK on an existing column, so each table is
-- rebuilt by RENAME→CREATE→INSERT...SELECT→DROP. FKs are toggled off
-- outside the transaction (PRAGMA foreign_keys is silently ignored
-- inside BEGIN/COMMIT) so child rows survive the rebuild.
--
-- Current production data has been audited: executions.status only
-- contains {failed, shadow_simulated, filled}; position_events.event_type
-- only contains {reconcile, open, resolve}. The new CHECK enums are
-- strict supersets of those, so INSERT...SELECT will not reject any row.

-- Default busy_timeout is 0 — if anything (alpha-lab on the same host, a
-- slow VACUUM, a manual ad-hoc SQL session) holds a write lock when
-- autopilot starts up and this migration kicks off, BEGIN TRANSACTION
-- crashes the service. 60s wait beats a crash loop.
PRAGMA busy_timeout = 60000;

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- ── executions ────────────────────────────────────────────────────────
-- New CHECK matches the actual status transitions in the code:
--   submitted → live → filled
--   submitted → live → failed
--   shadow_simulated (terminal, shadow mode only)
-- Anything else (e.g. the literal "invalid" that leaked through pre-fix
-- normalization, or legacy values like 'canceled_market_resolved' from
-- versions before the current state machine) is now rejected at the
-- column level. Self-heal stale data first so the rebuild's INSERT
-- doesn't trip the new CHECK on rows the code can't produce anymore.
UPDATE executions
SET status = 'failed',
    updated_at = COALESCE(updated_at, datetime('now')),
    error_message = COALESCE(
      NULLIF(error_message, ''),
      'migration_v10: status ' || status || ' coerced to failed (no longer recognized)'
    )
WHERE status NOT IN ('submitted', 'live', 'filled', 'failed', 'shadow_simulated');

ALTER TABLE executions RENAME TO executions_old;

CREATE TABLE executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('mock', 'real')),
  client_order_id TEXT,
  order_intent_json TEXT NOT NULL,
  requested_price REAL,
  requested_size_usdc REAL NOT NULL,
  max_slippage_bps INTEGER NOT NULL,
  observed_worst_price REAL,
  slippage_check_status TEXT NOT NULL CHECK(slippage_check_status IN ('passed', 'failed', 'skipped')),
  status TEXT NOT NULL CHECK(status IN ('submitted', 'live', 'filled', 'failed', 'shadow_simulated')),
  filled_size_usdc REAL,
  avg_fill_price REAL,
  txhash_or_order_id TEXT,
  slippage_bps REAL,
  error_message TEXT,
  error_category TEXT,
  submitted_at TEXT,
  filled_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id) ON DELETE CASCADE
);

-- Explicit column list: the old executions table has error_category /
-- submitted_at / filled_at AFTER created_at / updated_at (they were added
-- by later ALTERs), while the rebuilt schema puts them before. SELECT *
-- would map columns by position and silently shuffle data into the wrong
-- fields.
INSERT INTO executions (
  id, proposal_id, mode, client_order_id, order_intent_json,
  requested_price, requested_size_usdc, max_slippage_bps,
  observed_worst_price, slippage_check_status, status,
  filled_size_usdc, avg_fill_price, txhash_or_order_id, slippage_bps,
  error_message, error_category, submitted_at, filled_at,
  created_at, updated_at
)
SELECT
  id, proposal_id, mode, client_order_id, order_intent_json,
  requested_price, requested_size_usdc, max_slippage_bps,
  observed_worst_price, slippage_check_status, status,
  filled_size_usdc, avg_fill_price, txhash_or_order_id, slippage_bps,
  error_message, error_category, submitted_at, filled_at,
  created_at, updated_at
FROM executions_old;
DROP TABLE executions_old;

CREATE INDEX IF NOT EXISTS idx_executions_proposal ON executions(proposal_id);

-- ── position_events ───────────────────────────────────────────────────
-- 'redeem' was added by migration 20260322 but the column-level CHECK was
-- never actually loosened on live deployments (the migration was written
-- as if it would create the table fresh; existing installs kept the old
-- enum). _mark_positions_redeemed has been silently rolling back its
-- writes ever since. With this rebuild and the redeemer now updating
-- positions.redeemed_* directly, the gap is closed both ways.

-- Same self-heal for any historical event_type drift. Unknown values get
-- coerced to 'reconcile' (the most neutral / least-stateful entry).
UPDATE position_events
SET event_type = 'reconcile',
    payload_json = CASE
      WHEN payload_json LIKE '{%}' THEN
        substr(payload_json, 1, length(payload_json) - 1)
          || ', "_migration_v10_coerced_from": "' || event_type || '"}'
      ELSE payload_json
    END
WHERE event_type NOT IN ('open', 'mark_update', 'reduce', 'close', 'stop', 'resolve', 'reconcile', 'redeem');

ALTER TABLE position_events RENAME TO position_events_old;

CREATE TABLE position_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN ('open', 'mark_update', 'reduce', 'close', 'stop', 'resolve', 'reconcile', 'redeem')),
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (position_id) REFERENCES positions(id) ON DELETE CASCADE
);

-- Explicit columns for the same reason as executions — if some later
-- migration adds a column here, SELECT * would silently break.
INSERT INTO position_events (id, position_id, event_type, payload_json, created_at)
SELECT id, position_id, event_type, payload_json, created_at
FROM position_events_old;
DROP TABLE position_events_old;

CREATE INDEX IF NOT EXISTS idx_position_events_position ON position_events(position_id);

COMMIT;

PRAGMA foreign_keys = ON;
