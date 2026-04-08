PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS event_clusters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cluster_key TEXT NOT NULL UNIQUE,
  topic TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'resolved', 'archived')),
  canonical_start_time TEXT,
  canonical_end_time TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_event_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL,
  event_cluster_id INTEGER NOT NULL,
  link_confidence REAL NOT NULL DEFAULT 1.0,
  link_reason TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (market_id, event_cluster_id),
  FOREIGN KEY (market_id) REFERENCES market_snapshots(market_id) ON DELETE CASCADE,
  FOREIGN KEY (event_cluster_id) REFERENCES event_clusters(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_market_event_links_market ON market_event_links(market_id);
CREATE INDEX IF NOT EXISTS idx_market_event_links_cluster ON market_event_links(event_cluster_id);

CREATE TABLE IF NOT EXISTS research_memos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL,
  event_cluster_id INTEGER,
  topic TEXT NOT NULL,
  source_bundle_hash TEXT NOT NULL,
  thesis TEXT NOT NULL,
  supporting_evidence_json TEXT NOT NULL,
  counter_evidence_json TEXT NOT NULL,
  uncertainty_notes TEXT NOT NULL,
  generated_by TEXT NOT NULL,
  memo_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (market_id) REFERENCES market_snapshots(market_id) ON DELETE CASCADE,
  FOREIGN KEY (event_cluster_id) REFERENCES event_clusters(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_memos_bundle
ON research_memos(market_id, source_bundle_hash);

CREATE TABLE IF NOT EXISTS strategy_authorizations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_name TEXT NOT NULL,
  scope_topic TEXT,
  scope_market_type TEXT,
  scope_event_cluster_id INTEGER,
  max_order_usdc REAL NOT NULL,
  max_daily_gross_usdc REAL NOT NULL,
  max_open_positions INTEGER NOT NULL,
  max_daily_loss_usdc REAL NOT NULL,
  max_slippage_bps INTEGER NOT NULL,
  allow_auto_execute INTEGER NOT NULL DEFAULT 0,
  requires_human_if_above_usdc REAL,
  valid_from TEXT NOT NULL,
  valid_until TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active', 'expired', 'revoked')),
  created_by TEXT,
  created_at TEXT NOT NULL,
  revoked_at TEXT,
  FOREIGN KEY (scope_event_cluster_id) REFERENCES event_clusters(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_strategy_authorizations_active
ON strategy_authorizations(status, strategy_name, valid_from, valid_until);

CREATE TABLE IF NOT EXISTS shadow_executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id TEXT NOT NULL,
  simulated_fill_price REAL,
  simulated_size REAL,
  simulated_notional REAL,
  simulated_status TEXT NOT NULL,
  context_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_shadow_executions_proposal ON shadow_executions(proposal_id);

CREATE TABLE IF NOT EXISTS positions (
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
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (execution_id, is_shadow),
  FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id) ON DELETE CASCADE,
  FOREIGN KEY (execution_id) REFERENCES executions(id) ON DELETE CASCADE,
  FOREIGN KEY (market_id) REFERENCES market_snapshots(market_id) ON DELETE CASCADE,
  FOREIGN KEY (event_cluster_id) REFERENCES event_clusters(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

CREATE TABLE IF NOT EXISTS position_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN ('open', 'mark_update', 'reduce', 'close', 'stop', 'resolve', 'reconcile', 'redeem')),
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (position_id) REFERENCES positions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_position_events_position ON position_events(position_id);

CREATE TABLE IF NOT EXISTS order_reconciliations (
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

CREATE INDEX IF NOT EXISTS idx_order_reconciliations_execution ON order_reconciliations(execution_id);

CREATE TABLE IF NOT EXISTS kill_switches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_type TEXT NOT NULL CHECK(scope_type IN ('global', 'strategy', 'market', 'event_cluster')),
  scope_key TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active', 'released')),
  reason TEXT NOT NULL,
  created_by TEXT,
  created_at TEXT NOT NULL,
  released_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_kill_switches_active ON kill_switches(scope_type, scope_key, status);

CREATE TABLE IF NOT EXISTS exit_recommendations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL,
  recommendation TEXT NOT NULL CHECK(recommendation IN ('hold', 'reduce', 'close', 'cancel')),
  target_reduce_pct REAL,
  reasoning TEXT NOT NULL,
  confidence_score REAL NOT NULL,
  created_at TEXT NOT NULL,
  action_status TEXT NOT NULL CHECK(action_status IN ('generated', 'approved', 'executed', 'dismissed')),
  payload_json TEXT NOT NULL,
  FOREIGN KEY (position_id) REFERENCES positions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_exit_recommendations_position ON exit_recommendations(position_id);

CREATE TABLE IF NOT EXISTS agent_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER,
  proposal_id TEXT,
  event_cluster_id INTEGER,
  review_type TEXT NOT NULL CHECK(review_type IN ('post_trade', 'post_resolution', 'strategy_weekly')),
  summary TEXT NOT NULL,
  what_worked TEXT NOT NULL,
  what_failed TEXT NOT NULL,
  failure_bucket TEXT NOT NULL CHECK(failure_bucket IN ('signal', 'timing', 'sizing', 'execution', 'risk', 'unknown')),
  next_action TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (position_id) REFERENCES positions(id) ON DELETE SET NULL,
  FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id) ON DELETE SET NULL,
  FOREIGN KEY (event_cluster_id) REFERENCES event_clusters(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_reviews_position ON agent_reviews(position_id);

CREATE TABLE IF NOT EXISTS proposals_v_next (
  proposal_id TEXT PRIMARY KEY,
  market_id TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK(length(trim(outcome)) > 0),
  confidence_score REAL NOT NULL,
  recommended_size_usdc REAL NOT NULL,
  reasoning TEXT NOT NULL,
  decision_engine TEXT NOT NULL CHECK(decision_engine IN ('heuristic', 'openclaw_llm')),
  status TEXT NOT NULL CHECK(status IN ('proposed', 'risk_blocked', 'pending_approval', 'approved', 'rejected', 'authorized_for_execution', 'executed', 'failed', 'expired', 'cancelled')),
  max_slippage_bps INTEGER NOT NULL DEFAULT 500,
  strategy_name TEXT,
  topic TEXT,
  event_cluster_id INTEGER,
  source_memo_id INTEGER,
  authorization_status TEXT NOT NULL DEFAULT 'none' CHECK(authorization_status IN ('none', 'matched_manual_only', 'matched_auto_execute')),
  supervisor_decision TEXT CHECK(supervisor_decision IN ('promote', 'discard', 'merged')),
  priority_score REAL,
  proposal_json TEXT NOT NULL,
  context_payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (market_id) REFERENCES market_snapshots(market_id),
  FOREIGN KEY (event_cluster_id) REFERENCES event_clusters(id) ON DELETE SET NULL,
  FOREIGN KEY (source_memo_id) REFERENCES research_memos(id) ON DELETE SET NULL
);

INSERT INTO proposals_v_next (
  proposal_id, market_id, outcome, confidence_score, recommended_size_usdc, reasoning,
  decision_engine, status, max_slippage_bps, strategy_name, topic, event_cluster_id,
  source_memo_id, authorization_status, supervisor_decision, priority_score,
  proposal_json, context_payload_json, created_at, updated_at
)
SELECT
  proposal_id,
  market_id,
  outcome,
  confidence_score,
  recommended_size_usdc,
  reasoning,
  decision_engine,
  status,
  max_slippage_bps,
  NULL,
  NULL,
  NULL,
  NULL,
  'none',
  NULL,
  NULL,
  proposal_json,
  context_payload_json,
  created_at,
  updated_at
FROM proposals;

DROP TABLE proposals;
ALTER TABLE proposals_v_next RENAME TO proposals;

CREATE INDEX IF NOT EXISTS idx_proposals_market ON proposals(market_id);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_strategy ON proposals(strategy_name);
CREATE INDEX IF NOT EXISTS idx_proposals_cluster ON proposals(event_cluster_id);

PRAGMA foreign_keys = ON;
