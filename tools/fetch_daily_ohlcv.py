#!/usr/bin/env python3
"""Fetch ~80 days of daily OHLCV for every ticker in the current universe.

Hybrid data source: Stooq primary, yfinance fallback. A startup probe checks
whether Stooq is reachable. If yes, Stooq is tried first per ticker and
yfinance only covers misses. If no, Stooq is skipped entirely and the run
goes straight to yfinance (with fewer workers, since Yahoo rate-limits harder).

Reads .tmp/universe.csv (built by build_universe.py) and writes a single tidy
parquet:

    .tmp/ohlcv_YYYY-MM-DD.parquet
    columns: ticker, date, open, high, low, close, volume, source

80 trading days = SMA50 + ~30-day buffer for the v3 rolling-window scanner.

Reads STOOQ_API_KEY from .env.

Usage:
    python tools/fetch_daily_ohlcv.py [--workers 8] [--days 120] [--limit N]
                                      [--source auto|stooq|yfinance]
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

# yfinance is noisy on stderr — quiet it down.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
import yfinance as yf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = ROOT / ".tmp"

DEFAULT_WORKERS = 8
YFINANCE_WORKERS = 4  # Yahoo rate-limits; keep this conservative.
DEFAULT_CALENDAR_DAYS = 120  # ~80 trading days
STOOQ_TIMEOUT_S = 8  # tight: probe already proved reachability
STOOQ_RETRY_ATTEMPTS = 2
STOOQ_RETRY_BASE_DELAY = 0.5
PROBE_TICKER = "AAPL"
PROBE_TIMEOUT_S = 6
OUT_COLUMNS = ["ticker", "date", "open", "high", "low", "close", "volume", "source"]


def load_universe() -> list[str]:
    path = TMP_DIR / "universe.csv"
    if not path.exists():
        sys.exit(f"ERROR: {path} not found. Run build_universe.py first.")
    df = pd.read_csv(path)
    return df["ticker"].tolist()


def stooq_symbol(ticker: str) -> str:
    """Normalize ticker for Stooq URLs (dots → dashes, lowercase, .us suffix)."""
    return f"{ticker.replace('.', '-').lower()}.us"


def yfinance_symbol(ticker: str) -> str:
    """Normalize ticker for yfinance (dots → dashes; YF uses '-' for BRK-B etc.)."""
    return ticker.replace(".", "-")


def probe_stooq(api_key: str) -> tuple[bool, str]:
    """Single short probe to AAPL. Return (alive, reason_if_not)."""
    url = (
        "https://stooq.com/q/d/l/"
        f"?s={stooq_symbol(PROBE_TICKER)}&i=d"
        f"&d1={(dt.date.today() - dt.timedelta(days=10)).strftime('%Y%m%d')}"
        f"&d2={dt.date.today().strftime('%Y%m%d')}"
        f"&apikey={api_key}"
    )
    try:
        resp = requests.get(url, timeout=PROBE_TIMEOUT_S)
        resp.raise_for_status()
        text = resp.text.strip()
        if not text or text.lower().startswith("no data"):
            return False, "probe returned empty/no-data"
        if "Date" not in text.split("\n", 1)[0]:
            return False, f"probe returned unexpected payload: {text[:80]!r}"
        return True, ""
    except requests.RequestException as exc:
        return False, str(exc)


def fetch_one_stooq(ticker: str, start: dt.date, end: dt.date, api_key: str,
                    session: requests.Session) -> pd.DataFrame | None:
    """Return one ticker's OHLCV DataFrame from Stooq, or None on miss/error."""
    url = (
        "https://stooq.com/q/d/l/"
        f"?s={stooq_symbol(ticker)}&i=d"
        f"&d1={start.strftime('%Y%m%d')}&d2={end.strftime('%Y%m%d')}"
        f"&apikey={api_key}"
    )
    for attempt in range(STOOQ_RETRY_ATTEMPTS):
        try:
            resp = session.get(url, timeout=STOOQ_TIMEOUT_S)
            resp.raise_for_status()
            text = resp.text.strip()
            if not text or text.lower().startswith("no data"):
                return None
            df = pd.read_csv(StringIO(text))
            df.columns = [c.lower() for c in df.columns]
            if "date" not in df.columns or "close" not in df.columns:
                return None
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df["ticker"] = ticker
            df["source"] = "stooq"
            return df[OUT_COLUMNS]
        except (requests.RequestException, ValueError):
            if attempt == STOOQ_RETRY_ATTEMPTS - 1:
                return None
            time.sleep(STOOQ_RETRY_BASE_DELAY * (2 ** attempt))
    return None


def fetch_one_yfinance(ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame | None:
    """Return one ticker's OHLCV DataFrame from yfinance, or None on miss/error."""
    try:
        hist = yf.Ticker(yfinance_symbol(ticker)).history(
            start=start.isoformat(),
            end=(end + dt.timedelta(days=1)).isoformat(),  # yf end is exclusive
            interval="1d",
            auto_adjust=True,
            actions=False,
            raise_errors=False,
        )
    except Exception:
        return None
    if hist is None or hist.empty:
        return None
    df = hist.reset_index()
    df.columns = [str(c).lower() for c in df.columns]
    if "date" not in df.columns or "close" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["ticker"] = ticker
    df["source"] = "yfinance"
    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    if len(keep) < 5:
        return None
    return df[OUT_COLUMNS]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=None,
                        help=f"Concurrent workers (default: {DEFAULT_WORKERS} Stooq / {YFINANCE_WORKERS} yfinance)")
    parser.add_argument("--days", type=int, default=DEFAULT_CALENDAR_DAYS,
                        help=f"Calendar days of history to pull (default: {DEFAULT_CALENDAR_DAYS})")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap tickers for a quick test run")
    parser.add_argument("--source", choices=["auto", "stooq", "yfinance"], default="auto",
                        help="Force a data source. 'auto' probes Stooq and falls back.")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("STOOQ_API_KEY")
    if not api_key and args.source in ("auto", "stooq"):
        sys.exit("ERROR: STOOQ_API_KEY not set in .env (required for Stooq path)")

    TMP_DIR.mkdir(exist_ok=True)

    tickers = load_universe()
    if args.limit:
        tickers = tickers[: args.limit]

    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)

    # Decide which source(s) to use.
    if args.source == "stooq":
        stooq_alive = True
        use_yf_fallback = False
    elif args.source == "yfinance":
        stooq_alive = False
        use_yf_fallback = True
    else:  # auto
        print(f"Probing Stooq with {PROBE_TICKER}...", file=sys.stderr)
        stooq_alive, reason = probe_stooq(api_key)
        if stooq_alive:
            print("  Stooq reachable. Using Stooq primary + yfinance fallback for misses.",
                  file=sys.stderr)
        else:
            print(f"  Stooq unreachable ({reason}). Falling back to yfinance-only.",
                  file=sys.stderr)
        use_yf_fallback = True

    if args.workers is not None:
        workers = args.workers
    else:
        workers = DEFAULT_WORKERS if stooq_alive else YFINANCE_WORKERS

    mode = (
        "stooq-primary+yfinance-fallback" if (stooq_alive and use_yf_fallback)
        else "stooq-only" if stooq_alive
        else "yfinance-only"
    )
    print(f"Fetching {len(tickers):,} tickers {start}..{end} "
          f"(workers={workers}, mode={mode})...", file=sys.stderr)

    all_chunks: list[pd.DataFrame] = []
    missing: list[str] = []
    stooq_misses: list[str] = []
    yf_rescued: list[str] = []
    completed = 0

    def fetch_one(ticker: str, session: requests.Session) -> tuple[str, pd.DataFrame | None, bool]:
        """Return (ticker, df-or-None, used_fallback)."""
        if stooq_alive:
            df = fetch_one_stooq(ticker, start, end, api_key, session)
            if df is not None and not df.empty:
                return ticker, df, False
            if not use_yf_fallback:
                return ticker, None, False
            df = fetch_one_yfinance(ticker, start, end)
            return ticker, df, True
        # yfinance-only
        df = fetch_one_yfinance(ticker, start, end)
        return ticker, df, False

    with requests.Session() as session, \
         ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_one, t, session): t for t in tickers}
        for fut in as_completed(futures):
            ticker = futures[fut]
            completed += 1
            if completed % 200 == 0:
                print(f"  {completed:,}/{len(tickers):,}...", file=sys.stderr, flush=True)
            try:
                _, df, used_fallback = fut.result()
            except Exception as exc:
                print(f"  {ticker}: unexpected error ({exc})", file=sys.stderr)
                missing.append(ticker)
                continue
            if df is None or df.empty:
                missing.append(ticker)
                if stooq_alive:
                    stooq_misses.append(ticker)
                continue
            if used_fallback:
                yf_rescued.append(ticker)
                stooq_misses.append(ticker)
            all_chunks.append(df)

    if not all_chunks:
        sys.exit("ERROR: no data fetched from any source.")

    combined = pd.concat(all_chunks, ignore_index=True)
    combined = combined.dropna(subset=["close"])
    combined = combined.sort_values(["ticker", "date"]).reset_index(drop=True)

    today = end.isoformat()
    out_path = TMP_DIR / f"ohlcv_{today}.parquet"
    combined.to_parquet(out_path, index=False)

    fetched_tickers = combined["ticker"].nunique()
    src_counts = combined.groupby("source")["ticker"].nunique().to_dict()
    print(f"\nWrote {len(combined):,} rows across {fetched_tickers:,} tickers to {out_path}",
          file=sys.stderr)
    print(f"  By source: {src_counts}", file=sys.stderr)
    if yf_rescued:
        sample = sorted(yf_rescued)[:10]
        print(f"  yfinance rescued {len(yf_rescued):,} tickers Stooq missed "
              f"(sample: {sample}{'...' if len(yf_rescued) > 10 else ''})", file=sys.stderr)
    if missing:
        sample = sorted(missing)[:10]
        print(f"  {len(missing):,} tickers returned no data from any source "
              f"(sample: {sample}{'...' if len(missing) > 10 else ''})", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
