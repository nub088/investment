#!/usr/bin/env python3
"""Retrospective v3 TOP-pattern scan across the full universe.

Reads the latest .tmp/ohlcv_*.parquet, slides the v3 20-day window across
every bar from --since onward, and collects every ticker that qualified for
at least --min-streak consecutive days. Output is sorted to put the most
actionable names (currently passing + longest streak) at the top.

Outputs:
    .tmp/top_retrospective_<since>.csv      — per-ticker summary
    .tmp/signals_<scan_date>.parquet        — daily pass/fail history
                                              (consumed by build_retro_review.py)

Usage:
    python tools/scan_retrospective.py [--since 2026-01-01] [--min-streak 3]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from v3_filter import (
    MIN_BARS,
    rolling_signals,
    streaks_from_signals,
)

ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = ROOT / ".tmp"


def latest_ohlcv() -> Path:
    paths = sorted(TMP_DIR.glob("ohlcv_*.parquet"))
    if not paths:
        sys.exit("ERROR: no .tmp/ohlcv_*.parquet found. Run fetch_daily_ohlcv.py first.")
    return paths[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="2026-01-01", help="Reporting window start (YYYY-MM-DD)")
    parser.add_argument("--min-streak", type=int, default=3, help="Only keep tickers with a streak >= N days")
    args = parser.parse_args()
    since = date.fromisoformat(args.since)

    ohlcv_path = latest_ohlcv()
    print(f"Reading {ohlcv_path}...", file=sys.stderr)
    ohlcv = pd.read_parquet(ohlcv_path)
    ohlcv["date"] = pd.to_datetime(ohlcv["date"]).dt.date
    print(f"  {len(ohlcv):,} rows, {ohlcv['ticker'].nunique():,} tickers, "
          f"{ohlcv['date'].min()} → {ohlcv['date'].max()}", file=sys.stderr)

    # Scan date = max bar date in dataset
    scan_date = ohlcv["date"].max()

    summary_rows: list[dict] = []
    signal_frames: list[pd.DataFrame] = []
    total = ohlcv["ticker"].nunique()
    done = 0

    for ticker, group in ohlcv.groupby("ticker"):
        done += 1
        if done % 200 == 0:
            print(f"  {done}/{total}...", file=sys.stderr)

        if len(group) < MIN_BARS:
            continue

        signals = rolling_signals(group)
        if signals.empty:
            continue

        # Persist full per-bar signals (needed by HTML builder for shading).
        # We keep ALL evaluable bars, not just since-window, so the chart can
        # render shading across the visible range.
        sf = signals[["date", "pass"]].copy()
        sf["ticker"] = ticker
        signal_frames.append(sf)

        # Summary computed against the since-window
        window_signals = signals[signals["date"] >= since]
        if window_signals.empty or not window_signals["pass"].any():
            continue

        streaks = streaks_from_signals(window_signals, min_length=args.min_streak)
        if not streaks:
            continue

        first_qual = window_signals.loc[window_signals["pass"], "date"].iloc[0]
        pass_days = int(window_signals["pass"].sum())
        total_days = int(len(window_signals))
        current_pass = bool(signals["pass"].iloc[-1])
        last_close = float(signals["close"].iloc[-1])
        last_sma20 = float(signals["sma20"].iloc[-1])
        longest = max(n for _, _, n in streaks)

        summary_rows.append({
            "ticker": ticker,
            "first_qual_date": first_qual,
            "pass_pct": round(pass_days / total_days * 100, 1),
            "pass_days": pass_days,
            "total_days": total_days,
            "longest_streak_days": longest,
            "currently_passing": current_pass,
            "last_close": round(last_close, 2),
            "last_sma20": round(last_sma20, 2),
            "streaks": "; ".join(f"{s}→{e}({n}d)" for s, e, n in streaks),
        })

    print(f"\nDone. {len(summary_rows)} tickers with streaks >= {args.min_streak}d since {since}.",
          file=sys.stderr)

    # Save daily signals parquet (covers ALL evaluated tickers, not just summary)
    if signal_frames:
        signals_df = pd.concat(signal_frames, ignore_index=True)
        signals_path = TMP_DIR / f"signals_{scan_date}.parquet"
        signals_df.to_parquet(signals_path, index=False)
        print(f"Saved signals → {signals_path}  "
              f"({len(signals_df):,} rows, {signals_df['ticker'].nunique():,} tickers)",
              file=sys.stderr)

    if not summary_rows:
        print("No qualifying tickers — exiting.", file=sys.stderr)
        return 0

    summary = pd.DataFrame(summary_rows)

    # Sort: currently-passing first, then longest streak desc, then earliest first_qual
    summary = summary.sort_values(
        by=["currently_passing", "longest_streak_days", "first_qual_date"],
        ascending=[False, False, True],
    ).reset_index(drop=True)

    out_path = TMP_DIR / f"top_retrospective_{since}.csv"
    summary.to_csv(out_path, index=False)
    print(f"Saved summary → {out_path}", file=sys.stderr)

    # Console report
    print(f"\nRETROSPECTIVE TOP SCAN — since {since}  (streaks >= {args.min_streak}d)")
    print(f"{'Ticker':<8} {'First':>12} {'Pass%':>6} {'LongStrk':>9} {'Curr':>5}  Streaks")
    print("-" * 95)
    for r in summary.itertuples(index=False):
        curr_str = "PASS" if r.currently_passing else "----"
        print(f"{r.ticker:<8} {str(r.first_qual_date):>12} {r.pass_pct:>5}% "
              f"{r.longest_streak_days:>8}d {curr_str:>5}  {r.streaks}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
