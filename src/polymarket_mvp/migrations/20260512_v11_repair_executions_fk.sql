-- 2026-05-12: repair FK references corrupted by migration v10.
--
-- v10's `PRAGMA foreign_keys = OFF` at the top of the script was silently
-- ignored because Python's sqlite3 module had an active implicit
-- transaction running at the time the script was executed. With FK
-- enforcement still ON, the `ALTER TABLE executions RENAME TO
-- executions_old` step caused SQLite to update FK references in every
-- dependent table to point at `executions_old`. v10 then dropped
-- executions_old after copying data into the new executions table,
-- leaving 3 tables with dangling FK references.
--
-- The migration runner has been updated to set isolation_level=None for
-- the migration phase, so PRAGMA foreign_keys inside future scripts
-- actually takes effect. This migration rebuilds the 3 affected tables
-- with their FK references corrected.
--
-- On a fresh install where v10 worked correctly (because the runner fix
-- is in place), v11 still runs — it rebuilds the same tables with
-- equivalent schemas, so the net effect is a no-op identity rebuild.
-- Cheap and idempotent.

PRAGMA busy_timeout = 60000;
PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- positions ─────────────────────────────────────────────────────────
ALTER TABLE positions RENAME TO positions_v11_tmp;

CREATE TABLE positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id TEXT NOT NULL,
  execution_id INTEGER NOT NULL,
  market_id TEXT NOT NULL,
  event_cluster_id INTEGER,
  outcome TEXT NOT NULL,
  entry_price REAL,
  size_usdc REAL NOT NULL,
  filled_qty REAL,
  status TEXT NOT NULL CHECK(status IN ('open_requested', 'open', 'partially_filled', 'closing', 'closed', 'cancelled', 'resolved')),
  entry_time TEXT NOT NULL,
  last_mark_price REAL,
  unrealized_pnl REAL,
  realized_pnl REAL,
  strategy_name TEXT,
  is_shadow INTEGER NOT NULL DEFAULT 0,
  mode TEXT NOT NULL DEFAULT 'real' CHECK(mode IN ('mock', 'real', 'shadow')),
  redeemed_at TEXT,
  redeemed_tx_hash TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (execution_id, is_shadow),
  FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id) ON DELETE CASCADE,
  FOREIGN KEY (execution_id) REFERENCES executions(id) ON DELETE CASCADE,
  FOREIGN KEY (market_id) REFERENCES market_snapshots(market_id) ON DELETE CASCADE,
  FOREIGN KEY (event_cluster_id) REFERENCES event_clusters(id) ON DELETE SET NULL
);

INSERT INTO positions (
  id, proposal_id, execution_id, market_id, event_cluster_id, outcome,
  entry_price, size_usdc, filled_qty, status, entry_time, last_mark_price,
  unrealized_pnl, realized_pnl, strategy_name, is_shadow, mode,
  redeemed_at, redeemed_tx_hash, created_at, updated_at
)
SELECT
  id, proposal_id, execution_id, market_id, event_cluster_id, outcome,
  entry_price, size_usdc, filled_qty, status, entry_time, last_mark_price,
  unrealized_pnl, realized_pnl, strategy_name, is_shadow, mode,
  redeemed_at, redeemed_tx_hash, created_at, updated_at
FROM positions_v11_tmp;

DROP TABLE positions_v11_tmp;

CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_resolved_unredeemed
  ON positions(market_id, status, redeemed_at);

-- order_reconciliations ─────────────────────────────────────────────
ALTER TABLE order_reconciliations RENAME TO order_reconciliations_v11_tmp;

CREATE TABLE order_reconciliations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  execution_id INTEGER NOT NULL,
  external_order_id TEXT,
  observed_status TEXT,
  observed_fill_qty REAL,
  observed_fill_price REAL,
  reconciliation_result TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (execution_id) REFERENCES executions(id) ON DELETE CASCADE
);

INSERT INTO order_reconciliations (
  id, execution_id, external_order_id, observed_status, observed_fill_qty,
  observed_fill_price, reconciliation_result, payload_json, created_at
)
SELECT
  id, execution_id, external_order_id, observed_status, observed_fill_qty,
  observed_fill_price, reconciliation_result, payload_json, created_at
FROM order_reconciliations_v11_tmp;

DROP TABLE order_reconciliations_v11_tmp;

CREATE INDEX IF NOT EXISTS idx_order_reconciliations_execution
  ON order_reconciliations(execution_id);

-- execution_events ──────────────────────────────────────────────────
ALTER TABLE execution_events RENAME TO execution_events_v11_tmp;

CREATE TABLE execution_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  execution_id INTEGER NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
  from_status TEXT,
  to_status TEXT NOT NULL,
  trigger TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

INSERT INTO execution_events (
  id, execution_id, from_status, to_status, trigger, payload_json, created_at
)
SELECT
  id, execution_id, from_status, to_status, trigger, payload_json, created_at
FROM execution_events_v11_tmp;

DROP TABLE execution_events_v11_tmp;

CREATE INDEX IF NOT EXISTS idx_execution_events_execution
  ON execution_events(execution_id);

COMMIT;

PRAGMA foreign_keys = ON;
