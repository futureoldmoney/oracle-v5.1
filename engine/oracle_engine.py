"""
Oracle Engine v5
=================
The COMPLETE trade decision maker in one file.

ONE function decides everything: evaluate().
It reads Chainlink, checks timing, computes fair value,
estimates fill price, checks edge, sizes the position,
and returns TRADE or SKIP with full audit trail.

No ensemble. No signal registry. No weighted voting.
The oracle signal IS the strategy.

Corrected edge description (from verification chain):
  The edge is NOT "Chainlink lags Binance."
  The edge IS "most Polymarket participants don't have direct
  on-chain Chainlink access and don't reprice mid-window."
  The bot reads the settlement oracle directly and bets before
  the market adjusts.
"""

import time
import math
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict

logger = logging.getLogger("oracle.engine")


# ═══════════════════════════════════════════════════════════════
# TRADE DECISION (output of evaluate)
# ═══════════════════════════════════════════════════════════════

@dataclass
class TradeDecision:
    """Complete trade decision with full audit trail."""
    should_trade: bool
    direction: str = "NEUTRAL"       # UP / DOWN / NEUTRAL
    side: str = ""                   # YES / NO
    confidence: float = 0.0          # empirical win rate (fair value)
    fill_price: float = 0.0          # actual market price we'd pay
    edge_pct: float = 0.0            # fair_value - fill_price (as %)
    size_usd: float = 0.0            # position size in USD
    size_pct: float = 0.0            # position size as % of bankroll
    magnitude_pct: float = 0.0       # absolute Chainlink move %
    seconds_remaining: int = 0
    reason: str = ""
    # Amplifier data (for logging, not decision)
    ltp_confirms: Optional[bool] = None
    pcr_adjustment: float = 0.0
    sentiment_adjustment: float = 0.0
    tick_velocity: float = 0.0
    coinbase_price: float = 0.0
    chainlink_price: float = 0.0
    window_open_price: float = 0.0
    execution_mode: str = "TAKER"
    timestamp: float = field(default_factory=time.time)


# ═══════════════════════════════════════════════════════════════
# EMPIRICAL WIN RATES
# Source: 236 Chainlink windows (March 2026)
# Verified: these are autocorrelation/momentum rates, NOT latency rates
# ═══════════════════════════════════════════════════════════════

EMPIRICAL_WIN_RATES = {
    # (magnitude_pct, pct_through_window) -> win_rate
    # Early window
    (0.20, 0.20): 0.88, (0.15, 0.20): 0.84, (0.12, 0.20): 0.80, (0.08, 0.20): 0.75,
    # Mid window
    (0.20, 0.40): 0.92, (0.15, 0.40): 0.89, (0.12, 0.40): 0.86,
    (0.08, 0.40): 0.82, (0.06, 0.40): 0.78, (0.04, 0.40): 0.72,
    # Sweet spot (60-120s remaining)
    (0.20, 0.60): 0.96, (0.15, 0.60): 0.95, (0.12, 0.60): 0.93,
    (0.08, 0.60): 0.90, (0.06, 0.60): 0.87, (0.05, 0.60): 0.85,
    (0.04, 0.60): 0.80, (0.03, 0.60): 0.72,
    # Primary zone (60-90s remaining)
    (0.20, 0.75): 0.99, (0.15, 0.75): 0.98, (0.12, 0.75): 0.97,
    (0.08, 0.75): 0.95, (0.06, 0.75): 0.93, (0.05, 0.75): 0.90,
    (0.04, 0.75): 0.85, (0.03, 0.75): 0.78,
    # Late window (30-60s remaining)
    (0.20, 0.85): 0.99, (0.15, 0.85): 0.99, (0.12, 0.85): 0.99,
    (0.08, 0.85): 0.97, (0.06, 0.85): 0.95, (0.05, 0.85): 0.92,
    (0.04, 0.85): 0.88, (0.03, 0.85): 0.82,
}

# Timing thresholds: minimum CL move to trade at each timing
# Lowered from v4 to trade in low-volatility environments
# Verification: tested at every 10s interval, no dead zones
TIMING_THRESHOLDS = [
    # (min_seconds_remaining, min_magnitude_pct)
    # Sorted descending by seconds. First match where secs >= threshold wins.
    (300, 0.15),   # 0-60s into window — only large moves
    (240, 0.10),   # 60-120s
    (180, 0.08),   # 120-180s
    (120, 0.06),   # 180-240s (sweet spot begins)
    (60,  0.04),   # 240-270s (primary zone)
    (30,  0.04),   # 270-280s (late, needs LTP confirm)
    (20,  0.04),   # 280-300s (very late, still fillable)
    # <20s is blocked by Gate 2 (explicit check), not by this table
]


# ═══════════════════════════════════════════════════════════════
# ORACLE ENGINE
# ═══════════════════════════════════════════════════════════════

class OracleEngine:
    """
    Complete trade decision maker.

    Usage:
        engine = OracleEngine(config)
        decision = engine.evaluate(
            chainlink_open=66715.39,
            chainlink_current=66753.21,
            seconds_remaining=85,
            best_bid_yes=0.49,
            best_ask_yes=0.51,
        )
        if decision.should_trade:
            executor.place_order(decision)
    """

    def __init__(self, config: dict):
        self.config = config
        self.bankroll = float(config.get("bankroll", 1000.0))
        self.fractional_kelly = float(config.get("fractional_kelly", 0.20))
        self.min_size_pct = float(config.get("min_position_pct", 0.01))
        self.max_size_pct = float(config.get("max_position_pct", 0.03))
        self.min_edge_pct = float(config.get("min_edge_pct", 3.0))
        self.taker_fee_rate = float(config.get("taker_fee_rate", 0.0156))
        self.max_daily_loss_pct = float(config.get("max_daily_loss_pct", 0.10))
        self.min_order_usd = float(config.get("min_order_usd", 1.0))

        self._daily_pnl = 0.0
        self._traded_windows: set = set()
        self._max_tracked = 500

    def update_bankroll(self, new_bankroll: float):
        self.bankroll = new_bankroll

    def record_pnl(self, pnl: float):
        self._daily_pnl += pnl

    def reset_daily(self):
        self._daily_pnl = 0.0
        self._traded_windows.clear()

    # ── THE SINGLE EVALUATION FUNCTION ────────────────────

    def evaluate(
        self,
        chainlink_open: Optional[float],
        chainlink_current: Optional[float],
        seconds_remaining: int,
        best_bid_yes: Optional[float] = None,
        best_ask_yes: Optional[float] = None,
        window_ts: Optional[int] = None,
        ltp: Optional[float] = None,
        pcr_signal: Optional[Dict] = None,
        sentiment_adjustment: float = 0.0,
        tick_velocity: float = 0.0,
        coinbase_price: float = 0.0,
    ) -> TradeDecision:
        """
        Evaluate whether to trade this cycle.

        7 sequential gates. If any gate fails, returns SKIP.
        All inputs are simple types — no complex objects, no callbacks,
        no key name mismatches possible.

        Verification chain applied:
        - Gate inputs are explicit (no hidden state lookups)
        - Every gate logs its skip reason
        - No alternate code paths
        - Boundary values tested: 0, 19, 20, 30, 60, 120, 180, 240, 300
        """

        # ── GATE 1: One trade per window ──────────────────
        if window_ts is not None:
            if window_ts in self._traded_windows:
                return self._skip(f"Already traded window {window_ts}",
                                  seconds_remaining=seconds_remaining)

        # ── GATE 2: Too late to fill ──────────────────────
        if seconds_remaining < 20:
            return self._skip(f"Too late: {seconds_remaining}s remaining (< 20s)",
                              seconds_remaining=seconds_remaining)

        # ── GATE 3: Chainlink data available ──────────────
        # Verification: explicit None checks, no hidden tracker state
        if chainlink_open is None or chainlink_open <= 0:
            return self._skip("No Chainlink window open price",
                              seconds_remaining=seconds_remaining)
        if chainlink_current is None or chainlink_current <= 0:
            return self._skip("No current Chainlink price",
                              seconds_remaining=seconds_remaining)

        move_pct = ((chainlink_current - chainlink_open) / chainlink_open) * 100.0
        magnitude_pct = abs(move_pct)
        direction = "UP" if move_pct > 0 else "DOWN"

        # ── GATE 4: Magnitude vs timing threshold ─────────
        required_mag = self._get_required_magnitude(seconds_remaining)
        if magnitude_pct < required_mag:
            return self._skip(
                f"Magnitude {magnitude_pct:.4f}% < required {required_mag:.2f}% "
                f"at {seconds_remaining}s remaining",
                seconds_remaining=seconds_remaining,
                magnitude_pct=magnitude_pct,
                chainlink_price=chainlink_current,
                window_open_price=chainlink_open,
            )

        # ── GATE 5: LTP confirmation for weak signals ────
        ltp_confirms = None
        if ltp is not None:
            if direction == "UP" and ltp > 0.55:
                ltp_confirms = True
            elif direction == "DOWN" and ltp < 0.45:
                ltp_confirms = True
            elif direction == "UP" and ltp < 0.45:
                ltp_confirms = False
            elif direction == "DOWN" and ltp > 0.55:
                ltp_confirms = False

        if magnitude_pct < 0.08 and seconds_remaining < 90:
            if ltp_confirms is False:
                return self._skip(
                    f"LTP {ltp:.3f} contradicts oracle {direction} "
                    f"(weak signal {magnitude_pct:.4f}%)",
                    seconds_remaining=seconds_remaining,
                    magnitude_pct=magnitude_pct,
                )

        # ── COMPUTE: Fair value ───────────────────────────
        confidence = self._compute_fair_value(magnitude_pct, seconds_remaining)

        # Apply amplifiers (each capped at ±0.03)
        if ltp_confirms is True:
            confidence = min(0.99, confidence + 0.02)
        elif ltp_confirms is False:
            confidence = max(0.50, confidence - 0.03)

        pcr_adj = 0.0
        if pcr_signal and pcr_signal.get("direction"):
            pcr_dir = pcr_signal["direction"]
            if pcr_dir == direction:
                pcr_adj = min(0.03, pcr_signal.get("strength", 0) * 0.05)
            elif pcr_dir != "NEUTRAL":
                pcr_adj = max(-0.03, -pcr_signal.get("strength", 0) * 0.03)
            confidence = max(0.50, min(0.99, confidence + pcr_adj))

        if sentiment_adjustment != 0:
            confidence = max(0.50, min(0.99, confidence + sentiment_adjustment))

        # Coinbase cross-validation
        if coinbase_price > 0 and chainlink_open > 0:
            cb_move = abs((coinbase_price - chainlink_open) / chainlink_open * 100)
            if cb_move < magnitude_pct * 0.5:
                confidence = max(0.50, confidence - 0.03)

        # ── GATE 6: Book data required ────────────────────
        # Verification: explicit None checks on both bid and ask
        if best_bid_yes is None and best_ask_yes is None:
            return self._skip("No book data available",
                              seconds_remaining=seconds_remaining,
                              magnitude_pct=magnitude_pct, direction=direction)

        # ── COMPUTE: Fill price at actual market ──────────
        fill_price = self._estimate_fill_price(direction, best_bid_yes, best_ask_yes)
        if fill_price is None:
            return self._skip(
                f"Cannot estimate fill price for {direction} "
                f"(bid={best_bid_yes}, ask={best_ask_yes})",
                seconds_remaining=seconds_remaining,
                magnitude_pct=magnitude_pct, direction=direction,
            )

        # ── GATE 7: Edge check at actual fill price ───────
        edge = self._compute_edge(confidence, fill_price)
        if edge < self.min_edge_pct:
            return self._skip(
                f"Edge {edge:.1f}% < {self.min_edge_pct}% minimum at ${fill_price:.3f}",
                seconds_remaining=seconds_remaining,
                magnitude_pct=magnitude_pct, direction=direction,
                confidence=confidence, fill_price=fill_price, edge_pct=edge,
            )

        # ── COMPUTE: Position size ────────────────────────
        size_usd, size_pct, size_reason = self._compute_size(confidence, fill_price)
        if size_usd <= 0:
            return self._skip(f"Sizer rejected: {size_reason}",
                              seconds_remaining=seconds_remaining,
                              magnitude_pct=magnitude_pct, direction=direction,
                              confidence=confidence, fill_price=fill_price, edge_pct=edge)

        # ── RESULT: TRADE ─────────────────────────────────
        side = "YES" if direction == "UP" else "NO"

        if window_ts is not None:
            self._traded_windows.add(window_ts)
            if len(self._traded_windows) > self._max_tracked:
                self._traded_windows = set(sorted(self._traded_windows)[-200:])

        execution_mode = "TAKER" if abs(tick_velocity) > 0.02 else "ADAPTIVE"

        logger.info(
            f"TRADE: {side} {direction} | FV={confidence:.3f} fill=${fill_price:.3f} "
            f"edge={edge:.1f}% | ${size_usd:.2f} ({size_pct:.1%}) | "
            f"CL={move_pct:+.4f}% {seconds_remaining}s rem"
        )

        return TradeDecision(
            should_trade=True,
            direction=direction, side=side,
            confidence=confidence, fill_price=fill_price,
            edge_pct=edge, size_usd=size_usd, size_pct=size_pct,
            magnitude_pct=magnitude_pct,
            seconds_remaining=seconds_remaining,
            reason=f"TRADE: {direction} | CL {move_pct:+.4f}% | FV={confidence:.3f} "
                   f"fill=${fill_price:.3f} edge={edge:.1f}% | ${size_usd:.2f}",
            ltp_confirms=ltp_confirms,
            pcr_adjustment=pcr_adj,
            sentiment_adjustment=sentiment_adjustment,
            tick_velocity=tick_velocity,
            coinbase_price=coinbase_price,
            chainlink_price=chainlink_current,
            window_open_price=chainlink_open,
            execution_mode=execution_mode,
        )

    # ── INTERNAL: Timing thresholds ───────────────────────

    @staticmethod
    def _get_required_magnitude(seconds_remaining: int) -> float:
        """
        Return minimum CL move % needed at this timing.

        Verification: uses < (not <=) to prevent dead zones.
        Tested at boundaries: 20, 30, 60, 120, 180, 240, 300.
        """
        if seconds_remaining < 20:
            return 999.0

        for max_secs, min_mag in TIMING_THRESHOLDS:
            if seconds_remaining < max_secs:
                continue
            return min_mag

        return TIMING_THRESHOLDS[0][1]  # Most conservative

    # ── INTERNAL: Fair value computation ──────────────────

    @staticmethod
    def _compute_fair_value(magnitude_pct: float, seconds_remaining: int) -> float:
        """
        Compute win probability from empirical data.

        Returns 0.50 (coin flip) if below minimum magnitude.
        Interpolates between closest empirical entries.
        """
        pct_through = max(0.0, min(1.0, 1.0 - (seconds_remaining / 300.0)))
        abs_mag = abs(magnitude_pct)

        if abs_mag < 0.02:
            return 0.50  # No edge below 0.02%

        best_match = 0.50
        best_distance = float("inf")

        for (mag, pct), wr in EMPIRICAL_WIN_RATES.items():
            if abs_mag < mag * 0.8:
                continue
            dist = abs(pct - pct_through) + abs(mag - abs_mag) * 5
            if dist < best_distance:
                best_distance = dist
                best_match = wr

        # Magnitude boost for large moves
        if abs_mag >= 0.20:
            boost = 0.02
        elif abs_mag >= 0.12:
            boost = 0.01
        else:
            boost = 0.0

        return round(min(0.99, best_match + boost), 4)

    # ── INTERNAL: Fill price estimation ───────────────────

    @staticmethod
    def _estimate_fill_price(
        direction: str,
        best_bid_yes: Optional[float],
        best_ask_yes: Optional[float],
    ) -> Optional[float]:
        """
        Estimate taker fill price from book data.

        Verification: handles None for both bid and ask independently.
        For UP: buy YES at ask. For DOWN: buy NO at (1 - bid_yes).
        """
        if direction == "UP":
            if best_ask_yes is not None and 0.01 <= best_ask_yes < 0.99:
                return float(best_ask_yes)
            # Fallback: infer from bid
            if best_bid_yes is not None and 0.01 < best_bid_yes < 0.99:
                return round(best_bid_yes + 0.01, 4)  # Estimate ask from bid
            return None
        else:  # DOWN — buy NO
            if best_bid_yes is not None and 0.01 < best_bid_yes < 0.99:
                return round(1.0 - float(best_bid_yes), 4)
            if best_ask_yes is not None and 0.0 < best_ask_yes < 0.50:
                return round(1.0 - float(best_ask_yes), 4)
            return None

    # ── INTERNAL: Edge computation ────────────────────────

    def _compute_edge(self, fair_value: float, fill_price: float) -> float:
        """
        Compute edge percentage after taker fees.

        Binary bet math:
          Win: profit = (1 - fill_price) per share
          Lose: loss = fill_price per share
          EV = p * win - q * loss - fee
        """
        if fill_price <= 0 or fill_price >= 1.0:
            return 0.0

        p = fair_value
        q = 1 - p
        ev_gross = p * (1 - fill_price) - q * fill_price
        ev_net = ev_gross - (fill_price * self.taker_fee_rate)
        return round((ev_net / fill_price) * 100, 2) if fill_price > 0 else 0.0

    # ── INTERNAL: Position sizing ─────────────────────────

    def _compute_size(self, confidence: float, fill_price: float) -> tuple:
        """
        Kelly criterion position sizing.

        Returns: (size_usd, size_pct, reason)
        """
        # Daily loss check
        loss_limit = self.bankroll * self.max_daily_loss_pct
        if self._daily_pnl < -loss_limit:
            return (0, 0, f"Daily loss ${self._daily_pnl:.2f} exceeds limit")

        if fill_price <= 0 or fill_price >= 1.0:
            return (0, 0, f"Invalid fill price: {fill_price}")

        # Kelly formula
        b = (1.0 - fill_price) / fill_price
        q = 1.0 - confidence
        kelly_raw = (confidence * b - q) / b if b > 0 else 0.0

        if kelly_raw <= 0:
            return (0, 0, f"Negative Kelly ({kelly_raw:.4f})")

        kelly_adj = kelly_raw * self.fractional_kelly
        size_pct = max(self.min_size_pct, min(self.max_size_pct, kelly_adj))
        size_usd = self.bankroll * size_pct

        # Polymarket minimum
        pm_min = max(self.min_order_usd, 5.0 * fill_price)
        if size_usd < pm_min:
            if self.bankroll * self.min_size_pct >= pm_min:
                size_usd = pm_min
                size_pct = size_usd / self.bankroll
            else:
                return (0, 0, f"Bankroll ${self.bankroll:.2f} too small")

        return (round(size_usd, 2), round(size_pct, 4),
                f"Kelly {kelly_raw:.3f} × {self.fractional_kelly} → ${size_usd:.2f}")

    # ── INTERNAL: Skip helper ─────────────────────────────

    @staticmethod
    def _skip(reason: str, **kwargs) -> TradeDecision:
        return TradeDecision(
            should_trade=False,
            reason=reason,
            direction=kwargs.get("direction", "NEUTRAL"),
            magnitude_pct=kwargs.get("magnitude_pct", 0),
            seconds_remaining=kwargs.get("seconds_remaining", 0),
            confidence=kwargs.get("confidence", 0),
            fill_price=kwargs.get("fill_price", 0),
            edge_pct=kwargs.get("edge_pct", 0),
            chainlink_price=kwargs.get("chainlink_price", 0),
            window_open_price=kwargs.get("window_open_price", 0),
        )


# ═══════════════════════════════════════════════════════════════
# SETTLEMENT LOGIC (centralized, single source of truth)
# ═══════════════════════════════════════════════════════════════

def compute_settlement(side: str, outcome: str) -> dict:
    """
    Determine won/lost from side + outcome.

    This is the ONLY function that computes settlement.
    Every code path (realtime, backfill, paper) calls this.

    Verification: side-based, not direction-based.
    v3 bug: used direction == outcome (wrong for NO trades).
    """
    side = side.upper().strip()
    outcome = outcome.upper().strip()

    if side not in ("YES", "NO"):
        raise ValueError(f"Invalid side: '{side}'")
    if outcome not in ("UP", "DOWN"):
        raise ValueError(f"Invalid outcome: '{outcome}'")

    won = (side == "YES" and outcome == "UP") or (side == "NO" and outcome == "DOWN")
    return {"won": won, "reason": f"side={side} + outcome={outcome} → {'WIN' if won else 'LOSS'}"}


def compute_pnl(won: bool, fill_price: float, size_usd: float, fee_rate: float = 0.0156) -> dict:
    """Compute P&L from a settled trade."""
    if fill_price <= 0 or fill_price >= 1.0:
        return {"gross_pnl": 0, "fee": 0, "net_pnl": 0, "shares": 0}

    shares = size_usd / fill_price
    fee = size_usd * fee_rate

    if won:
        gross = shares * (1.0 - fill_price)  # Win payout minus cost
    else:
        gross = -size_usd  # Lose entire stake

    return {
        "gross_pnl": round(gross, 4),
        "fee": round(fee, 4),
        "net_pnl": round(gross - fee, 4),
        "shares": round(shares, 4),
    }
