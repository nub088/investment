#!/usr/bin/env python3
"""Build a self-contained HTML review page for the latest TOP candidates.

Reads the most recent `.tmp/top_candidates_*.csv` and the matching
`.tmp/ohlcv_*.parquet`, and writes `.tmp/review_YYYY-MM-DD.html`.

The page renders one ticker at a time via TradingView's `lightweight-charts`
(CDN-loaded), with daily candlesticks + SMA20/SMA50 overlays, a metrics
sidebar, and keyboard navigation (← / → / j / k for prev/next, t for the
TradingView link).

Usage:
    python tools/build_review_page.py [--out path] [--open]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import webbrowser
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = ROOT / ".tmp"


def latest_file(pattern: str) -> Path:
    matches = sorted(TMP_DIR.glob(pattern))
    if not matches:
        sys.exit(f"ERROR: no files match {TMP_DIR}/{pattern}")
    return matches[-1]


def build_payload(candidates: pd.DataFrame, ohlcv: pd.DataFrame) -> list[dict]:
    """One JSON-serializable record per candidate, sorted as the CSV is."""
    out: list[dict] = []
    grouped = ohlcv.groupby("ticker")
    for _, row in candidates.iterrows():
        t = row["ticker"]
        if t not in grouped.groups:
            continue
        bars = grouped.get_group(t).sort_values("date").reset_index(drop=True)
        sma20 = bars["close"].rolling(20).mean()
        sma50 = bars["close"].rolling(50).mean()
        candles = []
        sma20_pts = []
        sma50_pts = []
        for i, b in bars.iterrows():
            date_str = b["date"].isoformat() if hasattr(b["date"], "isoformat") else str(b["date"])
            candles.append({
                "time": date_str,
                "open": float(b["open"]),
                "high": float(b["high"]),
                "low": float(b["low"]),
                "close": float(b["close"]),
            })
            if not pd.isna(sma20.iloc[i]):
                sma20_pts.append({"time": date_str, "value": float(sma20.iloc[i])})
            if not pd.isna(sma50.iloc[i]):
                sma50_pts.append({"time": date_str, "value": float(sma50.iloc[i])})
        out.append({
            "ticker": t,
            "name": str(row.get("name", "")),
            "exchange": str(row.get("exchange", "")),
            "last_close": float(row["last_close"]),
            "last_date": str(row["last_date"]),
            "metrics": {
                "pct_above_sma20": float(row["pct_above_sma20"]),
                "greens": int(row["greens"]),
                "reds": int(row["reds"]),
                "gr_ratio": float(row["gr_ratio"]),
                "sma20_slope_pct_5d": float(row["sma20_slope_pct_5d"]),
                "median_range_pct": float(row["median_range_pct"]),
                "max_range_pct": float(row["max_range_pct"]),
                "avg_abs_day_pct": float(row["avg_abs_day_pct"]),
                "big_red_days": int(row["big_red_days"]),
                "big_green_days": int(row["big_green_days"]),
                "dist_above_sma20_pct": float(row["dist_above_sma20_pct"]),
                "sma20": float(row["sma20"]),
                "sma50": float(row["sma50"]),
                "avg_vol_20d": int(row["avg_vol_20d"]),
            },
            "chart_url": str(row.get("chart_url", "")),
            "candles": candles,
            "sma20": sma20_pts,
            "sma50": sma50_pts,
        })
    return out


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>TOP review — {scan_date} ({n} candidates)</title>
<style>
  :root {{
    --bg: #0e1117;
    --panel: #161b22;
    --text: #e6edf3;
    --muted: #7d8590;
    --accent: #58a6ff;
    --green: #26a69a;
    --red: #ef5350;
    --border: #30363d;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; height: 100%; background: var(--bg); color: var(--text);
    font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
  #app {{ display: grid; grid-template-columns: 1fr 320px; grid-template-rows: 56px 1fr;
    height: 100vh; }}
  header {{ grid-column: 1 / -1; display: flex; align-items: center; justify-content: space-between;
    padding: 0 18px; border-bottom: 1px solid var(--border); background: var(--panel); }}
  header .left {{ display: flex; align-items: baseline; gap: 14px; }}
  header h1 {{ font-size: 18px; margin: 0; font-weight: 600; }}
  header .ticker {{ font-size: 22px; font-weight: 700; color: var(--accent); letter-spacing: 0.5px; }}
  header .name {{ color: var(--muted); font-size: 13px; max-width: 360px; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }}
  header .right {{ color: var(--muted); font-size: 12px; }}
  header kbd {{ background: #21262d; border: 1px solid var(--border); border-radius: 4px;
    padding: 2px 6px; font-family: ui-monospace, monospace; font-size: 11px; margin: 0 2px; }}
  #chart {{ position: relative; }}
  #chart .overlay {{ position: absolute; top: 12px; left: 14px; font-size: 12px; color: var(--muted);
    background: rgba(22,27,34,0.85); padding: 6px 10px; border-radius: 4px; pointer-events: none;
    border: 1px solid var(--border); }}
  #chart .overlay .legend {{ display: flex; gap: 14px; margin-top: 4px; }}
  #chart .overlay .legend span {{ display: inline-flex; align-items: center; gap: 4px; }}
  #chart .overlay .legend .swatch {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
  #measure-rect {{ position: absolute; pointer-events: none; display: none; z-index: 5; }}
  #measure-label {{ position: absolute; pointer-events: none; display: none; color: #0e1117;
    font-size: 12px; font-weight: 600; padding: 6px 9px; border-radius: 4px;
    font-family: ui-monospace, monospace; z-index: 6; box-shadow: 0 2px 8px rgba(0,0,0,0.4);
    white-space: nowrap; line-height: 1.4; }}
  #measure-label .secondary {{ font-weight: 500; opacity: 0.9; font-size: 11px; }}
  aside {{ border-left: 1px solid var(--border); background: var(--panel); overflow-y: auto;
    padding: 18px; font-size: 13px; }}
  aside h2 {{ margin: 0 0 6px 0; font-size: 13px; color: var(--muted); text-transform: uppercase;
    letter-spacing: 0.7px; font-weight: 600; }}
  aside section {{ margin-bottom: 22px; }}
  aside .row {{ display: flex; justify-content: space-between; padding: 4px 0;
    border-bottom: 1px dotted #21262d; }}
  aside .row .k {{ color: var(--muted); }}
  aside .row .v {{ font-family: ui-monospace, monospace; }}
  aside .row .v.good {{ color: var(--green); }}
  aside .row .v.bad {{ color: var(--red); }}
  aside a.tv {{ display: inline-block; margin-top: 8px; padding: 8px 12px; background: var(--accent);
    color: #0e1117; text-decoration: none; border-radius: 5px; font-weight: 600; font-size: 12px; }}
  aside a.tv:hover {{ filter: brightness(1.1); }}
  .progress {{ font-family: ui-monospace, monospace; font-size: 14px; color: var(--text); }}
</style>
</head>
<body>
<div id="app">
  <header>
    <div class="left">
      <div class="progress" id="progress">1 / {n}</div>
      <div class="ticker" id="hdr-ticker">—</div>
      <div class="name" id="hdr-name"></div>
    </div>
    <div class="right">
      <kbd>←</kbd><kbd>→</kbd> nav · <kbd>shift</kbd>+drag measure · <kbd>esc</kbd> clear · <kbd>t</kbd> TradingView · <kbd>g</kbd> go to #
    </div>
  </header>
  <div id="chart">
    <div id="measure-rect"></div>
    <div id="measure-label"></div>
    <div class="overlay">
      <div id="ov-close">—</div>
      <div class="legend">
        <span><span class="swatch" style="background:#26a69a"></span>Candles</span>
        <span><span class="swatch" style="background:#ffd166"></span>SMA20</span>
        <span><span class="swatch" style="background:#bb86fc"></span>SMA50</span>
      </div>
    </div>
  </div>
  <aside id="sidebar"></aside>
</div>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script>
const DATA = {payload};
const SCAN_DATE = "{scan_date}";

const chartEl = document.getElementById('chart');
const chart = LightweightCharts.createChart(chartEl, {{
  layout: {{ background: {{ type: 'solid', color: '#0e1117' }}, textColor: '#e6edf3' }},
  grid: {{ vertLines: {{ color: '#21262d' }}, horzLines: {{ color: '#21262d' }} }},
  rightPriceScale: {{ borderColor: '#30363d' }},
  timeScale: {{ borderColor: '#30363d', timeVisible: false, secondsVisible: false }},
  crosshair: {{ mode: 1 }},
}});

const candleSeries = chart.addCandlestickSeries({{
  upColor: '#26a69a', downColor: '#ef5350',
  borderUpColor: '#26a69a', borderDownColor: '#ef5350',
  wickUpColor: '#26a69a', wickDownColor: '#ef5350',
}});
const sma20Series = chart.addLineSeries({{ color: '#ffd166', lineWidth: 2, priceLineVisible: false, lastValueVisible: false }});
const sma50Series = chart.addLineSeries({{ color: '#bb86fc', lineWidth: 2, priceLineVisible: false, lastValueVisible: false }});

let idx = 0;

function fmt(n, d=2) {{ return (n === null || n === undefined || isNaN(n)) ? '—' : Number(n).toFixed(d); }}
function fmtPct(n, d=2) {{ return fmt(n, d) + '%'; }}
function fmtInt(n) {{ return Math.round(n).toLocaleString(); }}

function render() {{
  const c = DATA[idx];
  document.getElementById('progress').textContent = `${{idx + 1}} / ${{DATA.length}}`;
  document.getElementById('hdr-ticker').textContent = c.ticker;
  document.getElementById('hdr-name').textContent = c.name + (c.exchange ? ` · ${{c.exchange}}` : '');
  document.getElementById('ov-close').textContent = `${{c.ticker}}  $${{fmt(c.last_close)}}  (${{c.last_date}})`;

  candleSeries.setData(c.candles);
  sma20Series.setData(c.sma20);
  sma50Series.setData(c.sma50);
  chart.timeScale().fitContent();
  clearMeasure();

  const m = c.metrics;
  const slopeClass = m.sma20_slope_pct_5d >= 0 ? 'good' : 'bad';
  const distClass = m.dist_above_sma20_pct >= 0 ? 'good' : 'bad';
  const sidebar = document.getElementById('sidebar');
  sidebar.innerHTML = `
    <section>
      <h2>v3 Filter</h2>
      <div class="row"><span class="k">% above SMA20</span><span class="v good">${{fmtPct(m.pct_above_sma20, 0)}}</span></div>
      <div class="row"><span class="k">Greens : Reds</span><span class="v">${{m.greens}} : ${{m.reds}} <span style="color:var(--muted)">(${{fmt(m.gr_ratio)}}×)</span></span></div>
      <div class="row"><span class="k">SMA20 5d slope</span><span class="v ${{slopeClass}}">${{fmtPct(m.sma20_slope_pct_5d)}}</span></div>
    </section>
    <section>
      <h2>Tightness</h2>
      <div class="row"><span class="k">Median range</span><span class="v">${{fmtPct(m.median_range_pct)}}</span></div>
      <div class="row"><span class="k">Max range</span><span class="v">${{fmtPct(m.max_range_pct)}}</span></div>
      <div class="row"><span class="k">Avg |day %|</span><span class="v">${{fmtPct(m.avg_abs_day_pct)}}</span></div>
      <div class="row"><span class="k">Big green days</span><span class="v">${{m.big_green_days}}</span></div>
      <div class="row"><span class="k">Big red days</span><span class="v">${{m.big_red_days}}</span></div>
    </section>
    <section>
      <h2>Context</h2>
      <div class="row"><span class="k">Last close</span><span class="v">$${{fmt(c.last_close)}}</span></div>
      <div class="row"><span class="k">SMA20</span><span class="v">$${{fmt(m.sma20)}}</span></div>
      <div class="row"><span class="k">SMA50</span><span class="v">$${{fmt(m.sma50)}}</span></div>
      <div class="row"><span class="k">Dist above SMA20</span><span class="v ${{distClass}}">${{fmtPct(m.dist_above_sma20_pct)}}</span></div>
      <div class="row"><span class="k">Avg vol 20d</span><span class="v">${{fmtInt(m.avg_vol_20d)}}</span></div>
    </section>
    <section>
      <a class="tv" id="tv-link" href="${{c.chart_url}}" target="_blank" rel="noopener">Open in TradingView ↗</a>
    </section>
  `;
}}

function go(delta) {{
  idx = (idx + delta + DATA.length) % DATA.length;
  render();
}}

// --- Measure tool (shift+drag) ---
let measureAnchor = null;
let measureEnd = null;
let measuring = false;

const measureRect = document.getElementById('measure-rect');
const measureLabel = document.getElementById('measure-label');

function clearMeasure() {{
  measureAnchor = null;
  measureEnd = null;
  measuring = false;
  measureRect.style.display = 'none';
  measureLabel.style.display = 'none';
}}

function chartCoords(e) {{
  const r = chartEl.getBoundingClientRect();
  return {{ x: e.clientX - r.left, y: e.clientY - r.top }};
}}

function pointFromEvent(e) {{
  const {{ x, y }} = chartCoords(e);
  const price = candleSeries.coordinateToPrice(y);
  const time = chart.timeScale().coordinateToTime(x);
  return {{ x, y, price, time }};
}}

function drawMeasure() {{
  if (!measureAnchor || !measureEnd) return;
  const a = measureAnchor, b = measureEnd;
  if (a.price == null || b.price == null) return;

  const x1 = Math.min(a.x, b.x), x2 = Math.max(a.x, b.x);
  const y1 = Math.min(a.y, b.y), y2 = Math.max(a.y, b.y);

  const dPrice = b.price - a.price;
  const pct = (dPrice / a.price) * 100;
  const isUp = dPrice >= 0;

  // bar count between the two times
  let bars = 0;
  const c = DATA[idx].candles;
  if (a.time && b.time) {{
    const tA = String(a.time), tB = String(b.time);
    const tMin = tA < tB ? tA : tB;
    const tMax = tA < tB ? tB : tA;
    bars = c.filter(k => k.time >= tMin && k.time <= tMax).length;
  }}

  const upColor = '#26a69a', downColor = '#ef5350';
  const fill = isUp ? 'rgba(38,166,154,0.15)' : 'rgba(239,83,80,0.15)';
  const borderColor = isUp ? upColor : downColor;

  measureRect.style.display = 'block';
  measureRect.style.left = x1 + 'px';
  measureRect.style.top = y1 + 'px';
  measureRect.style.width = (x2 - x1) + 'px';
  measureRect.style.height = (y2 - y1) + 'px';
  measureRect.style.background = fill;
  measureRect.style.border = `1px dashed ${{borderColor}}`;

  const sign = isUp ? '+' : '';
  measureLabel.style.display = 'block';
  measureLabel.style.background = borderColor;
  // place label near the endpoint, but keep it on-screen
  const labelX = Math.min(b.x + 10, chartEl.clientWidth - 130);
  const labelY = Math.min(b.y + 10, chartEl.clientHeight - 50);
  measureLabel.style.left = labelX + 'px';
  measureLabel.style.top = labelY + 'px';
  measureLabel.innerHTML =
    `<div>${{sign}}${{dPrice.toFixed(2)}}  (${{sign}}${{pct.toFixed(2)}}%)</div>` +
    `<div class="secondary">${{bars}} bars · ${{a.price.toFixed(2)}} → ${{b.price.toFixed(2)}}</div>`;
}}

// Disable the chart's built-in pan while Shift is held so the measure
// tool gets exclusive control of drag.
document.addEventListener('keydown', (e) => {{
  if (e.key === 'Shift') {{
    chart.applyOptions({{ handleScroll: {{ pressedMouseMove: false }} }});
  }}
}});
document.addEventListener('keyup', (e) => {{
  if (e.key === 'Shift') {{
    chart.applyOptions({{ handleScroll: {{ pressedMouseMove: true }} }});
    measuring = false;
  }}
}});

chartEl.addEventListener('mousedown', (e) => {{
  if (!e.shiftKey) {{
    if (measureAnchor) clearMeasure();
    return;
  }}
  e.preventDefault();
  e.stopPropagation();
  measuring = true;
  measureAnchor = pointFromEvent(e);
  measureEnd = measureAnchor;
  drawMeasure();
}}, true);

chartEl.addEventListener('mousemove', (e) => {{
  if (!measuring) return;
  measureEnd = pointFromEvent(e);
  drawMeasure();
}});

window.addEventListener('mouseup', (e) => {{
  if (!measuring) return;
  measuring = false;
  // keep the measurement visible after release; cleared on next non-shift click,
  // navigation, or Esc
}});

function goTo() {{
  const n = prompt(`Go to candidate # (1 - ${{DATA.length}}):`);
  if (n === null) return;
  const v = parseInt(n, 10);
  if (!isNaN(v) && v >= 1 && v <= DATA.length) {{ idx = v - 1; render(); }}
}}

document.addEventListener('keydown', (e) => {{
  if (e.target.tagName === 'INPUT') return;
  switch (e.key) {{
    case 'ArrowRight': case 'j': case 'l': case ' ': go(1); e.preventDefault(); break;
    case 'ArrowLeft':  case 'k': case 'h':            go(-1); e.preventDefault(); break;
    case 't': case 'T': window.open(DATA[idx].chart_url, '_blank'); break;
    case 'g': case 'G': goTo(); break;
    case 'Home': idx = 0; render(); break;
    case 'End':  idx = DATA.length - 1; render(); break;
    case 'Escape': clearMeasure(); break;
  }}
}});

new ResizeObserver(() => chart.applyOptions({{ width: chartEl.clientWidth, height: chartEl.clientHeight }})).observe(chartEl);

render();
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=None,
                        help="Path to top_candidates CSV (default: latest in .tmp/)")
    parser.add_argument("--ohlcv", type=Path, default=None,
                        help="Path to ohlcv parquet (default: latest in .tmp/)")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output HTML path (default: .tmp/review_YYYY-MM-DD.html)")
    parser.add_argument("--open", action="store_true",
                        help="Open the resulting HTML in the default browser")
    args = parser.parse_args()

    cand_path = args.candidates or latest_file("top_candidates_*.csv")
    ohlcv_path = args.ohlcv or latest_file("ohlcv_*.parquet")
    today = dt.date.today().isoformat()
    out_path = args.out or (TMP_DIR / f"review_{today}.html")

    print(f"Reading candidates : {cand_path}", file=sys.stderr)
    print(f"Reading OHLCV      : {ohlcv_path}", file=sys.stderr)

    candidates = pd.read_csv(cand_path)
    ohlcv = pd.read_parquet(ohlcv_path)
    ohlcv["date"] = pd.to_datetime(ohlcv["date"]).dt.date

    payload = build_payload(candidates, ohlcv)
    scan_date = str(candidates["scan_date"].iloc[0]) if "scan_date" in candidates.columns else today
    html = HTML_TEMPLATE.format(
        payload=json.dumps(payload, separators=(",", ":")),
        n=len(payload),
        scan_date=scan_date,
    )
    out_path.write_text(html, encoding="utf-8")
    print(f"Wrote {out_path}  ({len(payload)} candidates, {out_path.stat().st_size // 1024} KB)",
          file=sys.stderr)

    if args.open:
        webbrowser.open(f"file://{out_path.resolve()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
