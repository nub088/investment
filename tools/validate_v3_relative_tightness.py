#!/usr/bin/env python
"""Test RELATIVE tightness metrics for R3 across the 4 training tickers.

Hypothesis: SNDK/BE failed R3 with an absolute range% threshold because they are
inherently more volatile stocks. If we normalize lead-edge range% to each stock's
own baseline volatility, a universal threshold may emerge.

Pulls each ticker with extended history (60+ trading days before TOP start) so we
have a real baseline. Computes several candidate relative-tightness metrics for
each rolling 20-day window, reports distributions on qualifying days.
"""
from __future__ import annotations
import datetime as dt
import os
import sys
from io import StringIO
from pathlib import Path
import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
API_KEY = os.environ.get("STOOQ_API_KEY")
if not API_KEY:
    sys.exit("ERROR: STOOQ_API_KEY not set in .env")

# Extended pulls: at least 90 calendar days before TOP start, so trailing-60d baseline is real.
TICKERS = {
    "SNDK": ("2025-05-01", "2025-11-06"),  # TOP forms ~early-Sep
    "WDC":  ("2025-01-01", "2025-09-26"),  # TOP forms ~4/9
    "GLW":  ("2025-01-01", "2025-07-23"),  # TOP forms ~5/12
    "BE":   ("2025-01-01", "2025-09-09"),  # TOP forms ~5/06
}

WINDOW = 20         # trading days
LEAD_EDGE = 5       # days at start of window for "tightness"
BASELINE = 60       # trailing days for baseline volatility


def fetch_stooq(ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    url = (
        "https://stooq.com/q/d/l/"
        f"?s={ticker.lower()}.us&i=d"
        f"&d1={start.strftime('%Y%m%d')}&d2={end.strftime('%Y%m%d')}"
        f"&apikey={API_KEY}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    text = resp.text.strip()
    if not text or text.lower().startswith("no data"):
        sys.exit(f"ERROR: Stooq returned no data for {ticker}")
    df = pd.read_csv(StringIO(text))
    df.columns = [c.lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def decorate(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["sma20"] = df["close"].rolling(20).mean()
    df["sma20_slope_5d"] = df["sma20"].diff(5)
    df["range_pct"] = (df["high"] - df["low"]) / df["close"] * 100
    df["color"] = df.apply(
        lambda r: "G" if r["close"] > r["open"] else ("R" if r["close"] < r["open"] else "-"),
        axis=1,
    )
    return df


def evaluate(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    rows = []
    # Need WINDOW + BASELINE days of history before any evaluable point
    min_i = WINDOW + BASELINE - 1
    for i in range(min_i, len(df)):
        win = df.iloc[i - WINDOW + 1 : i + 1]
        lead = win.iloc[:LEAD_EDGE]
        baseline = df.iloc[i - WINDOW + 1 - BASELINE : i - WINDOW + 1]
        cur = df.iloc[i]

        closes_above = (win["close"] > win["sma20"]).sum()
        pct_above = closes_above / WINDOW * 100
        greens = (win["color"] == "G").sum()
        reds = (win["color"] == "R").sum()
        slope = cur["sma20_slope_5d"]

        r1 = pct_above >= 90
        r2a = greens >= 1.75 * reds
        r4 = pd.notna(slope) and slope > 0

        # Absolute tightness
        lead_range = lead["range_pct"].mean()
        win_range = win["range_pct"].mean()
        baseline_range = baseline["range_pct"].mean()

        # Relative tightness candidates:
        # M1: lead vs trailing baseline
        m1_lead_vs_baseline = lead_range / baseline_range if baseline_range > 0 else None
        # M2: lead vs full window
        m2_lead_vs_window = lead_range / win_range if win_range > 0 else None
        # M3: window vs baseline (is the whole pattern tighter than baseline?)
        m3_window_vs_baseline = win_range / baseline_range if baseline_range > 0 else None
        # M4: percentile rank of lead_range within trailing 60d ranges (lower pct = tighter)
        baseline_ranges = baseline["range_pct"].dropna().tolist()
        if baseline_ranges:
            below_lead = sum(1 for r in baseline_ranges if r < lead_range)
            m4_lead_pct_rank = below_lead / len(baseline_ranges) * 100
        else:
            m4_lead_pct_rank = None

        rows.append({
            "ticker": ticker,
            "date": cur["date"].strftime("%Y-%m-%d"),
            "close": round(cur["close"], 2),
            "pct_above": round(pct_above, 1),
            "greens": int(greens),
            "reds": int(reds),
            "lead_range%": round(lead_range, 2),
            "win_range%": round(win_range, 2),
            "baseline_range%": round(baseline_range, 2),
            "M1_lead_vs_base": round(m1_lead_vs_baseline, 2) if m1_lead_vs_baseline else None,
            "M2_lead_vs_win": round(m2_lead_vs_window, 2) if m2_lead_vs_window else None,
            "M3_win_vs_base": round(m3_window_vs_baseline, 2) if m3_window_vs_baseline else None,
            "M4_lead_pctile": round(m4_lead_pct_rank, 1) if m4_lead_pct_rank is not None else None,
            "pass_R124": r1 and r2a and r4,
        })
    return pd.DataFrame(rows)


def main() -> int:
    print("=" * 110)
    print(f"RELATIVE TIGHTNESS METRICS for R3")
    print(f"window={WINDOW}d, lead-edge={LEAD_EDGE}d, baseline={BASELINE}d trailing")
    print("=" * 110)

    all_qual = []
    all_nonqual = []
    for ticker, (start, end) in TICKERS.items():
        sd = dt.date.fromisoformat(start)
        ed = dt.date.fromisoformat(end)
        print(f"\nFetching {ticker} {sd}..{ed}...", file=sys.stderr)
        raw = fetch_stooq(ticker, sd, ed)
        dec = decorate(raw)
        result = evaluate(dec, ticker)
        qual = result[result["pass_R124"]]
        nonqual = result[~result["pass_R124"]]
        all_qual.append(qual)
        all_nonqual.append(nonqual)

        if qual.empty:
            print(f"\n--- {ticker}: no R1+R2a+R4 qualifying days in range ---")
            continue

        first_q = qual.iloc[0]
        print(f"\n--- {ticker} ({len(result)} evaluable days, {len(qual)} qualifying) ---")
        print(f"  First qual: {first_q['date']}  close={first_q['close']}")
        print(f"  Tightness metric distributions on qualifying days:")
        for col in ["lead_range%", "M1_lead_vs_base", "M2_lead_vs_win", "M3_win_vs_base", "M4_lead_pctile"]:
            s = qual[col].dropna()
            if not s.empty:
                print(f"    {col:22s}  min={s.min():6.2f}  p25={s.quantile(.25):6.2f}  median={s.median():6.2f}  p75={s.quantile(.75):6.2f}  max={s.max():6.2f}")

    # Cross-ticker comparison: median by ticker, for each metric
    print("\n" + "=" * 110)
    print("CROSS-TICKER COMPARISON: median value on QUALIFYING days")
    print("=" * 110)
    combined = pd.concat(all_qual, ignore_index=True)
    print(combined.groupby("ticker")[
        ["lead_range%", "M1_lead_vs_base", "M2_lead_vs_win", "M3_win_vs_base", "M4_lead_pctile"]
    ].median().round(2).to_string())

    # Compare qualifying vs non-qualifying days: does any metric SEPARATE them?
    print("\n" + "=" * 110)
    print("DISCRIMINATION CHECK: qualifying vs non-qualifying days, all tickers combined")
    print("=" * 110)
    qual_all = pd.concat(all_qual, ignore_index=True)
    nonqual_all = pd.concat(all_nonqual, ignore_index=True)
    for col in ["lead_range%", "M1_lead_vs_base", "M2_lead_vs_win", "M3_win_vs_base", "M4_lead_pctile"]:
        q = qual_all[col].dropna()
        n = nonqual_all[col].dropna()
        if not q.empty and not n.empty:
            print(f"  {col:22s}  qual median={q.median():6.2f}  nonqual median={n.median():6.2f}  diff={q.median()-n.median():+6.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
