-- 2026-05-12: track redemption state on positions so the redeem loop can
-- skip already-redeemed markets and the bulk of resolved markets where we
-- never held a position. Before this, redeem_resolved_positions iterated
-- every row in market_resolutions every reconcile tick (114+ markets ×
-- ~11 RPC calls each), which hammered the Polygon RPC into 429s.
--
-- Also fixes a silent failure: _mark_positions_redeemed was writing
-- position_events with event_type='redeem', which violated the original
-- CHECK constraint on position_events.event_type (added to the v0.3/v0.4
-- migration but the column-level CHECK was never actually loosened in the
-- live schema). The redeemer now updates positions.redeemed_* directly.

ALTER TABLE positions ADD COLUMN redeemed_at TEXT;
ALTER TABLE positions ADD COLUMN redeemed_tx_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_positions_resolved_unredeemed
  ON positions(market_id, status, redeemed_at);
