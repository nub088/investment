#!/usr/bin/env python
"""Gap and wick anatomy comparison: AAPL vs the 5 training tickers on first-qual day.

Tests the user's hypothesis that AAPL fails on stealth-accumulation signature
(visible as gaps + wicky candles) even though it passes the scanner's surface metrics.
"""
from __future__ import annotations
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp"

TARGETS = [
    ("AAPL", None,                                                "2026-05-19"),
    ("SNDK", TMP / "training_SNDK_2025-09-03_2025-11-06.csv",     "2025-09-30"),
    ("WDC",  TMP / "training_WDC_2025-04-08_2025-09-26.csv",      "2025-06-09"),
    ("GLW",  TMP / "training_GLW_2025-04-07_2025-07-23.csv",      "2025-05-16"),
    ("BE",   TMP / "training_BE_2025-04-01_2025-09-09.csv",       "2025-06-20"),
    ("RBLX", TMP / "training_RBLX_2025-01-02_2025-12-31.csv",     "2025-05-09"),
]

WINDOW = 20


def load_aapl() -> pd.DataFrame:
    pq = sorted(TMP.glob("ohlcv_*.parquet"))[-1]
    df = pd.read_parquet(pq)
    df = df[df["ticker"] == "AAPL"].copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def load_training(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def analyze(df: pd.DataFrame, ticker: str, end_date: str) -> dict:
    end = pd.to_datetime(end_date)
    idx = df.index[df["date"] <= end].tolist()[-1]
    if idx + 1 < WINDOW:
        return {"ticker": ticker, "ERROR": "not enough history"}

    # Need one extra bar before window start for gap calc
    win = df.iloc[max(0, idx - WINDOW + 1) : idx + 1].copy().reset_index(drop=True)
    if idx >= WINDOW:
        prev_close_start = df["close"].iloc[idx - WINDOW]
    else:
        prev_close_start = win["close"].iloc[0]

    win["prev_close"] = win["close"].shift(1).fillna(prev_close_start)
    win["gap_pct"] = (win["open"] / win["prev_close"] - 1) * 100
    win["body"] = (win["close"] - win["open"]).abs()
    win["range"] = win["high"] - win["low"]
    win["body_pct_of_range"] = (win["body"] / win["range"].where(win["range"] > 0) * 100).fillna(100)
    win["upper_wick_pct"] = (
        (win["high"] - win[["open", "close"]].max(axis=1)) / win["close"] * 100
    )
    win["lower_wick_pct"] = (
        (win[["open", "close"]].min(axis=1) - win["low"]) / win["close"] * 100
    )

    return {
        "ticker":          ticker,
        "qual_date":       end_date,
        "avg_|gap%|":      round(float(win["gap_pct"].abs().mean()), 2),
        "max_|gap%|":      round(float(win["gap_pct"].abs().max()), 2),
        "gaps>0.5%":       int((win["gap_pct"].abs() > 0.5).sum()),
        "gaps>1%":         int((win["gap_pct"].abs() > 1.0).sum()),
        "avg_body/rng%":   round(float(win["body_pct_of_range"].mean()), 1),
        "wicky_days(<30%)": int((win["body_pct_of_range"] < 30).sum()),
        "decisive(>70%)":  int((win["body_pct_of_range"] > 70).sum()),
        "avg_upper_wick%": round(float(win["upper_wick_pct"].mean()), 2),
        "avg_lower_wick%": round(float(win["lower_wick_pct"].mean()), 2),
    }


def main() -> int:
    aapl = load_aapl()
    rows = []
    for ticker, csv, end_date in TARGETS:
        df = aapl if csv is None else load_training(csv)
        rows.append(analyze(df, ticker, end_date))

    out = pd.DataFrame(rows)
    print("\nGAP + WICK ANATOMY at first-qualification day (20-day window):\n")
    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(out.to_string(index=False))

    print("\nLegend:")
    print("  STEALTH ACCUMULATION = small gaps, high body/range, few wicky days")
    print("  NEWS-DRIVEN ACTION    = frequent gaps, low body/range, many wicky days")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
