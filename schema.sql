PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  name TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_snapshots (
  market_id TEXT PRIMARY KEY,
  question TEXT,
  slug TEXT,
  market_url TEXT,
  condition_id TEXT,
  active INTEGER NOT NULL DEFAULT 0,
  closed INTEGER NOT NULL DEFAULT 0,
  accepting_orders INTEGER NOT NULL DEFAULT 0,
  end_date TEXT,
  seconds_to_expiry INTEGER,
  days_to_expiry REAL,
  liquidity_usdc REAL,
  volume_usdc REAL,
  volume_24h_usdc REAL,
  outcomes_json TEXT NOT NULL,
  market_json TEXT NOT NULL,
  last_scanned_at TEXT NOT NULL
);

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

CREATE TABLE IF NOT EXISTS market_contexts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN (
    'cryptopanic', 'apify_twitter', 'perplexity', 'web_search', 'sports_data',
    'polymarket_historical', 'gdelt', 'tavily', 'odds_api', 'reddit'
  )),
  source_id TEXT,
  title TEXT,
  published_at TEXT,
  url TEXT,
  raw_text TEXT NOT NULL,
  display_text TEXT NOT NULL,
  importance_weight REAL NOT NULL DEFAULT 1.0,
  normalized_payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (market_id) REFERENCES market_snapshots(market_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_market_contexts_market ON market_contexts(market_id);
CREATE INDEX IF NOT EXISTS idx_market_contexts_market_type ON market_contexts(market_id, source_type);

-- Daily call counter for paid context adapters (Tavily, The Odds API) to
-- protect small credit pools. Keyed by UTC date so a new day auto-resets.
CREATE TABLE IF NOT EXISTS adapter_budget_tracking (
  provider TEXT NOT NULL,
  date_utc TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0,
  last_call_at TEXT,
  PRIMARY KEY (provider, date_utc)
);

CREATE INDEX IF NOT EXISTS idx_adapter_budget_date ON adapter_budget_tracking(date_utc);

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

CREATE TABLE IF NOT EXISTS proposals (
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
  risk_block_reasons_json TEXT,
  llm_meta_json TEXT,
  conviction_tier TEXT,
  catalyst_clarity TEXT,
  downside_risk TEXT,
  asymmetric_target_multiplier REAL,
  thesis_catalyst_deadline TEXT,
  proposal_json TEXT NOT NULL,
  context_payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (market_id) REFERENCES market_snapshots(market_id),
  FOREIGN KEY (event_cluster_id) REFERENCES event_clusters(id) ON DELETE SET NULL,
  FOREIGN KEY (source_memo_id) REFERENCES research_memos(id) ON DELETE SET NULL,
  FOREIGN KEY (target_position_id) REFERENCES positions(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS llm_rate_limit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  hit_at TEXT NOT NULL,
  stderr_snippet TEXT,
  cooldown_applied_sec INTEGER NOT NULL,
  consecutive_count INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_rate_limit_events_hit ON llm_rate_limit_events(hit_at);

CREATE INDEX IF NOT EXISTS idx_proposals_market ON proposals(market_id);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_strategy ON proposals(strategy_name);
CREATE INDEX IF NOT EXISTS idx_proposals_cluster ON proposals(event_cluster_id);
CREATE INDEX IF NOT EXISTS idx_proposals_alpha_signal ON proposals(alpha_signal_id);

CREATE TABLE IF NOT EXISTS proposal_contexts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN (
    'cryptopanic', 'apify_twitter', 'perplexity', 'web_search', 'sports_data',
    'polymarket_historical', 'gdelt', 'tavily', 'odds_api', 'reddit'
  )),
  source_id TEXT,
  title TEXT,
  published_at TEXT,
  url TEXT,
  raw_text TEXT NOT NULL,
  display_text TEXT NOT NULL,
  importance_weight REAL NOT NULL DEFAULT 1.0,
  normalized_payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_proposal_contexts_proposal ON proposal_contexts(proposal_id);

CREATE TABLE IF NOT EXISTS approvals (
  proposal_id TEXT PRIMARY KEY,
  decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected')),
  decided_at TEXT NOT NULL,
  telegram_user_id TEXT,
  telegram_username TEXT,
  callback_query_id TEXT NOT NULL UNIQUE,
  telegram_message_id TEXT,
  raw_callback_json TEXT NOT NULL,
  FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS executions (
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

CREATE INDEX IF NOT EXISTS idx_executions_proposal ON executions(proposal_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_executions_real_success
ON executions(proposal_id)
WHERE mode = 'real' AND status = 'filled';

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

CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_resolved_unredeemed ON positions(market_id, status, redeemed_at);

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

CREATE TABLE IF NOT EXISTS market_resolutions (
  market_id TEXT PRIMARY KEY,
  resolved_outcome TEXT NOT NULL,
  resolved_at TEXT NOT NULL,
  source_payload_json TEXT NOT NULL,
  FOREIGN KEY (market_id) REFERENCES market_snapshots(market_id)
);
