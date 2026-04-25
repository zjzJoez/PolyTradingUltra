-- v08: conviction-tier sizing fields on proposals + rate-limit event log.
-- All ADDs are nullable so pre-v08 code stays readable.

ALTER TABLE proposals ADD COLUMN conviction_tier TEXT;
ALTER TABLE proposals ADD COLUMN catalyst_clarity TEXT;
ALTER TABLE proposals ADD COLUMN downside_risk TEXT;
ALTER TABLE proposals ADD COLUMN asymmetric_target_multiplier REAL;
ALTER TABLE proposals ADD COLUMN thesis_catalyst_deadline TEXT;

CREATE TABLE IF NOT EXISTS llm_rate_limit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  hit_at TEXT NOT NULL,
  stderr_snippet TEXT,
  cooldown_applied_sec INTEGER NOT NULL,
  consecutive_count INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_rate_limit_events_hit ON llm_rate_limit_events(hit_at);
