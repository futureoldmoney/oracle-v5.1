"""Oracle Bot v5 Backtest — Fixed sizing, realistic fills, no compounding overflow."""
import pandas as pd
import numpy as np
from collections import defaultdict

TIMING_THRESHOLDS = [
    (300, 0.15), (240, 0.10), (180, 0.08), (120, 0.06),
    (60, 0.04), (30, 0.04), (20, 0.04),
]
EMPIRICAL_WIN_RATES = {
    (0.20, 0.20): 0.88, (0.15, 0.20): 0.84, (0.12, 0.20): 0.80, (0.08, 0.20): 0.75,
    (0.20, 0.40): 0.92, (0.15, 0.40): 0.89, (0.12, 0.40): 0.86,
    (0.08, 0.40): 0.82, (0.06, 0.40): 0.78, (0.04, 0.40): 0.72,
    (0.20, 0.60): 0.96, (0.15, 0.60): 0.95, (0.12, 0.60): 0.93,
    (0.08, 0.60): 0.90, (0.06, 0.60): 0.87, (0.05, 0.60): 0.85,
    (0.04, 0.60): 0.80, (0.03, 0.60): 0.72,
    (0.20, 0.75): 0.99, (0.15, 0.75): 0.98, (0.12, 0.75): 0.97,
    (0.08, 0.75): 0.95, (0.06, 0.75): 0.93, (0.05, 0.75): 0.90,
    (0.04, 0.75): 0.85, (0.03, 0.75): 0.78,
    (0.20, 0.85): 0.99, (0.15, 0.85): 0.99, (0.12, 0.85): 0.99,
    (0.08, 0.85): 0.97, (0.06, 0.85): 0.95, (0.05, 0.85): 0.92,
    (0.04, 0.85): 0.88, (0.03, 0.85): 0.82,
}
TAKER_FEE = 0.0156
MIN_EDGE_PCT = 3.0
FLAT_SIZE = 10.0  # $10 per trade (no compounding)

# Realistic fill prices: larger moves = worse fills (market reprices)
def realistic_fill_price(magnitude_pct, seconds_remaining):
    """Market partly reprices based on move size. Bigger moves = higher fill."""
    base = 0.50
    # Market reprices: 20-40% of the move gets priced in
    reprice_factor = 0.25 if seconds_remaining > 120 else 0.35
    adjustment = magnitude_pct * reprice_factor * 10  # Convert to price units
    return min(0.65, base + adjustment)

def get_required_magnitude(secs):
    if secs < 20: return 999.0
    for max_secs, min_mag in TIMING_THRESHOLDS:
        if secs < max_secs: continue
        return min_mag
    return TIMING_THRESHOLDS[0][1]

def compute_fair_value(mag, secs):
    pct_through = max(0.0, min(1.0, 1.0 - (secs / 300.0)))
    if abs(mag) < 0.02: return 0.50
    best, best_d = 0.50, float("inf")
    for (m, p), wr in EMPIRICAL_WIN_RATES.items():
        if abs(mag) < m * 0.8: continue
        d = abs(p - pct_through) + abs(m - abs(mag)) * 5
        if d < best_d: best_d, best = d, wr
    if abs(mag) >= 0.20: best = min(0.99, best + 0.02)
    elif abs(mag) >= 0.12: best = min(0.99, best + 0.01)
    return round(best, 4)

print("Loading data...")
df = pd.read_csv("/home/claude/btc_1min.csv")
print(f"Loaded {len(df):,} candles | {pd.Timestamp(df.timestamp.min(), unit='s').strftime('%Y-%m-%d')} → {pd.Timestamp(df.timestamp.max(), unit='s').strftime('%Y-%m-%d')}")

# Build 5-min windows
df["window_ts"] = (df["timestamp"] // 300) * 300
df["offset"] = df["timestamp"] - df["window_ts"]

windows = df.groupby("window_ts").agg(
    open_price=("open", "first"), close_price=("close", "last"),
    high=("high", "max"), low=("low", "min"), n=("timestamp", "count"),
).reset_index()
windows = windows[windows.n == 5].copy()
print(f"Complete 5-min windows: {len(windows):,}")

# Get minute prices per window
minute_prices = {}
for wt, grp in df.groupby("window_ts"):
    prices = {int(r["offset"]): r["close"] for _, r in grp.iterrows()}
    if len(prices) >= 5: minute_prices[wt] = prices

# SIMULATE
trades = []
EVAL_POINTS = [60, 120, 180, 240, 270]

for _, w in windows.iterrows():
    wt = int(w.window_ts)
    op, cp = float(w.open_price), float(w.close_price)
    if op <= 0 or wt not in minute_prices: continue

    actual_dir = "UP" if cp > op else "DOWN"
    prices = minute_prices[wt]
    traded = False

    for eval_off in EVAL_POINTS:
        if traded: break
        closest = min(prices.keys(), key=lambda x: abs(x - eval_off))
        if abs(closest - eval_off) > 30: continue
        cur = prices[closest]
        secs_rem = 300 - eval_off
        move_pct = ((cur - op) / op) * 100.0
        mag = abs(move_pct)

        if mag < get_required_magnitude(secs_rem): continue
        fv = compute_fair_value(mag, secs_rem)
        fill = realistic_fill_price(mag, secs_rem)

        # Edge check
        p, q = fv, 1 - fv
        ev_net = p * (1 - fill) - q * fill - fill * TAKER_FEE
        edge = (ev_net / fill) * 100 if fill > 0 else 0
        if edge < MIN_EDGE_PCT: continue

        # Trade!
        traded = True
        direction = "UP" if move_pct > 0 else "DOWN"
        won = (direction == actual_dir)
        shares = FLAT_SIZE / fill
        fee = FLAT_SIZE * TAKER_FEE
        pnl = (shares * (1 - fill) - fee) if won else (-FLAT_SIZE - fee)

        trades.append({
            "window_ts": wt, "direction": direction, "actual": actual_dir,
            "won": won, "mag": mag, "secs_rem": secs_rem, "fv": fv,
            "fill": fill, "edge": edge, "pnl": pnl,
            "month": pd.Timestamp(wt, unit='s').strftime("%Y-%m"),
        })

# RESULTS
print("\n" + "=" * 70)
print("ORACLE BOT v5 BACKTEST — REALISTIC FILLS, FLAT $10 SIZING")
print("=" * 70)

tdf = pd.DataFrame(trades)
n = len(tdf)
wins = tdf.won.sum()
total_pnl = tdf.pnl.sum()

print(f"\nWindows:      {len(windows):,}")
print(f"Trades:       {n:,} ({n/len(windows)*100:.1f}% of windows)")
print(f"Win rate:     {wins}/{n} = {wins/n*100:.1f}%")
print(f"Total P&L:    ${total_pnl:,.2f} (flat $10/trade)")
print(f"Avg P&L/trade: ${total_pnl/n:.4f}")
print(f"Avg fill:     ${tdf.fill.mean():.3f}")
print(f"Avg edge:     {tdf.edge.mean():.1f}%")

print(f"\n{'─'*70}")
print("BY MAGNITUDE")
print(f"{'─'*70}")
for lo, hi, label in [(0.03,0.05,"0.03-0.05%"), (0.05,0.08,"0.05-0.08%"),
                       (0.08,0.12,"0.08-0.12%"), (0.12,0.20,"0.12-0.20%"), (0.20,99,">0.20%")]:
    sub = tdf[(tdf.mag >= lo) & (tdf.mag < hi)]
    if len(sub):
        print(f"  {label:>12s}: {sub.won.sum()}/{len(sub)} = {sub.won.mean()*100:.1f}% | "
              f"avg fill=${sub.fill.mean():.3f} | P&L=${sub.pnl.sum():,.2f}")

print(f"\n{'─'*70}")
print("BY TIMING")
print(f"{'─'*70}")
for lo, hi in [(20,60), (60,120), (120,180), (180,240), (240,300)]:
    sub = tdf[(tdf.secs_rem >= lo) & (tdf.secs_rem < hi)]
    if len(sub):
        print(f"  {lo:3d}-{hi:3d}s: {sub.won.sum()}/{len(sub)} = {sub.won.mean()*100:.1f}% | "
              f"P&L=${sub.pnl.sum():,.2f}")

print(f"\n{'─'*70}")
print("BY DIRECTION")
print(f"{'─'*70}")
for d in ["UP", "DOWN"]:
    sub = tdf[tdf.direction == d]
    print(f"  {d:>5s}: {sub.won.sum()}/{len(sub)} = {sub.won.mean()*100:.1f}% | P&L=${sub.pnl.sum():,.2f}")

print(f"\n{'─'*70}")
print("MONTHLY BREAKDOWN")
print(f"{'─'*70}")
for month in sorted(tdf.month.unique()):
    sub = tdf[tdf.month == month]
    print(f"  {month}: {len(sub):4d} trades | WR={sub.won.mean()*100:.1f}% | P&L=${sub.pnl.sum():,.2f}")

print(f"\n{'─'*70}")
print("STATISTICAL SIGNIFICANCE")
print(f"{'─'*70}")
from scipy import stats
p_hat = wins / n
se = np.sqrt(0.5 * 0.5 / n)
z = (p_hat - 0.5) / se
pval = 1 - stats.norm.cdf(z)
# Also test vs breakeven (54% with taker fees)
z_be = (p_hat - 0.54) / np.sqrt(0.54 * 0.46 / n)
pval_be = 1 - stats.norm.cdf(z_be)
print(f"  n={n:,} | WR={p_hat*100:.2f}% | Z vs 50%={z:.1f} (p={pval:.2e})")
print(f"  Z vs 54% breakeven={z_be:.1f} (p={pval_be:.2e})")
print(f"  Significant vs 50%: {'YES ✓' if pval < 0.001 else 'NO'}")
print(f"  Significant vs 54%: {'YES ✓' if pval_be < 0.001 else 'NO'}")

# Equity curve
print(f"\n{'─'*70}")
print("EQUITY CURVE ($1000 start, flat $10/trade)")
print(f"{'─'*70}")
cumulative = 1000.0
for i, (_, t) in enumerate(tdf.iterrows()):
    cumulative += t.pnl
    if (i+1) % 5000 == 0 or i == len(tdf)-1:
        dt = pd.Timestamp(t.window_ts, unit='s').strftime("%Y-%m-%d")
        print(f"  Trade {i+1:6d} ({dt}): ${cumulative:,.2f}")

print(f"\n{'='*70}")
print(f"FINAL: $1,000 → ${cumulative:,.2f} ({(cumulative/1000-1)*100:+.1f}%) over {n:,} trades")
print(f"{'='*70}")
