"""
V5 Default Configuration
=========================
All tunable parameters in one place.
Each parameter has a comment explaining what it does and its valid range.
"""

DEFAULT_CONFIG = {
    # ── Position Sizing ──────────────────────────────────
    "bankroll": 1000.0,              # Current bankroll in USDC [1-100000]
    "fractional_kelly": 0.20,        # Kelly fraction (0.20 = 20% Kelly) [0.05-1.0]
    "min_position_pct": 0.01,        # Minimum trade as % of bankroll [0.005-0.10]
    "max_position_pct": 0.03,        # Maximum trade as % of bankroll [0.01-0.50]
    "min_order_usd": 1.0,            # Absolute minimum order size [0.25-100]
    "max_trade_size_usd": 50.0,      # Absolute maximum order size [5-1000]

    # ── Edge Requirements ────────────────────────────────
    "min_edge_pct": 3.0,             # Minimum edge % to trade [1.0-20.0]
    "taker_fee_rate": 0.0156,        # Polymarket taker fee (1.56%) [0.0-0.05]

    # ── Risk Limits ──────────────────────────────────────
    "max_daily_loss_pct": 0.10,      # Stop trading after this % daily loss [0.05-0.50]
    "max_drawdown_pct": 0.30,        # Alert threshold for drawdown from peak [0.05-0.50]

    # ── Timing ───────────────────────────────────────────
    "cycle_interval_seconds": 2.0,   # Main evaluation loop frequency [0.5-10]
    "market_scan_interval": 15.0,    # How often to scan for new markets [5-60]

    # ── Amplifier Weights (for logging/tuning, not trade decisions) ──
    "weight_ltp_confirmation": 0.02,  # LTP agrees → +2% confidence [0-0.05]
    "weight_pcr_signal": 0.03,        # Deribit PCR → ±3% confidence [0-0.05]
    "weight_sentiment": 0.03,         # Sentiment → ±3% confidence [0-0.05]
    "weight_coinbase_cross": 0.03,    # Coinbase disagrees → -3% confidence [0-0.05]
}
