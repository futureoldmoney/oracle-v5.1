-- ═══════════════════════════════════════════════════════════════
-- Oracle Bot v5 — Heartbeat Observability Schema
-- ═══════════════════════════════════════════════════════════════
--
-- THEORY:
--   paper_trades = continuous heartbeat (~every 3 seconds)
--     Every evaluation cycle logs what the bot SEES, what it THINKS,
--     and WHY it decided to trade or skip. This is the bot's "thought
--     process" in real time. You can replay any moment and understand
--     exactly what data the bot had and what decision it made.
--
--   paper_settled = only EXECUTED trades with outcomes (won/lost/P&L)
--     This is the scorecard. Clean, no noise, just results.
--
-- FLOW:
--   Every ~3s: evaluation → paper_trades row (skip or trade)
--   On trade: paper_trades row (trade_intention=TRUE) + copy to paper_settled
--   On settlement: paper_settled row updated with won/lost/pnl
--
-- This gives you:
--   - Real-time visibility into WHY the bot isn't trading
--   - Pattern analysis: what does the market look like right before a trade fires?
--   - Threshold tuning: are we skipping too many good opportunities?
--   - Regime detection: how does skip_reason distribution change across market conditions?
-- ═══════════════════════════════════════════════════════════════

-- Drop old paper_trades if you want fresh schema (CAREFUL: loses v4 data)
-- Uncomment the next line only if you're OK losing historical paper_trades:
-- DROP TABLE IF EXISTS paper_trades CASCADE;

-- ═══════════════════════════════════════════════════════════════
-- HEARTBEAT TABLE (every ~3 second evaluation)
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS eval_heartbeat (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMPTZ DEFAULT now(),

  -- ── WINDOW CONTEXT ──────────────────────────────────
  window_ts BIGINT,                     -- 5-min window unix timestamp
  seconds_remaining INTEGER,            -- countdown to window close
  window_phase TEXT,                     -- 'EARLY' / 'MID' / 'SWEET_SPOT' / 'PRIMARY' / 'LATE' / 'TOO_LATE'

  -- ── WHAT THE BOT SEES (raw market data) ─────────────
  chainlink_open DOUBLE PRECISION,      -- CL price at window open
  chainlink_current DOUBLE PRECISION,   -- CL price right now
  chainlink_move_pct DOUBLE PRECISION,  -- (current - open) / open * 100
  binance_price DOUBLE PRECISION,       -- Binance BTC/USD (via proxy)
  coinbase_price DOUBLE PRECISION,      -- Coinbase BTC/USD
  best_bid_yes DOUBLE PRECISION,        -- Polymarket YES book bid
  best_ask_yes DOUBLE PRECISION,        -- Polymarket YES book ask
  spread_bps INTEGER,                   -- (ask - bid) * 10000
  ltp DOUBLE PRECISION,                 -- Last trade price on Polymarket
  tick_velocity DOUBLE PRECISION,       -- Price change rate (LTP/sec)
  deribit_pcr DOUBLE PRECISION,         -- Put/call ratio
  fear_greed_index INTEGER,             -- Alternative.me F&G (0-100)

  -- ── WHAT THE BOT THINKS (computed values) ───────────
  magnitude_pct DOUBLE PRECISION,       -- abs(chainlink_move_pct)
  required_magnitude DOUBLE PRECISION,  -- minimum magnitude for this timing
  implied_direction TEXT,               -- 'UP' / 'DOWN' / 'NEUTRAL'
  fair_value DOUBLE PRECISION,          -- empirical win probability
  fill_price_estimate DOUBLE PRECISION, -- estimated taker fill price
  edge_pct DOUBLE PRECISION,            -- (fair_value - fill) as %
  min_edge_required DOUBLE PRECISION,   -- configured minimum edge
  kelly_raw DOUBLE PRECISION,           -- raw Kelly fraction
  kelly_adj DOUBLE PRECISION,           -- fractional Kelly (raw * 0.20)
  position_size_usd DOUBLE PRECISION,   -- computed trade size
  position_size_pct DOUBLE PRECISION,   -- as % of bankroll
  bankroll DOUBLE PRECISION,            -- current bankroll at eval time

  -- ── AMPLIFIER SIGNALS ───────────────────────────────
  ltp_confirms BOOLEAN,                 -- does LTP agree with oracle direction?
  pcr_adjustment DOUBLE PRECISION,      -- PCR confidence adjustment (±0.03)
  sentiment_adjustment DOUBLE PRECISION,-- sentiment confidence adjustment (±0.03)
  coinbase_agrees BOOLEAN,              -- does Coinbase confirm CL direction?

  -- ── DECISION ────────────────────────────────────────
  trade_intention BOOLEAN NOT NULL DEFAULT FALSE,  -- TRUE = placing trade, FALSE = skipping
  gate_reached INTEGER,                 -- which gate stopped evaluation (1-7, 8=all passed)
  skip_reason TEXT,                     -- human-readable reason for skip (NULL if trading)
  
  -- ── EXECUTION (only populated when trade_intention=TRUE) ──
  side TEXT CHECK (side IN ('YES', 'NO')),
  execution_mode TEXT,                  -- 'TAKER' / 'ADAPTIVE' / 'PAPER'
  order_id TEXT,                        -- CLOB order ID (NULL for paper)

  -- ── META ────────────────────────────────────────────
  cycle_number INTEGER,                 -- monotonic evaluation counter
  eval_duration_ms DOUBLE PRECISION,    -- how long the evaluation took
  engine_version TEXT DEFAULT '5.0.0',
  bot_mode TEXT DEFAULT 'paper'
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_eval_hb_window ON eval_heartbeat(window_ts);
CREATE INDEX IF NOT EXISTS idx_eval_hb_created ON eval_heartbeat(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_eval_hb_trade ON eval_heartbeat(trade_intention) WHERE trade_intention = TRUE;
CREATE INDEX IF NOT EXISTS idx_eval_hb_gate ON eval_heartbeat(gate_reached);

-- ═══════════════════════════════════════════════════════════════
-- EXECUTED TRADES TABLE (only trades that were placed)
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS paper_settled (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMPTZ DEFAULT now(),

  -- ── TRADE IDENTITY ──────────────────────────────────
  eval_heartbeat_id UUID REFERENCES eval_heartbeat(id),  -- link back to the heartbeat that triggered this
  market_id TEXT,
  market_slug TEXT,
  market_question TEXT,
  window_ts BIGINT,
  token_id TEXT,

  -- ── ENTRY DATA (snapshot from heartbeat at trade time) ──
  side TEXT CHECK (side IN ('YES', 'NO')),
  implied_direction TEXT CHECK (implied_direction IN ('UP', 'DOWN')),
  seconds_remaining INTEGER,
  chainlink_open DOUBLE PRECISION,
  chainlink_at_trade DOUBLE PRECISION,
  chainlink_move_pct DOUBLE PRECISION,
  fair_value DOUBLE PRECISION,
  fill_price DOUBLE PRECISION,
  edge_pct DOUBLE PRECISION,
  size_usd DOUBLE PRECISION,
  size_pct DOUBLE PRECISION,
  execution_mode TEXT,
  order_id TEXT,

  -- ── AMPLIFIERS AT TRADE TIME ────────────────────────
  ltp_at_trade DOUBLE PRECISION,
  ltp_confirms BOOLEAN,
  pcr_adjustment DOUBLE PRECISION,
  sentiment_adjustment DOUBLE PRECISION,
  binance_price DOUBLE PRECISION,
  coinbase_price DOUBLE PRECISION,
  fear_greed_index INTEGER,
  tick_velocity DOUBLE PRECISION,
  bankroll_at_trade DOUBLE PRECISION,

  -- ── OUTCOME (populated on settlement) ───────────────
  resolved_outcome TEXT CHECK (resolved_outcome IN ('UP', 'DOWN')),
  chainlink_at_close DOUBLE PRECISION,
  won BOOLEAN,
  gross_pnl DOUBLE PRECISION,
  taker_fee DOUBLE PRECISION,
  net_pnl DOUBLE PRECISION,
  shares DOUBLE PRECISION,
  settled_at TIMESTAMPTZ,

  -- ── META ────────────────────────────────────────────
  engine_version TEXT DEFAULT '5.0.0',
  bot_mode TEXT DEFAULT 'paper'
);

CREATE INDEX IF NOT EXISTS idx_settled_window ON paper_settled(window_ts);
CREATE INDEX IF NOT EXISTS idx_settled_outcome ON paper_settled(won) WHERE won IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_settled_unsettled ON paper_settled(settled_at) WHERE settled_at IS NULL;

-- Same structure for live trades
CREATE TABLE IF NOT EXISTS live_settled (LIKE paper_settled INCLUDING ALL);

-- ═══════════════════════════════════════════════════════════════
-- DATA RETENTION
-- eval_heartbeat generates ~1,200 rows/hour (every 3s)
-- = ~28,800/day = ~200K/week
-- Auto-cleanup keeps 7 days of heartbeats, 90 days of settlements
-- ═══════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION cleanup_heartbeat_data()
RETURNS void AS $$
BEGIN
  -- Keep 7 days of heartbeat data (enough for pattern analysis)
  DELETE FROM eval_heartbeat WHERE created_at < now() - interval '7 days';
  -- Keep 90 days of settled trades (enough for statistical significance)
  DELETE FROM paper_settled WHERE created_at < now() - interval '90 days';
  DELETE FROM live_settled WHERE created_at < now() - interval '90 days';
END;
$$ LANGUAGE plpgsql;


-- ═══════════════════════════════════════════════════════════════
-- USEFUL VIEWS
-- ═══════════════════════════════════════════════════════════════

-- View: Why is the bot skipping? (gate distribution over last hour)
CREATE OR REPLACE VIEW v_skip_reasons_1h AS
SELECT
  gate_reached,
  skip_reason,
  COUNT(*) as count,
  ROUND(COUNT(*)::numeric / SUM(COUNT(*)) OVER() * 100, 1) as pct
FROM eval_heartbeat
WHERE created_at > now() - interval '1 hour'
  AND trade_intention = FALSE
GROUP BY gate_reached, skip_reason
ORDER BY count DESC;

-- View: What does the market look like right before a trade fires?
CREATE OR REPLACE VIEW v_pre_trade_conditions AS
SELECT
  h.created_at,
  h.window_ts,
  h.seconds_remaining,
  h.chainlink_move_pct,
  h.magnitude_pct,
  h.fair_value,
  h.fill_price_estimate,
  h.edge_pct,
  h.position_size_usd,
  h.ltp,
  h.ltp_confirms,
  h.spread_bps,
  h.fear_greed_index,
  h.implied_direction,
  s.won,
  s.net_pnl
FROM eval_heartbeat h
JOIN paper_settled s ON s.eval_heartbeat_id = h.id
ORDER BY h.created_at DESC;

-- View: Heartbeat summary per window (how many evals, did it trade, why not)
CREATE OR REPLACE VIEW v_window_summary AS
SELECT
  window_ts,
  COUNT(*) as total_evals,
  SUM(CASE WHEN trade_intention THEN 1 ELSE 0 END) as trades_placed,
  MAX(magnitude_pct) as max_magnitude_pct,
  MAX(edge_pct) as max_edge_pct,
  MIN(seconds_remaining) as min_secs_remaining,
  MODE() WITHIN GROUP (ORDER BY skip_reason) as most_common_skip,
  MAX(CASE WHEN gate_reached >= 4 THEN TRUE ELSE FALSE END) as passed_magnitude_gate,
  MAX(CASE WHEN gate_reached >= 7 THEN TRUE ELSE FALSE END) as passed_edge_gate
FROM eval_heartbeat
WHERE created_at > now() - interval '24 hours'
GROUP BY window_ts
ORDER BY window_ts DESC;
