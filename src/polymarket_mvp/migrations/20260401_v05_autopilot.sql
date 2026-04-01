-- v0.5 autopilot: proposal TTL/expiry fields, exit proposal support, heartbeats

ALTER TABLE proposals ADD COLUMN proposal_kind TEXT NOT NULL DEFAULT 'entry'
  CHECK(proposal_kind IN ('entry', 'exit'));

ALTER TABLE proposals ADD COLUMN target_position_id INTEGER REFERENCES positions(id);

ALTER TABLE proposals ADD COLUMN approval_ttl_seconds INTEGER;

ALTER TABLE proposals ADD COLUMN order_live_ttl_seconds INTEGER;

ALTER TABLE proposals ADD COLUMN approval_requested_at TEXT;

ALTER TABLE proposals ADD COLUMN approval_expires_at TEXT;

ALTER TABLE proposals ADD COLUMN telegram_message_id TEXT;

ALTER TABLE proposals ADD COLUMN telegram_chat_id TEXT;

CREATE TABLE IF NOT EXISTS autopilot_heartbeats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  loop_name TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  items_processed INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_heartbeats_loop ON autopilot_heartbeats(loop_name, started_at);
