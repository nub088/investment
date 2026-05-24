#!/usr/bin/env python3
"""Pull daily OHLCV from Stooq for a ticker + window, decorate with TOP-pattern
training indicators, and print a table for visual walkthrough.

Reads STOOQ_API_KEY from .env. Pulls ~70 calendar days before --start so that
SMA50 / AvgVol20 / EMA8 are warm by the first displayed row.

Usage:
    python tools/training_data.py --ticker SNDK --start 2025-09-03 --end 2025-11-06
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
WARMUP_DAYS = 100  # calendar days; ~70 trading days covers SMA50 + EMA8 warm-in


def fetch_stooq(ticker: str, start: dt.date, end: dt.date, api_key: str) -> pd.DataFrame:
    url = (
        "https://stooq.com/q/d/l/"
        f"?s={ticker.lower()}.us&i=d"
        f"&d1={start.strftime('%Y%m%d')}&d2={end.strftime('%Y%m%d')}"
        f"&apikey={api_key}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    text = resp.text.strip()
    if not text or text.lower().startswith("no data"):
        sys.exit(f"ERROR: Stooq returned no data for {ticker} {start}..{end}\n  body: {text[:200]}")
    df = pd.read_csv(StringIO(text))
    df.columns = [c.lower() for c in df.columns]
    if "date" not in df.columns or "close" not in df.columns:
        sys.exit(f"ERROR: unexpected Stooq response for {ticker}: {text[:200]}")
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df.sort_values("date").reset_index(drop=True)


def decorate(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["sma20"] = df["close"].rolling(20).mean()
    df["sma50"] = df["close"].rolling(50).mean()  # display-only; not filtered on
    df["ema8"] = df["close"].ewm(span=8, adjust=False).mean()
    df["avg_vol_20"] = df["volume"].rolling(20).mean()
    df["day_pct"] = (df["close"] / df["close"].shift(1) - 1.0) * 100.0
    df["vol_ratio"] = df["volume"] / df["avg_vol_20"]
    df["vs_ema8_pct"] = (df["close"] / df["ema8"] - 1.0) * 100.0
    df["color"] = df.apply(
        lambda r: "G" if r["close"] > r["open"] else ("R" if r["close"] < r["open"] else "-"),
        axis=1,
    )
    # Where the day closed inside its own range (0% = at low, 100% = at high)
    rng = df["high"] - df["low"]
    df["close_in_range_pct"] = ((df["close"] - df["low"]) / rng.where(rng > 0)) * 100.0
    return df


def format_table(df: pd.DataFrame, ticker: str) -> str:
    show = df[
        [
            "date",
            "color",
            "open",
            "high",
            "low",
            "close",
            "day_pct",
            "close_in_range_pct",
            "volume",
            "vol_ratio",
            "ema8",
            "vs_ema8_pct",
            "sma20",
            "sma50",
        ]
    ].copy()

    show["open"] = show["open"].round(2)
    show["high"] = show["high"].round(2)
    show["low"] = show["low"].round(2)
    show["close"] = show["close"].round(2)
    show["ema8"] = show["ema8"].round(2)
    show["sma20"] = show["sma20"].round(2)
    show["sma50"] = show["sma50"].round(2)
    show["day_pct"] = show["day_pct"].round(2)
    show["close_in_range_pct"] = show["close_in_range_pct"].round(0)
    show["vs_ema8_pct"] = show["vs_ema8_pct"].round(2)
    show["vol_ratio"] = show["vol_ratio"].round(2)
    show["volume"] = (show["volume"] / 1_000_000).round(2)  # millions

    show = show.rename(
        columns={
            "day_pct": "day%",
            "close_in_range_pct": "rng%",
            "volume": "vol(M)",
            "vol_ratio": "volX",
            "vs_ema8_pct": "vsE8%",
        }
    )

    with pd.option_context(
        "display.max_rows", None,
        "display.max_columns", None,
        "display.width", 200,
        "display.colheader_justify", "right",
    ):
        body = show.to_string(index=False)

    header = f"{ticker.upper()}  {show['date'].iloc[0]} → {show['date'].iloc[-1]}  ({len(show)} bars)"
    return f"{header}\n{'-' * len(header)}\n{body}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD (first displayed bar)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (last displayed bar)")
    parser.add_argument("--save", action="store_true",
                        help="Also write the decorated table to .tmp/training_{ticker}_{start}_{end}.csv")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("STOOQ_API_KEY")
    if not api_key:
        sys.exit("ERROR: STOOQ_API_KEY not set in .env")

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    fetch_start = start - dt.timedelta(days=WARMUP_DAYS)

    print(
        f"Pulling {args.ticker.upper()} from Stooq: {fetch_start}..{end} "
        f"(warmup {WARMUP_DAYS}d before display window)...",
        file=sys.stderr,
    )
    raw = fetch_stooq(args.ticker, fetch_start, end, api_key)
    decorated = decorate(raw)

    window = decorated[
        (decorated["date"] >= start) & (decorated["date"] <= end)
    ].reset_index(drop=True)
    if window.empty:
        sys.exit(f"ERROR: no rows in display window {start}..{end} (data range: "
                 f"{decorated['date'].min()}..{decorated['date'].max()})")

    print(format_table(window, args.ticker))

    if args.save:
        out_dir = ROOT / ".tmp"
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / f"training_{args.ticker.upper()}_{args.start}_{args.end}.csv"
        window.to_csv(out_path, index=False)
        print(f"\nSaved decorated table to {out_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
