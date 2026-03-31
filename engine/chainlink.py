"""
Chainlink Window Tracker v5
=============================
Tracks BTC/USD price at 5-minute window boundaries using
Chainlink's on-chain oracle on Polygon.

Simplified from v4:
- No callback-based updates (caller passes price directly)
- get_state() returns all data as simple dict (no method-missing bugs)
- Window transitions logged explicitly
- Supabase writes are fire-and-forget

Contract: 0xc907E116054Ad103354f2D350FD2514433D57F6f
Method: latestRoundData() → answer / 1e8 = BTC price
"""

import time
import logging
from typing import Optional, Dict, List
from collections import deque

logger = logging.getLogger("oracle.chainlink")

WINDOW_SECONDS = 300  # 5-minute windows
CHAINLINK_BTC_USD = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
CHAINLINK_ABI = [{
    "inputs": [], "name": "latestRoundData",
    "outputs": [
        {"name": "roundId", "type": "uint80"},
        {"name": "answer", "type": "int256"},
        {"name": "startedAt", "type": "uint256"},
        {"name": "updatedAt", "type": "uint256"},
        {"name": "answeredInRound", "type": "uint80"},
    ],
    "stateMutability": "view", "type": "function",
}]


class ChainlinkTracker:
    """
    Tracks Chainlink BTC/USD price at 5-minute window boundaries.

    Usage:
        tracker = ChainlinkTracker()
        tracker.update(67234.50)  # call every 3 seconds with on-chain price
        state = tracker.get_state()
        # state = {"open_price": 67200.0, "current_price": 67234.50,
        #          "move_pct": 0.0513, "window_ts": 1774883100, "tick_count": 42}
    """

    def __init__(self, supabase_client=None):
        self._supabase = supabase_client
        self._window_ts: int = 0
        self._open_price: float = 0.0
        self._current_price: float = 0.0
        self._high: float = 0.0
        self._low: float = float('inf')
        self._tick_count: int = 0
        self._price_path: List[Dict] = []
        self._last_sample_ts: float = 0.0
        self._history: deque = deque(maxlen=50)

    @staticmethod
    def _get_window_ts(ts: Optional[float] = None) -> int:
        now = int(ts or time.time())
        return (now // WINDOW_SECONDS) * WINDOW_SECONDS

    def update(self, price: float):
        """
        Feed current Chainlink price. Call every cycle (~3 seconds).
        Automatically detects window transitions and captures open price.
        """
        if price <= 0:
            return

        self._current_price = price
        window_ts = self._get_window_ts()

        if window_ts != self._window_ts:
            # Window transition — save old, start new
            if self._open_price > 0 and self._window_ts > 0:
                self._save_completed_window(price)

            self._window_ts = window_ts
            self._open_price = price
            self._high = price
            self._low = price
            self._tick_count = 0
            self._price_path = [{"t": int(time.time()), "p": round(price, 2)}]
            self._last_sample_ts = time.time()
            logger.info(f"ChainlinkWindow: New window {window_ts} | open=${price:,.2f}")
        else:
            self._high = max(self._high, price)
            self._low = min(self._low, price)
            self._tick_count += 1
            now = time.time()
            if now - self._last_sample_ts >= 30:
                self._price_path.append({"t": int(now), "p": round(price, 2)})
                self._last_sample_ts = now

    def get_state(self) -> Optional[Dict]:
        """
        Return current window state as a simple dict.

        Returns None if no window data available.
        This replaces v4's get_current_move() + window_open_price property.
        Verification: single return type, no method-missing possible.
        """
        if self._open_price <= 0 or self._current_price <= 0:
            return None

        move_pct = ((self._current_price - self._open_price) / self._open_price) * 100.0

        return {
            "open_price": self._open_price,
            "current_price": self._current_price,
            "move_pct": move_pct,
            "window_ts": self._window_ts,
            "tick_count": self._tick_count,
            "high": self._high,
            "low": self._low if self._low != float('inf') else self._open_price,
        }

    def _save_completed_window(self, close_price: float):
        """Write completed window to Supabase and local history."""
        move_pct = ((close_price - self._open_price) / self._open_price) * 100.0
        direction = "UP" if move_pct > 0.01 else "DOWN" if move_pct < -0.01 else "NEUTRAL"

        record = {
            "window_ts": self._window_ts,
            "open_price": round(self._open_price, 2),
            "close_price": round(close_price, 2),
            "high_price": round(self._high, 2),
            "low_price": round(self._low if self._low != float('inf') else self._open_price, 2),
            "price_move_pct": round(move_pct, 6),
            "direction": direction,
            "tick_count": self._tick_count,
            "price_path": self._price_path,
        }
        self._history.append(record)

        if self._supabase:
            try:
                from datetime import datetime, timezone
                row = {**record, "window_start": datetime.fromtimestamp(
                    record["window_ts"], tz=timezone.utc).isoformat()}
                self._supabase.table("chainlink_windows").upsert(
                    row, on_conflict="window_ts").execute()
            except Exception as e:
                logger.error(f"ChainlinkWindow: Supabase write failed: {e}")


async def chainlink_poller(tracker: ChainlinkTracker, rpc_url: str, interval: float = 3.0):
    """
    Async poller that reads Chainlink BTC/USD from Polygon every N seconds.
    Feeds prices into the tracker.
    """
    import asyncio
    try:
        from web3 import Web3
    except ImportError:
        logger.error("web3 not installed — Chainlink poller disabled")
        return

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(CHAINLINK_BTC_USD), abi=CHAINLINK_ABI)

    logger.info(f"Chainlink poller started (contract={CHAINLINK_BTC_USD[:12]}...)")

    while True:
        try:
            round_data = contract.functions.latestRoundData().call()
            price = round_data[1] / 1e8
            tracker.update(price)
        except Exception as e:
            logger.debug(f"Chainlink poller error: {e}")
        await asyncio.sleep(interval)
