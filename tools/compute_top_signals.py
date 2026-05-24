#!/usr/bin/env python3
"""Apply the v3 TOP-pattern scanner filter to the latest universe OHLCV pull.

Reads the latest .tmp/ohlcv_YYYY-MM-DD.parquet, computes per-ticker indicators,
and applies the v3 LOCKED filter over a rolling 20 trading-day window
END-ANCHORED to the most recent bar:

    R1: >=90% of closes in window above SMA20  (>=18 of 20)
    R2a: greens >= 1.75 * reds  (literal "75% more greens than reds")
    R4: SMA20 5-day slope > 0

See project_top_scanner_filter.md memory for the locked spec.

Output:
    .tmp/top_candidates_YYYY-MM-DD.csv

The output includes metrics that support the measured-vs-explosive tiebreaker
heuristic (per feedback_top_review_tiebreaker.md): names with lower median
daily range and higher G:R ratio are "measured" and rank higher in review.

Usage:
    python tools/compute_top_signals.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = ROOT / ".tmp"

WINDOW = 20            # trading days, end-anchored to latest bar
MIN_PCT_ABOVE = 90.0   # R1
GR_RATIO = 1.75        # R2a: greens >= 1.75 * reds
SLOPE_WINDOW = 5       # R4: SMA20 N-day slope
MIN_BARS = 40          # need 20-day SMA20 valid across the 20-day window


def latest_ohlcv() -> Path:
    paths = sorted(TMP_DIR.glob("ohlcv_*.parquet"))
    if not paths:
        sys.exit("ERROR: no .tmp/ohlcv_*.parquet found. Run fetch_daily_ohlcv.py first.")
    return paths[-1]


def load_universe_meta() -> pd.DataFrame:
    path = TMP_DIR / "universe.csv"
    if not path.exists():
        return pd.DataFrame(columns=["ticker", "name", "exchange", "avg_vol_20d"])
    return pd.read_csv(path)


def evaluate_ticker(df: pd.DataFrame) -> dict | None:
    """Return scanner-output row dict if the ticker qualifies, else None."""
    df = df.sort_values("date").reset_index(drop=True)
    if len(df) < MIN_BARS:
        return None

    df["sma20"] = df["close"].rolling(WINDOW).mean()
    df["sma50"] = df["close"].rolling(50).mean()  # display-only

    # Build the end-anchored 20-day window
    if pd.isna(df["sma20"].iloc[-1]) or pd.isna(df["sma20"].iloc[-1 - SLOPE_WINDOW]):
        return None

    win = df.iloc[-WINDOW:].copy()
    if win["sma20"].isna().any():
        return None

    last = df.iloc[-1]
    sma20_prev = df["sma20"].iloc[-1 - SLOPE_WINDOW]
    slope_pct_5d = (last["sma20"] / sma20_prev - 1.0) * 100.0

    # R1: % of closes in window above SMA20
    closes_above = int((win["close"] > win["sma20"]).sum())
    pct_above = closes_above / WINDOW * 100.0

    # R2a: green vs red candle count
    greens = int((win["close"] > win["open"]).sum())
    reds = int((win["close"] < win["open"]).sum())

    # R4: slope > 0
    r1 = pct_above >= MIN_PCT_ABOVE
    r2a = greens >= GR_RATIO * reds
    r4 = slope_pct_5d > 0

    if not (r1 and r2a and r4):
        return None

    # Tiebreaker metrics (for measured vs explosive flavor)
    win["range_pct"] = (win["high"] - win["low"]) / win["close"] * 100.0
    win["day_pct"] = win["close"].pct_change() * 100.0
    median_range_pct = float(win["range_pct"].median())
    max_range_pct = float(win["range_pct"].max())
    avg_abs_day_pct = float(win["day_pct"].abs().mean())
    big_red_days = int((win["day_pct"] < -5.0).sum())
    big_green_days = int((win["day_pct"] > 5.0).sum())

    dist_above_sma20 = (last["close"] / last["sma20"] - 1.0) * 100.0
    gr_ratio = greens / reds if reds > 0 else float("inf")

    sma50_val = last["sma50"]

    return {
        "ticker": last.get("ticker", df["ticker"].iloc[0]),
        "last_date": last["date"],
        "last_close": round(float(last["close"]), 2),
        "sma20": round(float(last["sma20"]), 2),
        "sma50": round(float(sma50_val), 2) if pd.notna(sma50_val) else None,
        # R1
        "pct_above_sma20": round(pct_above, 1),
        # R2a
        "greens": greens,
        "reds": reds,
        "gr_ratio": round(gr_ratio, 2) if gr_ratio != float("inf") else None,
        # R4
        "sma20_slope_pct_5d": round(slope_pct_5d, 2),
        # Tiebreaker metrics
        "median_range_pct": round(median_range_pct, 2),
        "max_range_pct": round(max_range_pct, 2),
        "avg_abs_day_pct": round(avg_abs_day_pct, 2),
        "big_red_days": big_red_days,
        "big_green_days": big_green_days,
        # Display
        "dist_above_sma20_pct": round(dist_above_sma20, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    ohlcv_path = latest_ohlcv()
    print(f"Reading {ohlcv_path}...", file=sys.stderr)
    ohlcv = pd.read_parquet(ohlcv_path)
    print(f"  {len(ohlcv):,} rows, {ohlcv['ticker'].nunique():,} tickers", file=sys.stderr)

    meta = load_universe_meta()

    rows: list[dict] = []
    skipped_short = 0
    skipped_filter = 0
    for ticker, group in ohlcv.groupby("ticker"):
        if len(group) < MIN_BARS:
            skipped_short += 1
            continue
        result = evaluate_ticker(group)
        if result is None:
            skipped_filter += 1
            continue
        rows.append(result)

    if not rows:
        print(f"\nNo candidates passed the v3 filter "
              f"(skipped: {skipped_filter:,} failed filter, {skipped_short:,} had < {MIN_BARS} bars)",
              file=sys.stderr)
        return 0

    result = pd.DataFrame(rows)

    if not meta.empty:
        result = result.merge(meta, on="ticker", how="left")

    today = dt.date.today().isoformat()
    result["scan_date"] = today
    result["chart_url"] = result["ticker"].apply(
        lambda t: f"https://www.tradingview.com/chart/?symbol={t}"
    )
    result["notes"] = ""

    # Flavor heuristic for review priority: measured = lower median range + high G:R
    # (purely a display-time sort; doesn't affect qualification)
    ordered = [
        "scan_date", "ticker", "name", "exchange", "last_date", "last_close",
        "avg_vol_20d",
        "pct_above_sma20", "greens", "reds", "gr_ratio", "sma20_slope_pct_5d",
        "median_range_pct", "max_range_pct", "avg_abs_day_pct",
        "big_red_days", "big_green_days",
        "dist_above_sma20_pct", "sma20", "sma50",
        "chart_url", "notes",
    ]
    ordered = [c for c in ordered if c in result.columns]
    result = result[ordered]

    # Default sort: measured TOPs first (low median range, high G:R, steep slope)
    result = result.sort_values(
        ["median_range_pct", "gr_ratio", "sma20_slope_pct_5d"],
        ascending=[True, False, False],
    ).reset_index(drop=True)

    out_path = TMP_DIR / f"top_candidates_{today}.csv"
    result.to_csv(out_path, index=False)

    print(f"\n{len(result):,} candidates passed the v3 filter "
          f"(skipped: {skipped_filter:,} failed filter, {skipped_short:,} had < {MIN_BARS} bars)",
          file=sys.stderr)
    print(f"Wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
