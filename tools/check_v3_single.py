#!/usr/bin/env python
"""Apply v3 LOCKED scanner filter to a single ticker's saved training CSV.

v3 rules (rolling 20 trading-day window):
  R1: >=90% of closes above SMA20  (>=18 of 20)
  R2a: greens >= 1.75 * reds
  R4: SMA20 5-day slope > 0

Reports first qualifying date and a chronology of pass/fail by month.
"""
import sys
from pathlib import Path
import pandas as pd

WINDOW = 20

def main(csv_path: str) -> int:
    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    if "color" not in df.columns:
        df["color"] = (df["close"] >= df["open"]).map({True: "G", False: "R"})
    df["sma20_slope_5d"] = df["sma20"].diff(5)

    rows = []
    for i in range(WINDOW - 1, len(df)):
        win = df.iloc[i - WINDOW + 1 : i + 1]
        cur = df.iloc[i]
        closes_above = (win["close"] > win["sma20"]).sum()
        pct_above = closes_above / WINDOW * 100
        greens = (win["color"] == "G").sum()
        reds = (win["color"] == "R").sum()
        slope = cur["sma20_slope_5d"]
        r1 = pct_above >= 90
        r2a = greens >= 1.75 * reds
        r4 = pd.notna(slope) and slope > 0
        rows.append({
            "date": cur["date"].strftime("%Y-%m-%d"),
            "close": round(cur["close"], 2),
            "sma20": round(cur["sma20"], 2),
            "pct_above": round(pct_above, 1),
            "G": int(greens),
            "R": int(reds),
            "slope_5d": round(slope, 3) if pd.notna(slope) else None,
            "R1": r1, "R2a": r2a, "R4": r4,
            "pass": r1 and r2a and r4,
        })
    out = pd.DataFrame(rows)
    qual = out[out["pass"]]

    print(f"Ticker file: {csv_path}")
    print(f"Total bars: {len(df)}, evaluable: {len(out)}, qualifying: {len(qual)} ({len(qual)/len(out)*100:.1f}%)\n")

    if qual.empty:
        print("NO QUALIFYING DAYS")
        return 0

    first = qual.iloc[0]
    print(f"FIRST QUALIFICATION: {first['date']}  close={first['close']}  sma20={first['sma20']}")
    print(f"  R1={first['pct_above']}% above SMA20, R2a={first['G']}G/{first['R']}R, slope={first['slope_5d']}\n")

    # Chronology: monthly summary
    out["month"] = pd.to_datetime(out["date"]).dt.to_period("M")
    monthly = out.groupby("month").agg(
        days=("pass", "size"),
        passing=("pass", "sum"),
        avg_pct_above=("pct_above", "mean"),
        avg_G=("G", "mean"),
        avg_R=("R", "mean"),
    ).round(1)
    monthly["pass_rate"] = (monthly["passing"] / monthly["days"] * 100).round(1)
    print("MONTHLY PASS RATE:")
    print(monthly.to_string())

    # Find contiguous qualifying streaks (>=5 days)
    print("\nQUALIFYING STREAKS (>=5 consecutive days):")
    streaks = []
    cur_start = None
    for _, r in out.iterrows():
        if r["pass"] and cur_start is None:
            cur_start = r["date"]
            cur_end = r["date"]
        elif r["pass"]:
            cur_end = r["date"]
        elif cur_start is not None:
            streaks.append((cur_start, cur_end))
            cur_start = None
    if cur_start is not None:
        streaks.append((cur_start, cur_end))
    for s, e in streaks:
        s_idx = out[out["date"] == s].index[0]
        e_idx = out[out["date"] == e].index[0]
        length = e_idx - s_idx + 1
        if length >= 5:
            print(f"  {s} → {e}  ({length} days)")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
