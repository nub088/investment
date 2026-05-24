# Workflow: Weekly TOP Pattern Scan (Price Action)

## Objective

Produce a weekly shortlist of US-listed stocks that pass the **v3 TOP scanner filter**
(Tight Orderly Progression — Pete Stolzers). The shortlist is not a buy list — it's
the universe of candidates worth visually reviewing on the daily chart for full TOP
character (tightness, AVWAP-E support, relative strength, catalyst context).

This is **Phase 1: Price Action**. Fundamentals are human judgment, not automated.

## When to run

- **Weekly** — Sunday evening or Monday pre-open.
- Universe rebuild is **monthly** (the listed universe changes slowly).

## Prerequisites

- Python venv at `.venv/` with `requirements.txt` installed
- `STOOQ_API_KEY` set in `.env`
- Internet access for NASDAQ Trader + Stooq

```bash
source .venv/bin/activate          # bash/zsh
# or: source .venv/bin/activate.fish
```

## Steps

### 1. (Monthly) Rebuild the universe

```bash
python tools/build_universe.py
```

- Pulls `nasdaqlisted.txt` + `otherlisted.txt` from NASDAQ Trader
- Filters to common stocks (drops ETFs, warrants, rights, units, preferred shares,
  test issues, financially-deficient names)
- Keeps tickers with `avg_vol_20d >= 2_000_000`
- Writes `.tmp/universe.csv`

Skip this if `.tmp/universe.csv` is less than ~30 days old.

### 2. Fetch fresh daily bars (Stooq)

```bash
python tools/fetch_daily_ohlcv.py [--workers 8] [--days 120]
```

- Reads `.tmp/universe.csv`
- Pulls ~80 trading days (default 120 calendar) of daily OHLCV from Stooq in parallel
- Writes `.tmp/ohlcv_YYYY-MM-DD.parquet` (columns: ticker, date, open, high, low, close, volume)

Runtime: ~5-10 minutes for ~1,800 tickers with 8 workers.

### 3. Compute v3 signals + filter

```bash
python tools/compute_top_signals.py
```

- Reads the latest `.tmp/ohlcv_*.parquet`
- Evaluates the v3 LOCKED filter over a rolling **20 trading-day window
  end-anchored to the most recent bar**:
  - **R1:** ≥90% of closes in window above SMA20 (≥18 of 20)
  - **R2a:** greens ≥ 1.75 × reds (literal "75% more greens than reds")
  - **R4:** SMA20 5-day slope > 0
- Computes tiebreaker metrics for measured-vs-explosive classification:
  median range %, max range %, avg |day%|, big red/green day counts
- Writes `.tmp/top_candidates_YYYY-MM-DD.csv` sorted measured-first
  (low median range × high G:R × steep slope)

**Expected output size:** 30–150 names (much smaller than prior single-day filter, which
produced 300–900). v3 trades early detection for precision — the 20-day window means
**a name only appears ~1 month after its visual TOP-start**.

### 4. Review the output

Open `.tmp/top_candidates_YYYY-MM-DD.csv` in a spreadsheet tool.

**Recommended manual review pass:**

1. Default sort puts **measured TOPs first** — those are the highest-confidence
   stealth-accumulation candidates (per [[feedback-top-review-tiebreaker]]).
2. For each name, click `chart_url`, look at the daily chart.
3. Walk back ~30 days from `last_date` to identify the **visual TOP-start**:
   typically a reversal/change-of-character candle from a base, often still
   below SMA20. This is the entry context — useful for sizing/risk but not
   the entry timing (entry is "now" = scanner firing).
4. Apply full pattern checklist from [[project-top-pattern-definition]]:
   tight rhythm, EMA-8 hugging, shallow dips, AVWAP-E support, recent earnings.
5. Drop names that look choppy, gap-prone, or where the 20-day window passed
   the filter only because of one big catalyst gap.

## Signal definition (v3 LOCKED)

A ticker appears in the output if AND only if, over the rolling 20 trading-day
window ending at the most recent bar:

1. `avg_vol_20d >= 2_000_000` (gate applied at universe build)
2. **R1:** at least 18 of 20 daily closes are above their respective SMA20 value
3. **R2a:** green candle count ≥ 1.75 × red candle count
4. **R4:** SMA20 today > SMA20 five trading days ago

Versioning: v3 (2026-05-20). See `project_top_scanner_filter.md` memory for full spec
and validation evidence against SNDK/WDC/GLW/BE/RBLX training tickers.

## Edge cases & known quirks

- **Stooq returns "no data" for some tickers** (delisted, ticker mismatch, very new
  listings). Logged at end of fetch. Don't worry unless > 5% of universe is missing.
- **Tickers with dots** (e.g., `BRK.B`) are normalized to dashes (`brk-b.us`) for Stooq.
- **TOP cannot be detected at inception.** The rolling 20-day window means we
  surface a name only after ~1 month of pattern formation. This is by design,
  not a bug. See [[project-top-scanner-filter]] for details.
- **Universe drift**: ticker delistings between universe-build and fetch result
  in empty data for those names — they simply don't appear in the output.

## Verification (after each run)

- [ ] Output row count plausible (30–150 names typical)
- [ ] Known TOP exemplars from training set (`WDC`, `GLW` etc.) present when their
      historical pattern would currently qualify
- [ ] No NaN values in numeric columns
- [ ] `last_date` is the most recent US trading day
- [ ] Spot-check SMA20 for 1-2 random tickers against TradingView (≤ 0.5% diff)

## Outputs delivered

- `.tmp/top_candidates_YYYY-MM-DD.csv` — the weekly shortlist
- Google Sheets push is **deferred** (Phase 1.5). The CSV stands in for now.

## Self-improvement notes

When something breaks or surprises you, update this workflow:
- Stooq rate limit issues? Document the worker count that worked and any required delays.
- Discovered a TOP-pattern winner the v3 filter missed? Note ticker + window. Add to
  the 5-ticker training set in `project_top_training_queue.md` once user confirms.
- Discovered noise in the output? Don't add a rule unilaterally — flag for user review.
  The training set is locked; rule changes need validation against all 5 examples.
