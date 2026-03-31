"""
Oracle Engine v5 Tests
=======================
Tests verify BEHAVIOR, not implementation.
Tests use the SAME path production uses (from verification chain).

Key test categories:
1. Gate behavior — each gate blocks when it should
2. Boundary values — 0, 19, 20, 30, 60, 120, 180, 240, 300
3. Fair value — empirical table lookup
4. Position sizing — Kelly math
5. Settlement — side + outcome → won/lost
"""

import pytest
from engine.oracle_engine import OracleEngine, compute_settlement, compute_pnl
from engine.config import DEFAULT_CONFIG


@pytest.fixture
def engine():
    config = DEFAULT_CONFIG.copy()
    config["bankroll"] = 1000.0
    config["min_edge_pct"] = 3.0
    return OracleEngine(config)


# ═══════════════════════════════════════════════════════════════
# GATE TESTS — verify each gate blocks correctly
# ═══════════════════════════════════════════════════════════════

class TestGate1_OneTradePerWindow:
    def test_blocks_second_trade_same_window(self, engine):
        """First trade passes, second trade in same window is blocked."""
        d1 = engine.evaluate(
            chainlink_open=66000, chainlink_current=66100,
            seconds_remaining=80, best_bid_yes=0.49, best_ask_yes=0.51,
            window_ts=1000)
        d2 = engine.evaluate(
            chainlink_open=66000, chainlink_current=66100,
            seconds_remaining=75, best_bid_yes=0.49, best_ask_yes=0.51,
            window_ts=1000)
        assert "Already traded" in d2.reason

    def test_allows_different_windows(self, engine):
        """Different window_ts should both be allowed."""
        d1 = engine.evaluate(
            chainlink_open=66000, chainlink_current=66100,
            seconds_remaining=80, best_bid_yes=0.49, best_ask_yes=0.51,
            window_ts=1000)
        d2 = engine.evaluate(
            chainlink_open=66000, chainlink_current=66100,
            seconds_remaining=80, best_bid_yes=0.49, best_ask_yes=0.51,
            window_ts=1300)
        # Both should pass gate 1 (may fail later gates)
        assert "Already traded" not in d2.reason


class TestGate2_TooLate:
    def test_blocks_at_19_seconds(self, engine):
        d = engine.evaluate(
            chainlink_open=66000, chainlink_current=66200,
            seconds_remaining=19, best_bid_yes=0.49, best_ask_yes=0.51)
        assert not d.should_trade
        assert "Too late" in d.reason

    def test_blocks_at_0_seconds(self, engine):
        d = engine.evaluate(
            chainlink_open=66000, chainlink_current=66200,
            seconds_remaining=0, best_bid_yes=0.49, best_ask_yes=0.51)
        assert not d.should_trade

    def test_allows_at_20_seconds(self, engine):
        d = engine.evaluate(
            chainlink_open=66000, chainlink_current=66200,
            seconds_remaining=20, best_bid_yes=0.49, best_ask_yes=0.51)
        assert "Too late" not in d.reason  # Passes gate 2


class TestGate3_ChainlinkData:
    def test_blocks_none_open(self, engine):
        d = engine.evaluate(
            chainlink_open=None, chainlink_current=66200,
            seconds_remaining=80, best_bid_yes=0.49, best_ask_yes=0.51)
        assert not d.should_trade
        assert "open price" in d.reason.lower()

    def test_blocks_none_current(self, engine):
        d = engine.evaluate(
            chainlink_open=66000, chainlink_current=None,
            seconds_remaining=80, best_bid_yes=0.49, best_ask_yes=0.51)
        assert not d.should_trade

    def test_blocks_zero_open(self, engine):
        d = engine.evaluate(
            chainlink_open=0, chainlink_current=66200,
            seconds_remaining=80, best_bid_yes=0.49, best_ask_yes=0.51)
        assert not d.should_trade


class TestGate4_Magnitude:
    def test_blocks_tiny_move_early_window(self, engine):
        """0.01% move at 200s remaining should be blocked (needs 0.10%)."""
        d = engine.evaluate(
            chainlink_open=66000, chainlink_current=66006.6,  # +0.01%
            seconds_remaining=200, best_bid_yes=0.49, best_ask_yes=0.51)
        assert not d.should_trade
        assert "Magnitude" in d.reason

    def test_allows_large_move_late_window(self, engine):
        """0.15% move at 80s remaining should pass magnitude gate."""
        d = engine.evaluate(
            chainlink_open=66000, chainlink_current=66099,  # +0.15%
            seconds_remaining=80, best_bid_yes=0.49, best_ask_yes=0.51)
        assert "Magnitude" not in d.reason


class TestGate6_BookData:
    def test_blocks_no_book(self, engine):
        d = engine.evaluate(
            chainlink_open=66000, chainlink_current=66100,
            seconds_remaining=80, best_bid_yes=None, best_ask_yes=None)
        assert not d.should_trade
        assert "book" in d.reason.lower()


# ═══════════════════════════════════════════════════════════════
# BOUNDARY VALUE TESTS
# ═══════════════════════════════════════════════════════════════

class TestBoundaryValues:
    """Test every timing boundary. From verification chain:
    'get_required_magnitude(30) returns 999.0 because of <= vs <'"""

    @pytest.mark.parametrize("secs,should_block", [
        (0, True), (1, True), (19, True),  # Too late
        (20, False), (21, False),           # Just in time
        (29, False), (30, False), (31, False),  # No dead zone at 30
        (59, False), (60, False), (61, False),
        (119, False), (120, False),
        (180, False), (240, False), (299, False), (300, False),
    ])
    def test_no_dead_zones(self, engine, secs, should_block):
        """Large move should only be blocked by Gate 2 (< 20s), never by dead zones."""
        d = engine.evaluate(
            chainlink_open=66000, chainlink_current=66200,  # +0.30% — large move
            seconds_remaining=secs, best_bid_yes=0.49, best_ask_yes=0.51)

        if should_block:
            assert not d.should_trade, f"Should block at {secs}s"
        else:
            # Should pass gate 2 and gate 4 (large move)
            assert "Too late" not in d.reason, f"Dead zone at {secs}s"
            assert "Magnitude" not in d.reason, f"Magnitude block at {secs}s (shouldn't)"


# ═══════════════════════════════════════════════════════════════
# FAIR VALUE TESTS
# ═══════════════════════════════════════════════════════════════

class TestFairValue:
    def test_large_move_late_window_high_confidence(self, engine):
        """0.15% move at 60s remaining should have >90% fair value."""
        fv = engine._compute_fair_value(0.15, 60)
        assert fv >= 0.90

    def test_tiny_move_returns_coinflip(self, engine):
        """<0.02% move should return 0.50 (no edge)."""
        fv = engine._compute_fair_value(0.01, 80)
        assert fv == 0.50

    def test_monotonic_in_magnitude(self, engine):
        """Higher magnitude at same timing should give higher confidence."""
        fv_small = engine._compute_fair_value(0.04, 80)
        fv_large = engine._compute_fair_value(0.12, 80)
        assert fv_large > fv_small


# ═══════════════════════════════════════════════════════════════
# SETTLEMENT TESTS
# ═══════════════════════════════════════════════════════════════

class TestSettlement:
    """From verification chain: v3 had 13.4% inversion bug."""

    def test_yes_up_wins(self):
        r = compute_settlement("YES", "UP")
        assert r["won"] is True

    def test_yes_down_loses(self):
        r = compute_settlement("YES", "DOWN")
        assert r["won"] is False

    def test_no_down_wins(self):
        r = compute_settlement("NO", "DOWN")
        assert r["won"] is True

    def test_no_up_loses(self):
        r = compute_settlement("NO", "UP")
        assert r["won"] is False

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError):
            compute_settlement("MAYBE", "UP")

    def test_invalid_outcome_raises(self):
        with pytest.raises(ValueError):
            compute_settlement("YES", "FLAT")


class TestPnL:
    def test_winning_trade_positive_pnl(self):
        pnl = compute_pnl(won=True, fill_price=0.50, size_usd=10.0)
        assert pnl["net_pnl"] > 0

    def test_losing_trade_negative_pnl(self):
        pnl = compute_pnl(won=False, fill_price=0.50, size_usd=10.0)
        assert pnl["net_pnl"] < 0

    def test_zero_fill_price_returns_zero(self):
        pnl = compute_pnl(won=True, fill_price=0.0, size_usd=10.0)
        assert pnl["net_pnl"] == 0


# ═══════════════════════════════════════════════════════════════
# INTEGRATION TEST — full evaluate path
# ═══════════════════════════════════════════════════════════════

class TestIntegration:
    def test_full_trade_decision(self, engine):
        """Test the complete production path: evaluate → trade decision."""
        d = engine.evaluate(
            chainlink_open=66000.0,
            chainlink_current=66100.0,  # +0.1515% move
            seconds_remaining=80,
            best_bid_yes=0.49,
            best_ask_yes=0.51,
            window_ts=9999,
            ltp=0.52,
            pcr_signal={"direction": "UP", "strength": 0.5},
            sentiment_adjustment=0.01,
            tick_velocity=0.001,
            coinbase_price=66095.0,
        )
        # Should be a trade
        assert d.should_trade
        assert d.direction == "UP"
        assert d.side == "YES"
        assert d.confidence > 0.50
        assert d.fill_price > 0
        assert d.edge_pct > 0
        assert d.size_usd > 0

    def test_down_direction(self, engine):
        """Negative CL move should produce DOWN/NO trade."""
        d = engine.evaluate(
            chainlink_open=66100.0,
            chainlink_current=66000.0,  # -0.1515%
            seconds_remaining=80,
            best_bid_yes=0.49,
            best_ask_yes=0.51,
            window_ts=8888,
        )
        if d.should_trade:
            assert d.direction == "DOWN"
            assert d.side == "NO"
