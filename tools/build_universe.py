#!/usr/bin/env python3
"""Build the tradeable US stock universe.

Pulls the two official NASDAQ Trader ticker files (covers NASDAQ + NYSE + NYSE
American + everything else listed in the US), filters to common stocks, then
runs a single yfinance batch pass to compute 20-day average daily volume and
drops anything below the liquidity floor.

Sources:
    https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt
    https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt

Output:
    .tmp/universe.csv  (columns: ticker, name, exchange, avg_vol_20d)

Usage:
    python tools/build_universe.py [--min-vol 2000000] [--batch-size 100]
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = ROOT / ".tmp"

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# NASDAQ Trader exchange codes used in otherlisted.txt
EXCHANGE_MAP = {
    "A": "NYSE American",
    "N": "NYSE",
    "P": "NYSE Arca",
    "Z": "BATS",
    "V": "IEX",
}

DEFAULT_MIN_VOL = 2_000_000
DEFAULT_BATCH_SIZE = 100
DEFAULT_LOOKBACK_DAYS = "1mo"  # for the volume-screen pass


def fetch_text(url: str, timeout: int = 30) -> str:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_listed_file(text: str, source: str) -> pd.DataFrame:
    """Parse a NASDAQ Trader pipe-delimited listed file into a clean DataFrame.

    Both files end with a "File Creation Time" footer line; we strip it.
    """
    # Drop footer line if present (starts with "File Creation Time")
    lines = [ln for ln in text.splitlines() if not ln.startswith("File Creation Time")]
    df = pd.read_csv(io.StringIO("\n".join(lines)), sep="|", dtype=str)

    if source == "nasdaq":
        # Columns: Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
        df = df[df["Test Issue"] == "N"]
        df = df[df["ETF"] == "N"]
        df = df[df["Financial Status"] == "N"]
        df = df.rename(columns={"Symbol": "ticker", "Security Name": "name"})
        df["exchange"] = "NASDAQ"
    elif source == "other":
        # Columns: ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
        df = df[df["Test Issue"] == "N"]
        df = df[df["ETF"] == "N"]
        df = df.rename(columns={"ACT Symbol": "ticker", "Security Name": "name"})
        df["exchange"] = df["Exchange"].map(EXCHANGE_MAP).fillna(df["Exchange"])
    else:
        raise ValueError(f"unknown source: {source}")

    return df[["ticker", "name", "exchange"]].dropna(subset=["ticker"])


def filter_symbol_quality(df: pd.DataFrame) -> pd.DataFrame:
    """Drop preferred shares, warrants, rights, units, etc. via symbol patterns.

    Heuristics chosen to be conservative: we err on the side of keeping
    legitimate stocks (e.g., BRK.B, BF.B) and let the volume screen handle
    anything weird that slips through.
    """
    t = df["ticker"]
    # Drop symbols with $ (preferred shares) and ^ (warrants on NASDAQ Trader)
    mask = ~t.str.contains(r"[\$\^]", regex=True, na=False)
    # Drop common non-stock suffixes when symbol length suggests it (4+ chars)
    # W = warrant, R = right, U = unit: only suffix-match for symbols >= 4 chars
    mask &= ~((t.str.len() >= 4) & t.str.endswith(("W", "R", "U", "WS", "RT")))
    # Drop test/dev symbols starting with ZZZ
    mask &= ~t.str.startswith("ZZZ")
    return df[mask].copy()


def fetch_volume_screen(
    tickers: list[str], batch_size: int, lookback: str
) -> dict[str, float]:
    """Run yfinance batched downloads and return ticker -> 20d avg volume."""
    avg_vol: dict[str, float] = {}
    n_batches = (len(tickers) + batch_size - 1) // batch_size

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i : i + batch_size]
        batch_num = i // batch_size + 1
        print(
            f"  [{batch_num}/{n_batches}] fetching {len(batch)} tickers...",
            file=sys.stderr,
            flush=True,
        )

        try:
            data = yf.download(
                batch,
                period=lookback,
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=True,
            )
        except Exception as exc:
            print(f"  batch {batch_num} failed: {exc}", file=sys.stderr)
            continue

        # When given a single ticker, yfinance returns flat columns; otherwise multi-level
        if len(batch) == 1:
            tkr = batch[0]
            if "Volume" in data.columns:
                vols = data["Volume"].dropna()
                if len(vols):
                    avg_vol[tkr] = float(vols.tail(20).mean())
        else:
            for tkr in batch:
                try:
                    vols = data[tkr]["Volume"].dropna()
                    if len(vols):
                        avg_vol[tkr] = float(vols.tail(20).mean())
                except (KeyError, AttributeError):
                    pass

        # Be a polite citizen
        time.sleep(0.5)

    return avg_vol


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-vol", type=int, default=DEFAULT_MIN_VOL,
                        help=f"Minimum 20-day avg daily volume (default: {DEFAULT_MIN_VOL:,})")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"yfinance batch size (default: {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--lookback", default=DEFAULT_LOOKBACK_DAYS,
                        help=f"yfinance period for volume screen (default: {DEFAULT_LOOKBACK_DAYS})")
    parser.add_argument("--limit", type=int, default=None,
                        help="Optional: cap tickers for a quick test run")
    args = parser.parse_args()

    TMP_DIR.mkdir(exist_ok=True)

    print("Fetching NASDAQ Trader listings...", file=sys.stderr)
    nasdaq_df = parse_listed_file(fetch_text(NASDAQ_URL), "nasdaq")
    other_df = parse_listed_file(fetch_text(OTHER_URL), "other")
    print(f"  nasdaqlisted: {len(nasdaq_df):,} common stocks", file=sys.stderr)
    print(f"  otherlisted:  {len(other_df):,} common stocks", file=sys.stderr)

    combined = pd.concat([nasdaq_df, other_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["ticker"])
    combined = filter_symbol_quality(combined)
    print(f"  after quality filter: {len(combined):,}", file=sys.stderr)

    if args.limit:
        combined = combined.head(args.limit)
        print(f"  --limit applied: {len(combined):,}", file=sys.stderr)

    tickers = combined["ticker"].tolist()

    print(f"Running volume screen on {len(tickers):,} tickers via yfinance...", file=sys.stderr)
    avg_vol = fetch_volume_screen(tickers, args.batch_size, args.lookback)
    print(f"  got volume data for {len(avg_vol):,} tickers", file=sys.stderr)

    combined["avg_vol_20d"] = combined["ticker"].map(avg_vol)
    survived = combined.dropna(subset=["avg_vol_20d"])
    survived = survived[survived["avg_vol_20d"] >= args.min_vol].copy()
    survived["avg_vol_20d"] = survived["avg_vol_20d"].astype("int64")
    survived = survived.sort_values("ticker").reset_index(drop=True)

    out_path = TMP_DIR / "universe.csv"
    survived.to_csv(out_path, index=False)
    print(f"\nWrote {len(survived):,} tickers to {out_path}", file=sys.stderr)

    if len(survived) < 500:
        print("WARNING: universe seems unexpectedly small. Check yfinance errors above.",
              file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
