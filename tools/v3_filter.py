#!/usr/bin/env python3
"""v3 LOCKED TOP-pattern filter: shared implementation.

Single source of truth for the rolling-window scanner rules. Imported by
compute_top_signals.py, scan_retrospective.py, scan_watchlist.py,
build_retro_review.py, and check_v3_single.py.

v3 rules over a rolling WINDOW (20 trading-day) end-anchored window:
  R1:  >= MIN_PCT_ABOVE % of closes above SMA20  (>=18 of 20 at 90%)
  R2b: sum(green bodies) >= BODY_RATIO * sum(red bodies)  (1.5x)
       Body-size weighted: captures Pete's "reds smaller than greens" intuition
       directly, instead of the brittle count-based R2a it replaces.
  R4:  SMA20 SLOPE_WINDOW-day slope > 0  (5-day positive)
  R5:  close > MIN_PRICE  (penny-stock floor; excludes pump-and-dumps)

Final 'pass' column is gated by MIN_STREAK_DAYS: a day only counts as a real
TOP detection if it is part of a continuous run of >= MIN_STREAK_DAYS days
where R1, R2b, R4, R5 all hold. Shorter qualifying runs are noise.

See memory/project_top_scanner_filter.md for the locked spec.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

WINDOW = 20
MIN_PCT_ABOVE = 90.0
BODY_RATIO = 1.5  # R2b: sum(green bodies) >= BODY_RATIO * sum(red bodies)
SLOPE_WINDOW = 5
MIN_PRICE = 5.0  # close must be > $5 on a qualifying day
MIN_STREAK_DAYS = 30  # core requirement: 30+ consecutive days of R1, R2b, R4, R5
MIN_BARS = 40  # need SMA20 valid across the 20-day window + slope lookback


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Return df sorted by date with sma20, sma50, color, body, slope columns added.

    Does not mutate the input.
    """
    out = df.sort_values("date").reset_index(drop=True).copy()
    out["sma20"] = out["close"].rolling(WINDOW).mean()
    out["sma50"] = out["close"].rolling(50).mean()
    out["color"] = (out["close"] >= out["open"]).map({True: "G", False: "R"})
    body = out["close"] - out["open"]
    out["green_body"] = body.clip(lower=0)
    out["red_body"] = (-body).clip(lower=0)
    out["sma20_slope_5d"] = out["sma20"].diff(SLOPE_WINDOW)
    return out


def rolling_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Return daily pass/fail + supporting metrics for one ticker's history.

    Input may have indicators already (from add_indicators) or not, we'll add
    them if missing. Output rows start at the first bar where SMA20 + slope are
    valid; earlier bars are dropped.

    Columns: date, close, sma20, pct_above, G, R, gbody_sum, rbody_sum, body_ratio,
             slope_5d, r1, r2b, r4, r5, raw_pass, pass.
    """
    needed = {"sma20", "sma20_slope_5d", "color", "green_body", "red_body"}
    if not needed.issubset(df.columns):
        df = add_indicators(df)
    else:
        df = df.sort_values("date").reset_index(drop=True)

    # Identify first index where all inputs are valid
    valid = df["sma20"].notna() & df["sma20_slope_5d"].notna()
    first_valid_idx = valid.idxmax() if valid.any() else None
    if first_valid_idx is None or not valid.iloc[first_valid_idx]:
        return pd.DataFrame(columns=[
            "date", "close", "sma20", "pct_above", "G", "R",
            "gbody_sum", "rbody_sum", "body_ratio", "slope_5d",
            "r1", "r2b", "r4", "r5", "raw_pass", "pass",
        ])

    start = max(first_valid_idx, WINDOW - 1)
    rows = []
    for i in range(start, len(df)):
        if not valid.iloc[i]:
            continue
        win = df.iloc[i - WINDOW + 1 : i + 1]
        cur = df.iloc[i]

        pct_above = (win["close"] > win["sma20"]).sum() / WINDOW * 100.0
        greens = int((win["color"] == "G").sum())
        reds = int((win["color"] == "R").sum())
        gbody = float(win["green_body"].sum())
        rbody = float(win["red_body"].sum())
        body_ratio = gbody / rbody if rbody > 0 else float("inf")

        r1 = pct_above >= MIN_PCT_ABOVE
        r2b = gbody >= BODY_RATIO * rbody
        r4 = cur["sma20_slope_5d"] > 0
        r5 = float(cur["close"]) > MIN_PRICE

        rows.append({
            "date": cur["date"],
            "close": round(float(cur["close"]), 2),
            "sma20": round(float(cur["sma20"]), 2),
            "pct_above": round(float(pct_above), 1),
            "G": greens,
            "R": reds,
            "gbody_sum": round(gbody, 2),
            "rbody_sum": round(rbody, 2),
            "body_ratio": round(body_ratio, 2) if rbody > 0 else None,
            "slope_5d": round(float(cur["sma20_slope_5d"]), 3),
            "r1": bool(r1),
            "r2b": bool(r2b),
            "r4": bool(r4),
            "r5": bool(r5),
            "raw_pass": bool(r1 and r2b and r4 and r5),
        })

    out = pd.DataFrame(rows)
    if out.empty:
        out["pass"] = []
        return out

    # Gate `pass` on MIN_STREAK_DAYS continuous raw_pass days. A day only
    # counts as a TOP detection if it's part of a run of >=30 consecutive
    # raw-passing days. This makes pump-and-dumps (which qualify briefly)
    # and single-catalyst gappers (which fail the duration test) fall out.
    raw = out["raw_pass"].to_numpy()
    final = [False] * len(raw)
    i = 0
    while i < len(raw):
        if raw[i]:
            j = i
            while j < len(raw) and raw[j]:
                j += 1
            if (j - i) >= MIN_STREAK_DAYS:
                for k in range(i, j):
                    final[k] = True
            i = j
        else:
            i += 1
    out["pass"] = final
    return out


def streaks_from_signals(
    signals: pd.DataFrame,
    min_length: int = 1,
) -> list[tuple[date, date, int]]:
    """Return contiguous qualifying streaks as (start, end, length) tuples.

    Counts in place: no per-streak dataframe filtering. signals must have
    'date' and 'pass' columns in chronological order.
    """
    streaks: list[tuple[date, date, int]] = []
    start = end = None
    length = 0
    for _, row in signals.iterrows():
        if row["pass"]:
            if start is None:
                start = row["date"]
                length = 0
            end = row["date"]
            length += 1
        else:
            if start is not None and length >= min_length:
                streaks.append((start, end, length))
            start = end = None
            length = 0
    if start is not None and length >= min_length:
        streaks.append((start, end, length))
    return streaks
