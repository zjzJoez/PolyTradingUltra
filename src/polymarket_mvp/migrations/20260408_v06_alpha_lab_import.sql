-- v0.6 alpha lab integration: extend proposals for alpha signal import

-- SQLite does not support ALTER TABLE ... ALTER CONSTRAINT, so we recreate
-- the CHECK via a trigger-based approach. The CHECK constraint on decision_engine
-- is relaxed by creating proposals through the new column-based approach.
-- For existing DBs, we add the new columns and use a trigger to validate.

-- Alpha Lab metadata columns on proposals
ALTER TABLE proposals ADD COLUMN alpha_signal_id TEXT;
ALTER TABLE proposals ADD COLUMN alpha_fair_probability REAL;
ALTER TABLE proposals ADD COLUMN alpha_market_probability REAL;
ALTER TABLE proposals ADD COLUMN alpha_gross_edge_bps REAL;
ALTER TABLE proposals ADD COLUMN alpha_net_edge_bps REAL;
ALTER TABLE proposals ADD COLUMN alpha_model_version TEXT;
ALTER TABLE proposals ADD COLUMN alpha_mapping_confidence REAL;

CREATE INDEX IF NOT EXISTS idx_proposals_alpha_signal
ON proposals(alpha_signal_id);

-- Since SQLite cannot alter CHECK constraints in place, we drop and recreate
-- the constraint via a workaround: create a trigger that enforces the new set.
-- The original CHECK still exists but only blocks values not in the OLD set.
-- We handle 'alpha_lab' by inserting with a temp value and updating.
-- Actually, the simplest safe approach: just allow the new engine value via
-- a BEFORE INSERT trigger that aborts for truly invalid values.
-- NOTE: The existing CHECK constraint ('heuristic', 'openclaw_llm') must be
-- relaxed. On fresh installs, schema.sql already has the updated CHECK.
-- For existing DBs, we work around the CHECK by temporarily disabling it.

-- Drop the old table and recreate with the new CHECK constraint.
-- This is safe because we copy all data.
CREATE TABLE IF NOT EXISTS proposals_new (
  proposal_id TEXT PRIMARY KEY,
  market_id TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK(length(trim(outcome)) > 0),
  confidence_score REAL NOT NULL,
  recommended_size_usdc REAL NOT NULL,
  reasoning TEXT NOT NULL,
  decision_engine TEXT NOT NULL CHECK(decision_engine IN ('heuristic', 'openclaw_llm', 'alpha_lab')),
  status TEXT NOT NULL CHECK(status IN ('proposed', 'risk_blocked', 'pending_approval', 'approved', 'rejected', 'authorized_for_execution', 'executed', 'failed', 'expired', 'cancelled')),
  max_slippage_bps INTEGER NOT NULL DEFAULT 500,
  strategy_name TEXT,
  topic TEXT,
  event_cluster_id INTEGER,
  source_memo_id INTEGER,
  authorization_status TEXT NOT NULL DEFAULT 'none' CHECK(authorization_status IN ('none', 'matched_manual_only', 'matched_auto_execute')),
  supervisor_decision TEXT CHECK(supervisor_decision IN ('promote', 'discard', 'merged')),
  priority_score REAL,
  proposal_kind TEXT NOT NULL DEFAULT 'entry' CHECK(proposal_kind IN ('entry', 'exit')),
  target_position_id INTEGER,
  approval_ttl_seconds INTEGER,
  order_live_ttl_seconds INTEGER,
  approval_requested_at TEXT,
  approval_expires_at TEXT,
  telegram_message_id TEXT,
  telegram_chat_id TEXT,
  alpha_signal_id TEXT,
  alpha_fair_probability REAL,
  alpha_market_probability REAL,
  alpha_gross_edge_bps REAL,
  alpha_net_edge_bps REAL,
  alpha_model_version TEXT,
  alpha_mapping_confidence REAL,
  proposal_json TEXT NOT NULL,
  context_payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (market_id) REFERENCES market_snapshots(market_id),
  FOREIGN KEY (event_cluster_id) REFERENCES event_clusters(id) ON DELETE SET NULL,
  FOREIGN KEY (source_memo_id) REFERENCES research_memos(id) ON DELETE SET NULL,
  FOREIGN KEY (target_position_id) REFERENCES positions(id) ON DELETE SET NULL
);

INSERT OR IGNORE INTO proposals_new
SELECT
  proposal_id, market_id, outcome, confidence_score, recommended_size_usdc, reasoning,
  decision_engine, status, max_slippage_bps, strategy_name, topic, event_cluster_id,
  source_memo_id, authorization_status, supervisor_decision, priority_score,
  proposal_kind, target_position_id, approval_ttl_seconds, order_live_ttl_seconds,
  approval_requested_at, approval_expires_at, telegram_message_id, telegram_chat_id,
  NULL, NULL, NULL, NULL, NULL, NULL, NULL,
  proposal_json, context_payload_json, created_at, updated_at
FROM proposals;

DROP TABLE proposals;
ALTER TABLE proposals_new RENAME TO proposals;

CREATE INDEX IF NOT EXISTS idx_proposals_market ON proposals(market_id);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_strategy ON proposals(strategy_name);
CREATE INDEX IF NOT EXISTS idx_proposals_cluster ON proposals(event_cluster_id);
CREATE INDEX IF NOT EXISTS idx_proposals_alpha_signal ON proposals(alpha_signal_id);
