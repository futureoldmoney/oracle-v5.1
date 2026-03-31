-- Oracle Bot v5 Schema
-- Run in Supabase SQL Editor before first deploy
-- ═══════════════════════════════════════════════════════════════

-- Bot control (key-value for mode switching)
CREATE TABLE IF NOT EXISTS bot_control (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT now()
);
INSERT INTO bot_control (key, value) VALUES ('mode', 'paper')
  ON CONFLICT (key) DO NOTHING;

-- Bot config (single row, all tunable parameters)
CREATE TABLE IF NOT EXISTS bot_config (
  id INTEGER PRIMARY KEY DEFAULT 1,
  bankroll DOUBLE PRECISION DEFAULT 1000.0,
  fractional_kelly DOUBLE PRECISION DEFAULT 0.20,
  min_position_pct DOUBLE PRECISION DEFAULT 0.01,
  max_position_pct DOUBLE PRECISION DEFAULT 0.03,
  min_order_usd DOUBLE PRECISION DEFAULT 1.0,
  max_trade_size_usd DOUBLE PRECISION DEFAULT 50.0,
  min_edge_pct DOUBLE PRECISION DEFAULT 3.0,
  taker_fee_rate DOUBLE PRECISION DEFAULT 0.0156,
  max_daily_loss_pct DOUBLE PRECISION DEFAULT 0.10,
  max_drawdown_pct DOUBLE PRECISION DEFAULT 0.30,
  cycle_interval_seconds DOUBLE PRECISION DEFAULT 2.0,
  market_scan_interval DOUBLE PRECISION DEFAULT 15.0,
  updated_at TIMESTAMPTZ DEFAULT now(),
  updated_by TEXT DEFAULT 'V5_INIT'
);
INSERT INTO bot_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- Heartbeats
CREATE TABLE IF NOT EXISTS heartbeats_v2 (
  id BIGSERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ DEFAULT now(),
  bot_mode TEXT,
  balance_usdc NUMERIC,
  active_orders INTEGER DEFAULT 0,
  pending_settlements INTEGER DEFAULT 0,
  trades_today INTEGER DEFAULT 0,
  pnl_today NUMERIC DEFAULT 0,
  status TEXT DEFAULT 'ALIVE' CHECK (status IN ('ALIVE', 'PAUSED', 'ERROR')),
  error_message TEXT
);

-- Paper trades
CREATE TABLE IF NOT EXISTS paper_trades (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMPTZ DEFAULT now(),
  market_id TEXT,
  market_slug TEXT,
  market_question TEXT,
  token_id TEXT,
  window_start_ts BIGINT,
  window_end_ts BIGINT,
  seconds_remaining INTEGER,
  decision TEXT NOT NULL CHECK (decision IN ('PAPER', 'SKIP', 'LIVE')),
  skip_reason TEXT,
  chainlink_price NUMERIC,
  btc_open_price NUMERIC,
  implied_direction TEXT CHECK (implied_direction IN ('UP', 'DOWN')),
  oracle_confidence NUMERIC,
  edge_bps INTEGER,
  side TEXT CHECK (side IN ('YES', 'NO')),
  hypothetical_price NUMERIC,
  hypothetical_size_usdc NUMERIC,
  hypothetical_shares NUMERIC,
  resolved_outcome TEXT,
  won BOOLEAN,
  pnl_usdc NUMERIC,
  settled_at TIMESTAMPTZ,
  engine_version TEXT,
  -- v5 columns
  simulated_fill_price DOUBLE PRECISION,
  fair_value_at_trade DOUBLE PRECISION,
  edge_at_fill DOUBLE PRECISION,
  taker_fee_estimate DOUBLE PRECISION,
  execution_mode TEXT,
  coinbase_price DOUBLE PRECISION,
  deribit_pcr DOUBLE PRECISION,
  ltp_velocity_30s DOUBLE PRECISION,
  sentiment_bias TEXT,
  sentiment_adjustment DOUBLE PRECISION,
  fear_greed_index INTEGER,
  size_pct DOUBLE PRECISION
);

-- Live trades (same schema as paper)
CREATE TABLE IF NOT EXISTS live_trades (LIKE paper_trades INCLUDING ALL);
ALTER TABLE live_trades ALTER COLUMN decision SET DEFAULT 'LIVE';

-- Settled copies
CREATE TABLE IF NOT EXISTS paper_settled (LIKE paper_trades INCLUDING ALL);
CREATE TABLE IF NOT EXISTS live_settled (LIKE paper_trades INCLUDING ALL);

-- Chainlink windows (5-min price data)
CREATE TABLE IF NOT EXISTS chainlink_windows (
  id BIGSERIAL PRIMARY KEY,
  window_ts BIGINT UNIQUE NOT NULL,
  window_start TIMESTAMPTZ,
  open_price DOUBLE PRECISION,
  close_price DOUBLE PRECISION,
  high_price DOUBLE PRECISION,
  low_price DOUBLE PRECISION,
  price_move_pct DOUBLE PRECISION,
  direction TEXT,
  tick_count INTEGER,
  price_path JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_paper_trades_window ON paper_trades(window_start_ts);
CREATE INDEX IF NOT EXISTS idx_paper_trades_settled ON paper_trades(settled_at) WHERE settled_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_live_trades_window ON live_trades(window_start_ts);
CREATE INDEX IF NOT EXISTS idx_live_trades_settled ON live_trades(settled_at) WHERE settled_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_heartbeats_created ON heartbeats_v2(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chainlink_windows_ts ON chainlink_windows(window_ts);

-- Data retention cleanup
CREATE OR REPLACE FUNCTION cleanup_old_data()
RETURNS void AS $$
BEGIN
  DELETE FROM heartbeats_v2 WHERE created_at < now() - interval '7 days';
  DELETE FROM paper_trades WHERE created_at < now() - interval '90 days'
    AND decision = 'SKIP';
END;
$$ LANGUAGE plpgsql;
