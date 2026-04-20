-- v0.6 Observability migration
-- Adds: error_category, submitted_at, filled_at to executions
-- Adds: risk_block_reasons_json to proposals
-- Adds: execution_events audit table

ALTER TABLE executions ADD COLUMN error_category TEXT;
ALTER TABLE executions ADD COLUMN submitted_at TEXT;
ALTER TABLE executions ADD COLUMN filled_at TEXT;

ALTER TABLE proposals ADD COLUMN risk_block_reasons_json TEXT;

CREATE TABLE IF NOT EXISTS execution_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  execution_id INTEGER NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
  from_status TEXT,
  to_status TEXT NOT NULL,
  trigger TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_execution_events_execution ON execution_events(execution_id);
