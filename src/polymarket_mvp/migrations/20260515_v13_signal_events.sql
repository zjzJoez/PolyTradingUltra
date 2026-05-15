-- 2026-05-15: signal_events table for deterministic-signal shadow tracking.
-- Each row records "what signal X would have done" on a given market at a
-- given moment, without actually placing a trade. This lets us evaluate
-- strategy candidates (Strategy D = ClubElo favorites, B = Pinnacle
-- divergence, A = Dixon-Coles, etc.) against the same market stream the
-- live bot sees, then run win-rate + CLV analysis on resolved markets
-- without ever risking capital.

PRAGMA busy_timeout = 60000;

BEGIN TRANSACTION;

CREATE TABLE IF NOT EXISTS signal_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_name TEXT NOT NULL,                  -- 'clubelo' | 'pinnacle_divergence' | 'dixon_coles' | ...
  market_id TEXT NOT NULL,
  outcome TEXT NOT NULL,                      -- 'Yes' / 'No' / 'home' / 'away' / 'draw' as appropriate
  recommendation TEXT NOT NULL CHECK(recommendation IN ('bet', 'skip', 'no_match')),
  model_p REAL,                               -- signal's predicted probability of `outcome` resolving true
  market_p REAL,                              -- Polymarket price for `outcome` at evaluation time
  edge REAL,                                  -- model_p - market_p
  size_recommendation_usdc REAL,              -- what the signal would have bet (NULL when recommendation != 'bet')
  payload_json TEXT NOT NULL DEFAULT '{}',    -- signal-specific debug info (Elo scores, raw probs, reasoning)
  generated_at TEXT NOT NULL,
  FOREIGN KEY (market_id) REFERENCES market_snapshots(market_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_signal_events_signal_market
  ON signal_events(signal_name, market_id);
CREATE INDEX IF NOT EXISTS idx_signal_events_generated
  ON signal_events(generated_at);
CREATE INDEX IF NOT EXISTS idx_signal_events_recommendation
  ON signal_events(signal_name, recommendation, generated_at);

COMMIT;
