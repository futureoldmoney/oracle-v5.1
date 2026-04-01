"""
Oracle Bot v5 — Entry Point
=============================
Single async event loop. Single evaluation path.
No evaluate_cycle() + evaluate_v4() dual path (v4 bug source).

Startup verification checklist (from verification chain):
  ✓ All required env vars present
  ✓ Supabase connected
  ✓ CLOB client authenticated
  ✓ Chainlink poller started
  ✓ Scanner found market
  ✓ Mode read from database (env var is fallback only)
  ✓ First heartbeat written
"""

import os
import sys
import json
import time
import asyncio
import logging
from typing import Optional, Dict

# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════

def setup_logging(level: str = "INFO"):
    fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format=fmt, stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

logger = logging.getLogger("oracle")


# ═══════════════════════════════════════════════════════════════
# ENVIRONMENT LOADING
# ═══════════════════════════════════════════════════════════════

REQUIRED_VARS = [
    "PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS", "POLYGON_RPC_URL",
    "SUPABASE_URL", "SUPABASE_KEY",
]

OPTIONAL_VARS = {
    "BOT_MODE": "paper",
    "CLOB_PROXY_URL": "",
    "DISCORD_WEBHOOK_URL": "",
    "LOG_LEVEL": "INFO",
    "CYCLE_INTERVAL": "2.0",
    "ENGINE_VERSION": "5.0.0",
}


def load_env() -> Dict[str, str]:
    """Load and validate environment variables. Fail loud on missing required vars."""
    env = {}
    missing = []
    for key in REQUIRED_VARS:
        val = os.getenv(key)
        if not val:
            missing.append(key)
        else:
            env[key] = val

    if missing:
        logger.error(f"FATAL: Missing required env vars: {missing}")
        sys.exit(1)

    for key, default in OPTIONAL_VARS.items():
        env[key] = os.getenv(key, default)

    # Log env vars (redacted) for deployment verification
    for key in env:
        val = env[key]
        if key in ("PRIVATE_KEY", "SUPABASE_KEY"):
            display = f"{val[:6]}...{val[-4:]}" if len(val) > 10 else "***"
        elif len(val) > 20:
            display = f"{val[:20]}..."
        else:
            display = val
        logger.info(f"  ENV {key} = {display}")

    return env


# ═══════════════════════════════════════════════════════════════
# BOT SERVICE
# ═══════════════════════════════════════════════════════════════

class OracleBot:
    """
    Main bot service. Single evaluation path.

    Verification chain applied:
    - ONE evaluate() call per cycle (no dual-path bug)
    - Mode from Supabase first, env var fallback
    - All data passed as simple types to engine.evaluate()
    - Startup checklist logged
    """

    def __init__(self, env: Dict[str, str]):
        self.env = env
        self.mode = "paper"
        self.cycle_interval = float(env.get("CYCLE_INTERVAL", "2.0"))
        self._cycle_count = 0
        self._running = True

        # Components (initialized in setup())
        self.sb = None           # Supabase client
        self.clob = None         # py-clob-client
        self.engine = None       # OracleEngine
        self.tracker = None      # ChainlinkTracker
        self.scanner = None      # MarketScanner
        self.book_fetcher = None # BookFetcher
        self.executor = None     # OrderExecutor
        self.binance = None      # BinancePoller
        self.coinbase = None     # CoinbasePoller
        self.ltp = None          # LTPPoller
        self.pcr = None          # DeribitPCR
        self.sentiment = None    # SentimentTracker
        self.db = None           # DatabaseOps

    async def setup(self):
        """Initialize all components. Log startup checklist."""
        logger.info("=" * 60)
        logger.info("Oracle Bot v5 — Starting")
        logger.info("=" * 60)

        # 1. Supabase
        from supabase import create_client
        self.sb = create_client(self.env["SUPABASE_URL"], self.env["SUPABASE_KEY"])
        logger.info("✓ Supabase connected")

        # 2. Read mode from database
        self.mode = await self._read_mode()
        logger.info(f"✓ Mode: {self.mode}")

        # 3. Database operations
        from engine.db import DatabaseOps
        self.db = DatabaseOps(self.sb)

        # 4. Load config from Supabase (or use defaults)
        from engine.config import DEFAULT_CONFIG
        config = await self.db.load_config()
        if not config:
            config = DEFAULT_CONFIG.copy()
            logger.warning("Using default config (Supabase config not found)")
        else:
            logger.info(f"✓ Config loaded from Supabase (bankroll=${config.get('bankroll', 0):.2f})")

        # 5. Oracle engine
        from engine.oracle_engine import OracleEngine
        self.engine = OracleEngine(config)
        logger.info("✓ Oracle engine initialized")

        # 6. Chainlink tracker
        from engine.chainlink import ChainlinkTracker
        self.tracker = ChainlinkTracker(supabase_client=self.sb)
        logger.info("✓ Chainlink tracker initialized")

        # 7. CLOB client
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        host = "https://clob.polymarket.com"
        chain_id = 137
        key = self.env["PRIVATE_KEY"]
        funder = self.env.get("POLYMARKET_FUNDER_ADDRESS")
        sig_type = int(os.environ.get("POLYMARKET_SIG_TYPE", "0"))

        self.clob = ClobClient(
            host, key=key, chain_id=chain_id,
            signature_type=sig_type, funder=funder,
        )
        self.clob.set_api_creds(self.clob.create_or_derive_api_creds())
        logger.info("✓ CLOB client authenticated")

        # 8. Market feeds
        from engine.feeds import (MarketScanner, BookFetcher, BinancePoller,
                                   CoinbasePoller, LTPPoller, DeribitPCR, SentimentTracker)
        self.scanner = MarketScanner()
        self.book_fetcher = BookFetcher()
        self.binance = BinancePoller(proxy_url=self.env.get("CLOB_PROXY_URL") or None)
        self.coinbase = CoinbasePoller()
        self.ltp = LTPPoller()
        self.pcr = DeribitPCR()
        self.sentiment = SentimentTracker()
        logger.info("✓ Market feeds initialized")

        # 9. Executor
        from engine.executor import OrderExecutor
        self.executor = OrderExecutor(self.clob, mode=self.mode)
        logger.info(f"✓ Executor initialized (mode={self.mode})")

        # 10. Wallet balance (live mode only — paper uses config bankroll)
        if self.mode == "live":
            balance = await self.db.sync_wallet_balance(self.clob)
            if balance is not None:
                self.engine.update_bankroll(balance)
                logger.info(f"✓ Wallet balance: ${balance:.2f}")
        else:
            logger.info(f"✓ Paper bankroll: ${self.engine.bankroll:.2f} (from config)")

        logger.info("=" * 60)
        logger.info(f"Oracle Bot v5 ready ({self.mode} mode)")
        logger.info("=" * 60)

    async def run(self):
        """Main run loop. Starts all background tasks."""
        await self.setup()

        tasks = [
            asyncio.create_task(self._evaluation_loop(), name="eval"),
            asyncio.create_task(self._chainlink_loop(), name="chainlink"),
            asyncio.create_task(self._scanner_loop(), name="scanner"),
            asyncio.create_task(self._amplifier_loop(), name="amplifiers"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._settlement_loop(), name="settlement"),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Bot shutting down")
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)

    # ── EVALUATION LOOP (THE SINGLE PATH) ─────────────────

    async def _evaluation_loop(self):
        """
        Main evaluation loop. ONE call to engine.evaluate() per cycle.
        All data gathered here, passed as simple types.
        No callbacks, no hidden state lookups, no key name mismatches.
        """
        # Wait for first Chainlink price
        logger.info("Evaluation loop: waiting for Chainlink data...")
        while self._running:
            if self.tracker.get_state() is not None:
                break
            await asyncio.sleep(1.0)
        logger.info("Evaluation loop: started")

        while self._running:
            try:
                self._cycle_count += 1

                # Gather ALL inputs as simple types
                cl_state = self.tracker.get_state()
                if cl_state is None:
                    await asyncio.sleep(self.cycle_interval)
                    continue

                market = self.scanner._current_market
                if market is None:
                    await asyncio.sleep(self.cycle_interval)
                    continue

                window_ts = cl_state["window_ts"]
                secs_left = max(0, (window_ts + 300) - int(time.time()))

                # Book data with fallback chain (verification: guaranteed keys)
                best_bid_yes = None
                best_ask_yes = None

                if market.get("yes_token_id"):
                    book = await self.book_fetcher.get_book(market["yes_token_id"])
                    best_bid_yes = book.get("best_bid")
                    best_ask_yes = book.get("best_ask")

                # Fallback to scanner prices if CLOB book is empty
                if best_bid_yes is None:
                    best_bid_yes = market.get("best_bid")
                if best_ask_yes is None:
                    best_ask_yes = market.get("best_ask")

                # LTP
                ltp_price = None
                if market.get("yes_token_id"):
                    ltp_price = await self.ltp.poll(market["yes_token_id"])

                tick_vel = self.ltp.get_velocity(market.get("yes_token_id", ""))

                # PCR + sentiment (cached, don't block)
                pcr_signal = self.pcr._last_signal
                sent_adj = self.sentiment.get_adjustment(
                    "UP" if cl_state["move_pct"] > 0 else "DOWN")

                # ── THE SINGLE EVALUATE CALL ──────────────
                eval_start = time.time()
                decision = self.engine.evaluate(
                    chainlink_open=cl_state["open_price"],
                    chainlink_current=cl_state["current_price"],
                    seconds_remaining=secs_left,
                    best_bid_yes=best_bid_yes,
                    best_ask_yes=best_ask_yes,
                    window_ts=window_ts,
                    ltp=ltp_price,
                    pcr_signal=pcr_signal,
                    sentiment_adjustment=sent_adj,
                    tick_velocity=tick_vel,
                    coinbase_price=self.coinbase.price,
                )
                eval_ms = (time.time() - eval_start) * 1000

                # Build market_data context dict for heartbeat logging
                from engine.oracle_engine import OracleEngine
                required_mag = OracleEngine._get_required_magnitude(secs_left)
                cb_agrees = None
                if self.coinbase.price > 0 and cl_state["open_price"] > 0:
                    cb_dir = "UP" if self.coinbase.price > cl_state["open_price"] else "DOWN"
                    cl_dir = "UP" if cl_state["move_pct"] > 0 else "DOWN"
                    cb_agrees = (cb_dir == cl_dir)

                market_data = {
                    "best_bid_yes": best_bid_yes,
                    "best_ask_yes": best_ask_yes,
                    "ltp": ltp_price,
                    "binance_price": self.binance.price,
                    "deribit_pcr": pcr_signal.get("pcr") if pcr_signal else None,
                    "fear_greed_index": self.sentiment._value if hasattr(self.sentiment, '_value') else None,
                    "required_magnitude": required_mag,
                    "min_edge_pct": self.engine.min_edge_pct,
                    "bankroll": self.engine.bankroll,
                    "coinbase_agrees": cb_agrees,
                }

                # Log evaluation
                if self._cycle_count % 15 == 0 or decision.should_trade:
                    logger.info(
                        f"Eval #{self._cycle_count} | trade={decision.should_trade} | "
                        f"CL_move={cl_state['move_pct']:+.4f}% | secs_left={secs_left} | "
                        f"bid={best_bid_yes} ask={best_ask_yes} | "
                        f"reason={decision.reason[:80]}"
                    )

                # Log EVERY eval to heartbeat table
                heartbeat_id = await self.db.log_eval_heartbeat(
                    decision, market, market_data,
                    self._cycle_count, eval_ms, self.mode)

                # Execute trade if signal fires
                if decision.should_trade:
                    await self._execute_trade(decision, market, heartbeat_id)

            except Exception as e:
                logger.error(f"Eval cycle error: {e}", exc_info=True)

            await asyncio.sleep(self.cycle_interval)

    async def _execute_trade(self, decision, market: Dict, heartbeat_id: Optional[str] = None):
        """Place order and log result."""
        try:
            result = await self.executor.place_order(
                decision=decision,
                token_id=market.get("yes_token_id") if decision.side == "YES"
                         else market.get("no_token_id"),
                market_id=market.get("condition_id", ""),
            )

            await self.db.log_trade(decision, market, result, heartbeat_id, self.mode)

            # Discord webhook alert
            webhook = self.env.get("DISCORD_WEBHOOK_URL")
            if webhook:
                await self._send_webhook(webhook, decision, result)

        except Exception as e:
            logger.error(f"Execution error: {e}", exc_info=True)

    # ── BACKGROUND LOOPS ──────────────────────────────────

    async def _chainlink_loop(self):
        """Poll Chainlink on-chain price every 3 seconds."""
        from engine.chainlink import chainlink_poller
        rpc_url = self.env.get("POLYGON_RPC_URL")
        if not rpc_url:
            logger.error("POLYGON_RPC_URL not set — Chainlink poller disabled")
            return
        await chainlink_poller(self.tracker, rpc_url, interval=3.0)

    async def _scanner_loop(self):
        """Scan for new BTC 5-min markets."""
        while self._running:
            try:
                market = await self.scanner.scan()
                if market:
                    logger.debug(f"Scanner: {market.get('question', '?')} | "
                                 f"{market.get('seconds_before_close', 0)}s left")
            except Exception as e:
                logger.debug(f"Scanner error: {e}")
            await asyncio.sleep(float(self.env.get("CYCLE_INTERVAL", "15.0")))

    async def _amplifier_loop(self):
        """Update amplifier signals (PCR, sentiment, Binance, Coinbase)."""
        while self._running:
            try:
                await asyncio.gather(
                    self.binance.poll(),
                    self.coinbase.poll(),
                    self.pcr.get_signal(),
                    self.sentiment.update(),
                    return_exceptions=True,
                )
            except Exception as e:
                logger.debug(f"Amplifier error: {e}")
            await asyncio.sleep(10.0)

    async def _heartbeat_loop(self):
        """Write heartbeat to Supabase every 60 seconds. Sync mode from DB."""
        while self._running:
            try:
                # Sync mode from database
                new_mode = await self._read_mode()
                if new_mode != self.mode:
                    logger.info(f"Mode changed: {self.mode} → {new_mode}")
                    self.mode = new_mode
                    self.executor.mode = new_mode

                # Sync wallet balance (live mode only — paper uses config bankroll)
                if self.mode == "live":
                    balance = await self.db.sync_wallet_balance(self.clob)
                    if balance is not None:
                        self.engine.update_bankroll(balance)

                # Write heartbeat
                await self.db.write_heartbeat(
                    mode=self.mode,
                    balance=self.engine.bankroll,
                    cycle_count=self._cycle_count,
                )
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
            await asyncio.sleep(60.0)

    async def _settlement_loop(self):
        """
        Settlement loop — runs every 30 seconds.
        
        3-step process:
          1. resolve_outcomes() — detect which windows closed, determine UP/DOWN
          2. check_settlements() — find trades with outcomes but no P&L yet
          3. settle_trade() — compute won/lost/pnl for each
        
        Paper mode: uses Chainlink window data to determine outcome
        Live mode: same + checks actual CLOB order fill status
        """
        # Wait 60s on startup for Chainlink windows to populate
        await asyncio.sleep(60)
        logger.info("Settlement loop started")

        while self._running:
            try:
                # STEP 1: Detect which markets have resolved
                clob_for_live = self.clob if self.mode == "live" else None
                resolved = await self.db.resolve_outcomes(self.mode, clob_client=clob_for_live)
                if resolved > 0:
                    logger.info(f"Settlement: resolved {resolved} trade(s)")

                # STEP 2: Find trades with outcomes, compute P&L
                ready = await self.db.check_settlements(self.mode)
                for trade in ready:
                    from engine.oracle_engine import compute_settlement, compute_pnl
                    result = compute_settlement(trade["side"], trade["outcome"])
                    pnl = compute_pnl(
                        result["won"], trade["fill_price"],
                        trade["size_usd"], self.engine.taker_fee_rate)

                    # STEP 3: Write result
                    self.engine.record_pnl(pnl["net_pnl"])
                    await self.db.settle_trade(trade["id"], result, pnl, self.mode)

                    # Log + Discord alert
                    emoji = "✅" if result["won"] else "❌"
                    logger.info(
                        f"{emoji} SETTLED: {result['reason']} | "
                        f"P&L=${pnl['net_pnl']:.2f} (gross=${pnl['gross_pnl']:.2f} fee=${pnl['fee']:.2f})"
                    )

                    # Discord webhook for settlement
                    webhook = self.env.get("DISCORD_WEBHOOK_URL")
                    if webhook:
                        try:
                            import httpx
                            msg = (f"{emoji} **SETTLED** {trade['side']} → {trade['outcome']} "
                                   f"| {'WON' if result['won'] else 'LOST'} "
                                   f"| P&L: **${pnl['net_pnl']:.2f}** "
                                   f"| Fill: ${trade['fill_price']:.3f} "
                                   f"| Size: ${trade['size_usd']:.2f}")
                            async with httpx.AsyncClient(timeout=5.0) as client:
                                await client.post(webhook, json={"content": msg})
                        except Exception:
                            pass

            except Exception as e:
                logger.error(f"Settlement error: {e}", exc_info=True)
            await asyncio.sleep(30.0)

    # ── HELPERS ───────────────────────────────────────────

    async def _read_mode(self) -> str:
        """Read mode from Supabase bot_control table. Env var is fallback only."""
        try:
            resp = self.sb.table("bot_control").select("value").eq(
                "key", "mode").limit(1).execute()
            if resp.data:
                return resp.data[0]["value"].lower()
        except Exception as e:
            logger.warning(f"Could not read bot_control: {e}")
        return self.env.get("BOT_MODE", "paper").lower()

    async def _send_webhook(self, url: str, decision, result):
        """Send trade alert to Discord webhook."""
        try:
            import httpx
            emoji = "🟢" if decision.direction == "UP" else "🔴"
            msg = (f"{emoji} **{decision.side} {decision.direction}** | "
                   f"FV={decision.confidence:.3f} fill=${decision.fill_price:.3f} "
                   f"edge={decision.edge_pct:.1f}% | ${decision.size_usd:.2f} | "
                   f"CL {decision.magnitude_pct:+.4f}% {decision.seconds_remaining}s rem")
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url, json={"content": msg})
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# DISCORD SERVICE (separate Railway service)
# ═══════════════════════════════════════════════════════════════

async def run_discord_service():
    """Run Discord bot as a separate service."""
    from engine.discord_bot import DiscordBot
    bot = DiscordBot()
    await bot.run()


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    env = load_env()
    setup_logging(env.get("LOG_LEVEL", "INFO"))

    service = os.environ.get("SERVICE_TYPE", "engine").lower()

    if service == "discord":
        logger.info("Starting Discord service")
        asyncio.run(run_discord_service())
    else:
        logger.info("Starting Engine service")
        bot = OracleBot(env)
        asyncio.run(bot.run())


if __name__ == "__main__":
    main()
