PRAGMA foreign_keys = ON;

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

CREATE TABLE IF NOT EXISTS market_contexts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN ('cryptopanic', 'apify_twitter', 'perplexity')),
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

CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY,
  market_id TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK(length(trim(outcome)) > 0),
  confidence_score REAL NOT NULL,
  recommended_size_usdc REAL NOT NULL,
  reasoning TEXT NOT NULL,
  decision_engine TEXT NOT NULL CHECK(decision_engine IN ('heuristic', 'openclaw_llm')),
  status TEXT NOT NULL CHECK(status IN ('proposed', 'risk_blocked', 'pending_approval', 'approved', 'rejected', 'executed', 'failed')),
  max_slippage_bps INTEGER NOT NULL DEFAULT 500,
  proposal_json TEXT NOT NULL,
  context_payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (market_id) REFERENCES market_snapshots(market_id)
);

CREATE INDEX IF NOT EXISTS idx_proposals_market ON proposals(market_id);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);

CREATE TABLE IF NOT EXISTS proposal_contexts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN ('cryptopanic', 'apify_twitter', 'perplexity')),
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
  status TEXT NOT NULL,
  filled_size_usdc REAL,
  avg_fill_price REAL,
  txhash_or_order_id TEXT,
  slippage_bps REAL,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_executions_proposal ON executions(proposal_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_executions_real_success
ON executions(proposal_id)
WHERE mode = 'real' AND status = 'filled';

CREATE TABLE IF NOT EXISTS market_resolutions (
  market_id TEXT PRIMARY KEY,
  resolved_outcome TEXT NOT NULL,
  resolved_at TEXT NOT NULL,
  source_payload_json TEXT NOT NULL,
  FOREIGN KEY (market_id) REFERENCES market_snapshots(market_id)
);
