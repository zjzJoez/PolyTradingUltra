-- Phase 2: extend market_contexts.source_type CHECK to allow 'sports_data'
-- (football-data.org adapter) and 'web_search' (DuckDuckGo fallback the
-- WebSearchAdapter has been emitting all along — its inserts were quietly
-- being rejected on freshly built schemas with the old CHECK list).
--
-- SQLite cannot ALTER a CHECK constraint, so we rebuild both context tables
-- in place. FKs are disabled for the rebuild so child rows survive the
-- DROP/RENAME dance, then re-enabled.

-- PRAGMA foreign_keys must be set outside an active transaction — SQLite
-- silently ignores it inside BEGIN/COMMIT. Toggle it, then wrap the actual
-- rebuild in an explicit transaction so a mid-script failure rolls back to
-- the pre-migration state instead of leaving an orphaned *_old table.
PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

ALTER TABLE market_contexts RENAME TO market_contexts_old;

CREATE TABLE market_contexts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN ('cryptopanic', 'apify_twitter', 'perplexity', 'web_search', 'sports_data')),
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

INSERT INTO market_contexts
  (id, market_id, source_type, source_id, title, published_at, url,
   raw_text, display_text, importance_weight, normalized_payload_json, created_at)
SELECT id, market_id, source_type, source_id, title, published_at, url,
       raw_text, display_text, importance_weight, normalized_payload_json, created_at
FROM market_contexts_old;

DROP TABLE market_contexts_old;

CREATE INDEX IF NOT EXISTS idx_market_contexts_market ON market_contexts(market_id);
CREATE INDEX IF NOT EXISTS idx_market_contexts_market_type ON market_contexts(market_id, source_type);

ALTER TABLE proposal_contexts RENAME TO proposal_contexts_old;

CREATE TABLE proposal_contexts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN ('cryptopanic', 'apify_twitter', 'perplexity', 'web_search', 'sports_data')),
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
  (id, proposal_id, source_type, source_id, title, published_at, url,
   raw_text, display_text, importance_weight, normalized_payload_json, created_at)
SELECT id, proposal_id, source_type, source_id, title, published_at, url,
       raw_text, display_text, importance_weight, normalized_payload_json, created_at
FROM proposal_contexts_old;

DROP TABLE proposal_contexts_old;

CREATE INDEX IF NOT EXISTS idx_proposal_contexts_proposal ON proposal_contexts(proposal_id);

COMMIT;

PRAGMA foreign_keys = ON;
