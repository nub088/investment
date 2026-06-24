#!/usr/bin/env python3
"""Retrospective v3 TOP-pattern scan for a watchlist of tickers (live fetch).

Fetches OHLCV via yfinance for a small ticker list (no universe.csv dependency)
and runs the rolling v3 filter at every bar. Useful for ad-hoc checks where
you don't want to rebuild the full-universe parquet.

For full-universe scans use scan_retrospective.py instead.

Usage:
    python tools/scan_watchlist.py [--tickers CVE BTE ...] [--since 2026-01-01]
                                   [--fetch-start 2025-10-01]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

import pandas as pd
import yfinance as yf

from v3_filter import MIN_BARS, rolling_signals, streaks_from_signals

DEFAULT_TICKERS = ["CVE", "BTE", "UMC", "STM", "CRWD", "S", "FLEX", "VSH", "IRDM", "MRVL", "PENG"]
DEFAULT_SINCE = "2026-01-01"
DEFAULT_FETCH_START = "2025-10-01"


def fetch_ohlcv(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    print(f"Fetching {len(tickers)} tickers from {start} to {end} via yfinance...", file=sys.stderr)
    raw = yf.download(tickers, start=start, end=end, auto_adjust=True, progress=False)
    if raw.empty:
        sys.exit("ERROR: yfinance returned no data.")

    if len(tickers) == 1:
        df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.columns = ["open", "high", "low", "close", "volume"]
        df["ticker"] = tickers[0]
        df.index.name = "date"
        df = df.reset_index()
    else:
        frames = []
        for tkr in tickers:
            try:
                sub = raw.xs(tkr, axis=1, level=1)[["Open", "High", "Low", "Close", "Volume"]].copy()
            except KeyError:
                print(f"  WARNING: no data for {tkr}", file=sys.stderr)
                continue
            sub.columns = ["open", "high", "low", "close", "volume"]
            sub["ticker"] = tkr
            sub.index.name = "date"
            frames.append(sub.reset_index())
        if not frames:
            sys.exit("ERROR: no data returned for any ticker.")
        df = pd.concat(frames, ignore_index=True)

    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.dropna(subset=["close"])
    return df


def print_ticker_report(ticker: str, signals: pd.DataFrame, since: date) -> None:
    if signals.empty:
        print(f"\n{'=' * 50}\n{ticker}: insufficient data")
        return

    window = signals[signals["date"] >= since].copy()
    current = signals.iloc[-1]
    status = "PASS" if current["pass"] else "FAIL"

    print(f"\n{'=' * 50}")
    print(f"{ticker}  |  current: {status}  |  last bar: {current['date']}  "
          f"close={current['close']}  sma20={current['sma20']}")
    body_ratio = current.get('body_ratio')
    body_str = f"{body_ratio:.2f}x" if body_ratio is not None and pd.notna(body_ratio) else "n/a"
    print(f"  R1={current['pct_above']}%above  R2b(body)={body_str}  "
          f"R4_slope={current['slope_5d']}  R5_price={current['close']}")

    if window.empty:
        print(f"  No data since {since}")
        return

    n_pass = int(window["pass"].sum())
    total = len(window)
    print(f"  Since {since}: {n_pass}/{total} days qualifying ({n_pass/total*100:.1f}%)")

    window["month"] = pd.to_datetime(window["date"]).dt.to_period("M")
    monthly = window.groupby("month").agg(days=("pass", "size"), passing=("pass", "sum"))
    monthly["rate"] = (monthly["passing"] / monthly["days"] * 100).round(0).astype(int)
    print("  Monthly: " + "  ".join(f"{m}: {r.rate}%" for m, r in monthly.iterrows()))

    streaks = streaks_from_signals(window, min_length=3)
    if streaks:
        print("  Streaks (>=3 days):")
        for s, e, n in streaks:
            print(f"    {s} → {e}  ({n} days)")
    else:
        print(f"  No streaks >=3 days since {since}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--fetch-start", default=DEFAULT_FETCH_START,
                        help="~40 trading days before --since for SMA warm-up")
    args = parser.parse_args()

    tickers = [t.upper() for t in args.tickers]
    since = date.fromisoformat(args.since)
    today = date.today().isoformat()

    df = fetch_ohlcv(tickers, start=args.fetch_start, end=today)

    results = []
    for ticker, group in df.groupby("ticker"):
        if len(group) < MIN_BARS:
            print(f"  SKIP {ticker}: only {len(group)} bars", file=sys.stderr)
            continue
        signals = rolling_signals(group)
        results.append((ticker, signals))

    # Sort: currently-passing first, then by pass rate since
    def sort_key(item):
        ticker, signals = item
        if signals.empty:
            return (1, 0)
        current_pass = int(signals.iloc[-1]["pass"])
        window = signals[signals["date"] >= since]
        rate = int(window["pass"].sum()) / max(len(window), 1)
        return (-current_pass, -rate)

    results.sort(key=sort_key)

    print(f"\nWATCHLIST TOP SCAN: since {since}")
    print(f"Tickers: {', '.join(tickers)}")
    for ticker, signals in results:
        print_ticker_report(ticker, signals, since)

    # Summary table
    print(f"\n{'=' * 50}\nSUMMARY")
    print(f"{'Ticker':<8} {'Status':<8} {'Pass%':<8} Longest")
    for ticker, signals in results:
        if signals.empty:
            print(f"{ticker:<8} {'NO DATA':<8}")
            continue
        status = "PASS" if signals.iloc[-1]["pass"] else "fail"
        window = signals[signals["date"] >= since]
        if window.empty:
            print(f"{ticker:<8} {status:<8} {'n/a':<8} 0d")
            continue
        rate = int(window["pass"].sum()) / len(window) * 100
        streaks = streaks_from_signals(window, min_length=1)
        longest = max((n for _, _, n in streaks), default=0)
        print(f"{ticker:<8} {status:<8} {rate:>4.0f}%   {longest}d")

    return 0


if __name__ == "__main__":
    sys.exit(main())
