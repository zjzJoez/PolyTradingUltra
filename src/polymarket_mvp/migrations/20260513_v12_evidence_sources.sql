-- 2026-05-13: expand market_contexts.source_type CHECK to allow five new
-- evidence adapters. Also adds a tiny adapter_budget_tracking table so
-- paid-tier providers (Tavily, The Odds API) can enforce daily caps
-- without burning the small monthly credit pools.
--
-- New source_type values:
--   'polymarket_historical' — base-rate priors from market_resolutions
--   'gdelt'                 — Global Database of Events, Language, Tone
--   'tavily'                — Tavily LLM-friendly search API
--   'odds_api'              — The Odds API bookmaker consensus
--   'reddit'                — Reddit sentiment via PRAW
--
-- Same CHECK lives on provider_event_links — kept in sync.
--
-- legacy_alter_table=ON ensures FK references survive the RENAME→CREATE→
-- INSERT...SELECT rebuild without rewriting child tables (the lesson from
-- v11). See commit 2a1295e for why this matters.

PRAGMA busy_timeout = 60000;
PRAGMA legacy_alter_table = ON;
PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- ── market_contexts ───────────────────────────────────────────────────
ALTER TABLE market_contexts RENAME TO market_contexts_old;

CREATE TABLE market_contexts (
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

INSERT INTO market_contexts (
  id, market_id, source_type, source_id, title, published_at, url,
  raw_text, display_text, importance_weight, normalized_payload_json, created_at
)
SELECT
  id, market_id, source_type, source_id, title, published_at, url,
  raw_text, display_text, importance_weight, normalized_payload_json, created_at
FROM market_contexts_old;
DROP TABLE market_contexts_old;

CREATE INDEX IF NOT EXISTS idx_market_contexts_market_type
  ON market_contexts(market_id, source_type);

-- ── proposal_contexts ─────────────────────────────────────────────────
-- Same source_type CHECK exists here. Same rebuild pattern.
ALTER TABLE proposal_contexts RENAME TO proposal_contexts_old;

CREATE TABLE proposal_contexts (
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

INSERT INTO proposal_contexts
SELECT * FROM proposal_contexts_old;
DROP TABLE proposal_contexts_old;

-- NOTE: schema.sql contains an old, aspirational definition of
-- provider_event_links with a source_type column, but the actual production
-- table is for sports-fixture mapping and has no source_type. We do NOT
-- touch it here.

-- ── adapter_budget_tracking (new) ─────────────────────────────────────
-- Tracks daily call counts per paid adapter so we don't blow through
-- monthly credit pools (Tavily: 1000 lifetime; Odds API: 500/month free).
-- Keyed by UTC date so a fresh day automatically resets.
CREATE TABLE IF NOT EXISTS adapter_budget_tracking (
  provider TEXT NOT NULL,
  date_utc TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0,
  last_call_at TEXT,
  PRIMARY KEY (provider, date_utc)
);

CREATE INDEX IF NOT EXISTS idx_adapter_budget_date
  ON adapter_budget_tracking(date_utc);

COMMIT;

PRAGMA foreign_keys = ON;
PRAGMA legacy_alter_table = OFF;
