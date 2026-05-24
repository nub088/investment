#!/usr/bin/env python3
"""Fetch company info (name, sector, summary) and cache locally.

Source: yfinance Ticker.info. `longBusinessSummary` is Yahoo's current
operational description (what the company does today), not historical.

Cache: .tmp/company_info.parquet
Columns: ticker, name, short_name, sector, industry, summary, country, website, fetched_at

Incremental by default — only fetches tickers not in the cache. Use --refresh
to overwrite. Tickers older than --max-age-days are also re-fetched.

Usage:
    python tools/fetch_company_info.py                      # fetch retro tickers
    python tools/fetch_company_info.py --tickers PAA MRVL   # specific tickers
    python tools/fetch_company_info.py --from-universe      # full universe
    python tools/fetch_company_info.py --refresh            # re-fetch everything
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

logging.getLogger("yfinance").setLevel(logging.CRITICAL)
import yfinance as yf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = ROOT / ".tmp"
CACHE_PATH = TMP_DIR / "company_info.parquet"

COLUMNS = [
    "ticker", "name", "short_name", "sector", "industry",
    "summary", "country", "website", "fetched_at",
]


def fetch_one(ticker: str) -> dict | None:
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        print(f"  ERR {ticker}: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    return {
        "ticker": ticker,
        "name": info.get("longName") or info.get("shortName") or "",
        "short_name": info.get("shortName") or "",
        "sector": info.get("sector") or "",
        "industry": info.get("industry") or "",
        "summary": info.get("longBusinessSummary") or "",
        "country": info.get("country") or "",
        "website": info.get("website") or "",
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


def load_cache() -> pd.DataFrame:
    if CACHE_PATH.exists():
        return pd.read_parquet(CACHE_PATH)
    return pd.DataFrame(columns=COLUMNS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--tickers", nargs="+")
    src.add_argument("--from-retro", metavar="SINCE", default=None,
                     help="Read tickers from top_retrospective_<since>.csv (default: 2026-01-01)")
    src.add_argument("--from-universe", action="store_true",
                     help="Read tickers from universe.csv")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--refresh", action="store_true",
                        help="Re-fetch all targeted tickers, ignoring cache freshness")
    parser.add_argument("--max-age-days", type=int, default=30,
                        help="Refetch cached entries older than N days (default: 30)")
    args = parser.parse_args()

    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
    elif args.from_universe:
        u_path = TMP_DIR / "universe.csv"
        if not u_path.exists():
            sys.exit("ERROR: .tmp/universe.csv missing — run build_universe.py first.")
        tickers = pd.read_csv(u_path)["ticker"].tolist()
    else:
        since = args.from_retro or "2026-01-01"
        retro_path = TMP_DIR / f"top_retrospective_{since}.csv"
        if not retro_path.exists():
            sys.exit(f"ERROR: {retro_path} missing — run scan_retrospective.py first.")
        tickers = pd.read_csv(retro_path)["ticker"].tolist()

    cache = load_cache()

    # Determine which tickers need fetching
    if args.refresh:
        to_fetch = tickers
    else:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.max_age_days)
        fresh: set[str] = set()
        if not cache.empty:
            stale_mask = pd.to_datetime(cache["fetched_at"], utc=True) < cutoff
            fresh_df = cache[~stale_mask]
            fresh = set(fresh_df["ticker"])
        to_fetch = [t for t in tickers if t not in fresh]

    print(f"Targets: {len(tickers)}  already fresh: {len(tickers) - len(to_fetch)}  "
          f"to fetch: {len(to_fetch)}", file=sys.stderr)

    if not to_fetch:
        print("Nothing to fetch — cache is current.", file=sys.stderr)
        return 0

    rows: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(fetch_one, t): t for t in to_fetch}
        for fut in as_completed(futures):
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(to_fetch)}", file=sys.stderr)
            r = fut.result()
            if r:
                rows.append(r)

    if not rows:
        print("No rows fetched — check Yahoo rate-limit / connectivity.", file=sys.stderr)
        return 1

    new_df = pd.DataFrame(rows, columns=COLUMNS)

    # Merge: new rows replace old for the same ticker; keep other cached tickers as-is.
    if not cache.empty:
        keep = cache[~cache["ticker"].isin(new_df["ticker"])]
        merged = pd.concat([keep, new_df], ignore_index=True)
    else:
        merged = new_df

    merged = merged.sort_values("ticker").reset_index(drop=True)
    merged.to_parquet(CACHE_PATH, index=False)

    print(f"\nFetched {len(new_df)} new/refreshed rows.", file=sys.stderr)
    print(f"Cache now has {len(merged)} total tickers → {CACHE_PATH}", file=sys.stderr)

    # Tell the user what failed
    failed = set(to_fetch) - set(new_df["ticker"])
    if failed:
        print(f"\n{len(failed)} tickers had no usable info (showing first 20):", file=sys.stderr)
        print("  " + ", ".join(sorted(failed)[:20]), file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
