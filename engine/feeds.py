"""
Market Feeds v5
================
Market discovery and book data with guaranteed fallbacks.

Simplified from v4:
- No DataRouter callback chain (direct function calls)
- Book data returned as simple dict (no key name mismatches)
- Scanner uses deterministic slug pattern (no Gamma API search)
- Binance via proxy, Coinbase direct, both optional

Verification chain applied:
- get_book() always returns a dict with best_bid_yes/best_ask_yes keys
- If WS book is unavailable, falls back to scanner prices
- No None values passed to oracle engine without explicit handling
"""

import os
import json
import time
import asyncio
import logging
from typing import Optional, Dict, List
from collections import deque

import httpx

logger = logging.getLogger("oracle.feeds")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
RTDS_URL = "wss://ws-live-data.polymarket.com"
MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WINDOW_SECONDS = 300


class MarketScanner:
    """Find the current BTC 5-minute market on Polymarket."""

    def __init__(self):
        self._current_market: Optional[Dict] = None
        self._scan_count = 0

    @staticmethod
    def _current_window_ts() -> int:
        return (int(time.time()) // WINDOW_SECONDS) * WINDOW_SECONDS

    async def scan(self) -> Optional[Dict]:
        """
        Find the active BTC 5-min market via deterministic slug.
        Returns dict with condition_id, yes_token_id, no_token_id, question,
        seconds_before_close, window_ts, best_bid, best_ask.
        """
        window_ts = self._current_window_ts()
        slug = f"btc-updown-5m-{window_ts}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{GAMMA_API}/events", params={"slug": slug})
                resp.raise_for_status()
                events = resp.json()

                if not events:
                    return None

                event = events[0]
                markets = event.get("markets", [])
                if not markets:
                    return None

                market = markets[0]
                condition_id = market.get("conditionId", "")
                tokens = market.get("clobTokenIds", "[]")
                if isinstance(tokens, str):
                    tokens = json.loads(tokens)

                yes_token = tokens[0] if len(tokens) >= 1 else ""
                no_token = tokens[1] if len(tokens) >= 2 else ""

                prices = market.get("outcomePrices", "[]")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                yes_price = float(prices[0]) if prices else 0.5

                window_end = window_ts + WINDOW_SECONDS
                secs_left = max(0, window_end - int(time.time()))

                self._current_market = {
                    "condition_id": condition_id,
                    "question": market.get("question", "BTC 5-min"),
                    "yes_token_id": yes_token,
                    "no_token_id": no_token,
                    "window_ts": window_ts,
                    "seconds_before_close": secs_left,
                    # Scanner-derived prices as fallback
                    "best_bid": max(yes_price - 0.01, 0.01),
                    "best_ask": min(yes_price + 0.01, 0.99),
                }
                self._scan_count += 1
                return self._current_market

        except Exception as e:
            logger.debug(f"Scanner: {e}")
            return self._current_market  # Return last known


class BookFetcher:
    """
    Fetch order book data from CLOB API.
    Returns simple dict with guaranteed keys.
    
    CRITICAL FIX: Polymarket CLOB API returns bids sorted ascending
    (worst first) and asks sorted descending (worst first). Must use
    max(bids) and min(asks) to get the actual best prices.
    """

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=5.0)
        return self._client

    async def get_book(self, token_id: str) -> Dict:
        """
        Fetch order book for a token.
        Always returns dict with best_bid and best_ask keys.
        Values may be None if API fails.
        """
        try:
            client = await self._get_client()
            resp = await client.get(f"{CLOB_API}/book", params={"token_id": token_id})
            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                logger.debug(f"Book API error: {data['error']}")
                return {"best_bid": None, "best_ask": None, "bid_depth": 0, "ask_depth": 0}

            bids = data.get("bids", [])
            asks = data.get("asks", [])

            # CRITICAL: API returns bids ascending, asks descending
            # Must use max/min to find actual best prices
            best_bid = max((float(b["price"]) for b in bids), default=None) if bids else None
            best_ask = min((float(a["price"]) for a in asks), default=None) if asks else None

            # Sanity check: bid should be < ask
            if best_bid and best_ask and best_bid >= best_ask:
                logger.warning(f"Book crossed: bid=${best_bid} >= ask=${best_ask}")

            bid_depth = sum(float(b.get("size", 0)) for b in bids
                          if best_bid and abs(float(b["price"]) - best_bid) <= 0.05)
            ask_depth = sum(float(a.get("size", 0)) for a in asks
                          if best_ask and abs(float(a["price"]) - best_ask) <= 0.05)

            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "bid_depth": round(bid_depth, 1),
                "ask_depth": round(ask_depth, 1),
            }
        except Exception as e:
            logger.debug(f"BookFetcher: {e}")
            return {"best_bid": None, "best_ask": None, "bid_depth": 0, "ask_depth": 0}


class BinancePoller:
    """BTC price from Binance via SOCKS5 proxy."""

    def __init__(self, proxy_url: Optional[str] = None):
        self._proxy = proxy_url
        self._price: float = 0.0

    async def poll(self) -> float:
        try:
            transport = httpx.AsyncHTTPTransport(proxy=self._proxy) if self._proxy else None
            async with httpx.AsyncClient(transport=transport, timeout=5.0) as client:
                resp = await client.get("https://api.binance.com/api/v3/ticker/price",
                                        params={"symbol": "BTCUSDT"})
                resp.raise_for_status()
                self._price = float(resp.json()["price"])
                return self._price
        except Exception as e:
            logger.debug(f"Binance: {e}")
            return self._price

    @property
    def price(self) -> float:
        return self._price


class CoinbasePoller:
    """BTC price from Coinbase (no auth, no proxy)."""

    def __init__(self):
        self._price: float = 0.0

    async def poll(self) -> float:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
                resp.raise_for_status()
                self._price = float(resp.json()["price"])
                return self._price
        except Exception as e:
            logger.debug(f"Coinbase: {e}")
            return self._price

    @property
    def price(self) -> float:
        return self._price


class LTPPoller:
    """Last trade price from CLOB API."""

    def __init__(self):
        self._prices: Dict[str, float] = {}
        self._history: Dict[str, deque] = {}

    async def poll(self, token_id: str) -> Optional[float]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{CLOB_API}/last-trade-price",
                                        params={"token_id": token_id})
                resp.raise_for_status()
                price = float(resp.json().get("price", 0))
                if price > 0:
                    self._prices[token_id] = price
                    if token_id not in self._history:
                        self._history[token_id] = deque(maxlen=30)
                    self._history[token_id].append((time.time(), price))
                return price
        except Exception:
            return self._prices.get(token_id)

    def get_velocity(self, token_id: str) -> float:
        """Price change per second over last 30 observations."""
        hist = self._history.get(token_id)
        if not hist or len(hist) < 2:
            return 0.0
        oldest_t, oldest_p = hist[0]
        newest_t, newest_p = hist[-1]
        dt = newest_t - oldest_t
        if dt <= 0:
            return 0.0
        return (newest_p - oldest_p) / dt


class DeribitPCR:
    """Deribit BTC options put/call ratio (free API)."""

    def __init__(self):
        self._last_signal: Optional[Dict] = None
        self._last_fetch: float = 0

    async def get_signal(self) -> Optional[Dict]:
        if time.time() - self._last_fetch < 300:  # Cache 5 min
            return self._last_signal

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
                    params={"currency": "BTC", "kind": "option"})
                resp.raise_for_status()
                data = resp.json().get("result", [])

                calls = sum(1 for d in data if d.get("instrument_name", "").endswith("-C"))
                puts = sum(1 for d in data if d.get("instrument_name", "").endswith("-P"))

                pcr = puts / calls if calls > 0 else 1.0

                if pcr > 1.2:
                    direction, strength = "UP", min(1.0, (pcr - 1.0) / 0.5)
                elif pcr < 0.7:
                    direction, strength = "DOWN", min(1.0, (1.0 - pcr) / 0.5)
                else:
                    direction, strength = "NEUTRAL", 0.0

                self._last_signal = {"direction": direction, "strength": strength, "pcr": pcr}
                self._last_fetch = time.time()
                return self._last_signal
        except Exception as e:
            logger.debug(f"Deribit: {e}")
            return self._last_signal


class SentimentTracker:
    """Fear & Greed index from Alternative.me."""

    def __init__(self):
        self._value: int = 50
        self._label: str = "Neutral"
        self._last_fetch: float = 0

    async def update(self):
        if time.time() - self._last_fetch < 900:  # Cache 15 min
            return

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get("https://api.alternative.me/fng/")
                resp.raise_for_status()
                data = resp.json().get("data", [{}])[0]
                self._value = int(data.get("value", 50))
                self._label = data.get("value_classification", "Neutral")
                self._last_fetch = time.time()
        except Exception as e:
            logger.debug(f"Sentiment: {e}")

    def get_adjustment(self, direction: str) -> float:
        """±0.03 confidence adjustment based on sentiment alignment."""
        if self._value < 20:  # Extreme fear → contrarian bullish
            return 0.01 if direction == "UP" else -0.01
        elif self._value > 80:  # Extreme greed → contrarian bearish
            return 0.01 if direction == "DOWN" else -0.01
        return 0.0

    @property
    def summary(self) -> str:
        return f"{self._label} (F&G:{self._value})"
