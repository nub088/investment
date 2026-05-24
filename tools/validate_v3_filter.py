#!/usr/bin/env python
"""Empirical validation of proposed v3 scanner filter against training tickers.

Proposed rules (user 2026-05-20), evaluated over a rolling 20-trading-day window:
  R1: >=90% of closes in window above SMA20  (>=18 of 20)
  R2a: greens >= 1.75 * reds  (literal "75% more")
  R2b: greens >= 0.75 * total  (shorthand "75% are greens")
  R3:  leading-edge tightness — first 5 days of window have avg daily range pct below threshold
       (sweeps a few thresholds; reports what's found)
  R4:  SMA20 5-day slope > 0

For each ticker, reports the earliest date all rules pass, plus pass-rates across the full window.
"""
from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd

TMP = Path(__file__).resolve().parent.parent / ".tmp"

TICKERS = {
    "SNDK": "training_SNDK_2025-09-03_2025-11-06.csv",
    "WDC":  "training_WDC_2025-04-08_2025-09-26.csv",
    "GLW":  "training_GLW_2025-04-07_2025-07-23.csv",
    "BE":   "training_BE_2025-04-01_2025-09-09.csv",
}

# User-stated visual TOP start dates (where known). For comparison only.
USER_TOP_START = {
    "BE":  "2025-05-06",
    "WDC": "2025-04-09",   # inferred from user's "starts right after 4/9 earnings"
    "GLW": None,           # not explicitly stated
    "SNDK": None,          # not explicitly stated
}

WINDOW = 20
LEAD_EDGE = 5  # days at the start of window for "tightness" check

def load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    # day%/color may be missing or computed differently; we normalize here.
    df["day_pct"] = df.get("day_pct", df["close"].pct_change() * 100)
    if "color" not in df.columns:
        df["color"] = (df["close"] >= df["open"]).map({True: "G", False: "R"})
    df["range_pct"] = (df["high"] - df["low"]) / df["close"] * 100
    df["sma20_slope_5d"] = df["sma20"].diff(5)
    return df

def evaluate(df: pd.DataFrame, ticker: str) -> dict:
    """Walk the window across the ticker's full table; report pass-rates and earliest-qualifying date."""
    rows = []
    for i in range(WINDOW - 1, len(df)):
        win = df.iloc[i - WINDOW + 1 : i + 1]
        lead = win.iloc[:LEAD_EDGE]
        cur = df.iloc[i]

        closes_above = (win["close"] > win["sma20"]).sum()
        pct_above = closes_above / WINDOW * 100
        greens = (win["color"] == "G").sum()
        reds = (win["color"] == "R").sum()
        avg_lead_range = lead["range_pct"].mean()
        avg_win_range = win["range_pct"].mean()
        slope = cur["sma20_slope_5d"]

        r1 = pct_above >= 90
        r2a = greens >= 1.75 * reds  # literal
        r2b = greens >= 0.75 * WINDOW  # shorthand
        # No fixed R3 threshold yet — just record metric
        r4 = pd.notna(slope) and slope > 0

        rows.append({
            "date": cur["date"].strftime("%Y-%m-%d"),
            "close": cur["close"],
            "sma20": cur["sma20"],
            "pct_above": round(pct_above, 1),
            "greens": int(greens),
            "reds": int(reds),
            "g_over_r": round(greens / max(reds, 1), 2),
            "g_pct_total": round(greens / WINDOW * 100, 1),
            "lead_avg_range%": round(avg_lead_range, 2),
            "win_avg_range%": round(avg_win_range, 2),
            "slope_5d": round(slope, 3) if pd.notna(slope) else None,
            "R1_90pct_above": r1,
            "R2a_lit": r2a,
            "R2b_short": r2b,
            "R4_slope_up": r4,
            "pass_all_2a": r1 and r2a and r4,
            "pass_all_2b": r1 and r2b and r4,
        })
    out = pd.DataFrame(rows)
    first_2a = out[out["pass_all_2a"]].head(1)
    first_2b = out[out["pass_all_2b"]].head(1)
    summary = {
        "ticker": ticker,
        "rows_evaluated": len(out),
        "first_qual_date_2a": first_2a.iloc[0]["date"] if not first_2a.empty else None,
        "first_qual_date_2b": first_2b.iloc[0]["date"] if not first_2b.empty else None,
        "pct_days_passing_2a": round(out["pass_all_2a"].mean() * 100, 1),
        "pct_days_passing_2b": round(out["pass_all_2b"].mean() * 100, 1),
        "user_stated_start": USER_TOP_START.get(ticker),
    }
    return summary, out


def main() -> int:
    print("=" * 100)
    print(f"v3 SCANNER FILTER VALIDATION  (window={WINDOW} trading days, lead={LEAD_EDGE} days)")
    print("=" * 100)

    all_summaries = []
    for ticker, fname in TICKERS.items():
        path = TMP / fname
        if not path.exists():
            print(f"\n!! Missing {path}")
            continue
        df = load(path)
        summary, detail = evaluate(df, ticker)
        all_summaries.append(summary)

        print(f"\n--- {ticker}  ({path.name}, {len(df)} bars) ---")
        print(f"  User-stated TOP start:  {summary['user_stated_start']}")
        print(f"  First qual (R2a literal G>=1.75R):   {summary['first_qual_date_2a']}")
        print(f"  First qual (R2b shorthand G>=75%):    {summary['first_qual_date_2b']}")
        print(f"  % of evaluable days passing 2a: {summary['pct_days_passing_2a']}%")
        print(f"  % of evaluable days passing 2b: {summary['pct_days_passing_2b']}%")

        # Show a sample around the first qualifying date for inspection
        first_qual = summary["first_qual_date_2a"] or summary["first_qual_date_2b"]
        if first_qual:
            idx = detail[detail["date"] == first_qual].index[0]
            window_start = max(0, idx - 3)
            window_end = min(len(detail), idx + 5)
            print("\n  Context around first qual (2a):")
            print(detail.iloc[window_start:window_end].to_string(index=False))

    print("\n" + "=" * 100)
    print("SUMMARY TABLE")
    print("=" * 100)
    summary_df = pd.DataFrame(all_summaries)
    print(summary_df.to_string(index=False))

    print("\n" + "=" * 100)
    print("LEADING-EDGE TIGHTNESS (R3) — per-ticker distribution of lead_avg_range% on qualifying days")
    print("=" * 100)
    for ticker, fname in TICKERS.items():
        path = TMP / fname
        if not path.exists():
            continue
        df = load(path)
        _, detail = evaluate(df, ticker)
        qual = detail[detail["pass_all_2a"]]
        if qual.empty:
            print(f"  {ticker}: no qualifying days")
            continue
        print(f"  {ticker}: lead_avg_range% across qualifying days — "
              f"min={qual['lead_avg_range%'].min():.2f}  "
              f"median={qual['lead_avg_range%'].median():.2f}  "
              f"max={qual['lead_avg_range%'].max():.2f}")

    return 0

if __name__ == "__main__":
    sys.exit(main())
