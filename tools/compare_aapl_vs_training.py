#!/usr/bin/env python
"""Snapshot comparison: AAPL (today) vs the 5 training tickers on first-qualifying day.

All metrics computed on the same end-anchored 20-day window logic that
compute_top_signals.py uses.
"""
from __future__ import annotations
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp"

# (ticker, training csv path or None for AAPL parquet, evaluation date)
TARGETS = [
    ("AAPL", None,                                                "2026-05-19"),
    ("SNDK", TMP / "training_SNDK_2025-09-03_2025-11-06.csv",     "2025-09-30"),
    ("WDC",  TMP / "training_WDC_2025-04-08_2025-09-26.csv",      "2025-06-09"),
    ("GLW",  TMP / "training_GLW_2025-04-07_2025-07-23.csv",      "2025-05-16"),
    ("BE",   TMP / "training_BE_2025-04-01_2025-09-09.csv",       "2025-06-20"),
    ("RBLX", TMP / "training_RBLX_2025-01-02_2025-12-31.csv",     "2025-05-09"),
]

WINDOW = 20
SLOPE_WINDOW = 5


def load_aapl() -> pd.DataFrame:
    pq = sorted(TMP.glob("ohlcv_*.parquet"))[-1]
    df = pd.read_parquet(pq)
    df = df[df["ticker"] == "AAPL"].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["sma20"] = df["close"].rolling(WINDOW).mean()
    df["sma50"] = df["close"].rolling(50).mean()
    return df


def load_training(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    # sma20 / sma50 already present
    return df


def snapshot(df: pd.DataFrame, ticker: str, end_date: str) -> dict:
    end = pd.to_datetime(end_date)
    idx = df.index[df["date"] == end].tolist()
    if not idx:
        # if exact date missing (holiday), use last bar <= end
        idx = df.index[df["date"] <= end].tolist()
        if not idx:
            return {"ticker": ticker, "ERROR": f"no data on/before {end_date}"}
    i = idx[-1]
    if i + 1 < WINDOW:
        return {"ticker": ticker, "ERROR": "not enough history"}

    win = df.iloc[i - WINDOW + 1 : i + 1].copy()
    cur = df.iloc[i]

    sma20_then = df["sma20"].iloc[i - SLOPE_WINDOW]
    slope_pct_5d = (cur["sma20"] / sma20_then - 1.0) * 100.0

    closes_above = int((win["close"] > win["sma20"]).sum())
    pct_above = closes_above / WINDOW * 100.0
    greens = int((win["close"] > win["open"]).sum())
    reds = int((win["close"] < win["open"]).sum())
    gr_ratio = greens / reds if reds > 0 else float("inf")

    win["range_pct"] = (win["high"] - win["low"]) / win["close"] * 100.0
    win["day_pct"] = win["close"].pct_change() * 100.0
    median_range = float(win["range_pct"].median())
    max_range = float(win["range_pct"].max())
    avg_abs_day = float(win["day_pct"].abs().mean())
    big_red = int((win["day_pct"] < -5.0).sum())
    big_green = int((win["day_pct"] > 5.0).sum())

    dist_above = (cur["close"] / cur["sma20"] - 1.0) * 100.0

    # Window return: % change from window start close to window end close
    win_start_close = float(win["close"].iloc[0])
    win_end_close = float(win["close"].iloc[-1])
    window_return = (win_end_close / win_start_close - 1.0) * 100.0

    return {
        "ticker": ticker,
        "qual_date": end_date,
        "close": round(float(cur["close"]), 2),
        "sma20": round(float(cur["sma20"]), 2),
        "sma50": round(float(cur["sma50"]), 2) if pd.notna(cur["sma50"]) else None,
        "dist_sma20%": round(dist_above, 2),
        "pct_above": round(pct_above, 1),
        "G": greens,
        "R": reds,
        "G:R": round(gr_ratio, 2),
        "slope_5d%": round(slope_pct_5d, 2),
        "median_rng%": round(median_range, 2),
        "max_rng%": round(max_range, 2),
        "avg_|day|%": round(avg_abs_day, 2),
        "big_red(-5%)": big_red,
        "big_grn(+5%)": big_green,
        "win_return%": round(window_return, 2),
    }


def main() -> int:
    aapl_df = load_aapl()
    rows = []
    for ticker, csv_path, end_date in TARGETS:
        if csv_path is None:
            df = aapl_df
        else:
            df = load_training(csv_path)
        rows.append(snapshot(df, ticker, end_date))

    out = pd.DataFrame(rows)
    print("\nSnapshot at first-qualification date (20-day end-anchored window):\n")
    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(out.to_string(index=False))

    # Flavor classification heuristic
    print("\n" + "=" * 100)
    print("FLAVOR CLASSIFICATION (per feedback_top_review_tiebreaker):")
    print("  Measured: low median range, few big-move days, low avg|day|")
    print("  Explosive: high median range, big catalyst days, high avg|day|")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
