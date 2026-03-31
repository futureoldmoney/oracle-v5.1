"""
Order Executor v5
==================
Taker-first execution via py-clob-client.

Simplified from v4:
- No maker/taker mode switching (taker always for 5-min markets)
- No rate limiter class (simple timestamp check)
- Paper mode returns simulated fills
- Entry price cap: reject trades above $0.65

Verification chain applied:
- MAX_ENTRY_PRICE prevents catastrophic fills
- Paper mode uses actual market prices for realistic simulation
- All errors logged with full context
"""

import os
import time
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("oracle.executor")

MAX_ENTRY_PRICE = 0.65  # Reject trades above this — almost no margin left
MIN_ORDER_SIZE = 5.0     # Polymarket minimum shares


@dataclass
class ExecutionResult:
    success: bool
    order_id: str = ""
    fill_price: float = 0.0
    fill_shares: float = 0.0
    error: str = ""
    execution_mode: str = "TAKER"


class OrderExecutor:
    """
    Places taker orders on Polymarket CLOB.

    Usage:
        executor = OrderExecutor(clob_client, mode="paper")
        result = executor.place_order(decision, token_id, market_id)
    """

    def __init__(self, clob_client, mode: str = "paper"):
        self._client = clob_client
        self.mode = mode
        self._last_order_ts: float = 0.0
        self._min_interval: float = 2.0  # Min 2s between orders

    async def place_order(self, decision, token_id: str, market_id: str) -> ExecutionResult:
        """
        Place a taker order.

        Args:
            decision: TradeDecision from OracleEngine
            token_id: YES or NO token ID
            market_id: Condition ID
        """
        # Rate limit
        now = time.time()
        if now - self._last_order_ts < self._min_interval:
            return ExecutionResult(success=False, error="Rate limited")
        self._last_order_ts = now

        # Entry price cap (verification chain: worst-trade check)
        price = round(float(decision.fill_price), 2)
        if price > MAX_ENTRY_PRICE:
            return ExecutionResult(
                success=False,
                error=f"Price ${price:.2f} > max ${MAX_ENTRY_PRICE:.2f}")

        if price <= 0.01 or price >= 0.99:
            return ExecutionResult(
                success=False, error=f"Invalid price: ${price:.2f}")

        # Compute shares
        size_usd = float(decision.size_usd)
        shares = size_usd / price
        if shares < MIN_ORDER_SIZE:
            shares = MIN_ORDER_SIZE
            size_usd = shares * price

        # Paper mode: simulate fill
        if self.mode == "paper":
            logger.info(f"PAPER TRADE: {decision.side} {shares:.1f} shares @ ${price:.3f} "
                        f"= ${size_usd:.2f}")
            return ExecutionResult(
                success=True,
                order_id=f"paper-{int(now)}",
                fill_price=price,
                fill_shares=round(shares, 2),
                execution_mode="PAPER",
            )

        # Live mode: place taker order
        try:
            from py_clob_client.order_builder.constants import BUY
            order_args = {
                "token_id": token_id,
                "price": price,
                "size": round(shares, 2),
                "side": BUY,
            }

            signed = self._client.create_and_post_order(order_args)

            order_id = ""
            if isinstance(signed, dict):
                order_id = signed.get("orderID", signed.get("id", ""))
            elif hasattr(signed, "orderID"):
                order_id = signed.orderID

            logger.info(f"LIVE ORDER: {decision.side} {shares:.1f}sh @ ${price:.3f} "
                        f"→ {order_id}")

            return ExecutionResult(
                success=True,
                order_id=str(order_id),
                fill_price=price,
                fill_shares=round(shares, 2),
                execution_mode="TAKER",
            )

        except Exception as e:
            logger.error(f"Order failed: {e}", exc_info=True)
            return ExecutionResult(success=False, error=str(e))
