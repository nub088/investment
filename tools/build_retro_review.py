#!/usr/bin/env python3
"""Build a self-contained HTML review page for the retrospective TOP scan.

Reads .tmp/top_retrospective_<since>.csv (summary) + .tmp/signals_<scan_date>.parquet
(daily pass/fail history, produced by scan_retrospective.py) + the latest OHLCV
parquet. Renders one ticker at a time with:
  - Daily candlesticks + SMA20/SMA50
  - Green shading behind candles over qualifying TOP windows
  - Sidebar showing first-qual date, streaks, pass%, current status

If the signals parquet is missing (e.g. older scan output), falls back to
recomputing signals on the fly.

Usage:
    python tools/build_retro_review.py [--since 2026-01-01] [--out path] [--open]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import webbrowser
from pathlib import Path

import pandas as pd

from v3_filter import rolling_signals

ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = ROOT / ".tmp"

SUMMARY_MAX_CHARS = 320  # truncated for sidebar display


def latest_match(pattern: str) -> Path | None:
    paths = sorted(TMP_DIR.glob(pattern))
    return paths[-1] if paths else None


def short_description(text: str, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    """Truncate at the nearest sentence boundary <= max_chars; hard-cut only if
    no sentence break exists in the window (one giant sentence)."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last_period = cut.rfind(". ")
    if last_period > 0:
        return cut[: last_period + 1]
    return cut.rstrip() + "…"


def build_payload(
    retro: pd.DataFrame,
    ohlcv: pd.DataFrame,
    signals_by_ticker: dict[str, set[str]],
    company_by_ticker: dict[str, dict],
) -> list[dict]:
    grouped = ohlcv.groupby("ticker")
    out: list[dict] = []

    for _, row in retro.iterrows():
        t = row["ticker"]
        if t not in grouped.groups:
            continue

        bars = grouped.get_group(t).sort_values("date").reset_index(drop=True)
        sma20 = bars["close"].rolling(20).mean()
        sma50 = bars["close"].rolling(50).mean()

        candles, sma20_pts, sma50_pts = [], [], []
        for i, b in bars.iterrows():
            ds = b["date"].isoformat() if hasattr(b["date"], "isoformat") else str(b["date"])
            candles.append({
                "time": ds, "open": float(b["open"]), "high": float(b["high"]),
                "low": float(b["low"]), "close": float(b["close"]),
            })
            if not pd.isna(sma20.iloc[i]):
                sma20_pts.append({"time": ds, "value": round(float(sma20.iloc[i]), 4)})
            if not pd.isna(sma50.iloc[i]):
                sma50_pts.append({"time": ds, "value": round(float(sma50.iloc[i]), 4)})

        # Pull qualifying dates from cached signals; fall back to recompute if absent.
        if t in signals_by_ticker:
            qualifying_dates = signals_by_ticker[t]
        else:
            sig = rolling_signals(bars)
            qualifying_dates = {
                (r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]))
                for _, r in sig.iterrows() if r["pass"]
            }

        company = company_by_ticker.get(t, {})

        out.append({
            "ticker": t,
            "name": company.get("name", ""),
            "sector": company.get("sector", ""),
            "industry": company.get("industry", ""),
            "summary": short_description(company.get("summary", "")),
            "first_qual": str(row["first_qual_date"]),
            "pass_pct": float(row["pass_pct"]),
            "pass_days": int(row["pass_days"]),
            "longest_streak": int(row["longest_streak_days"]),
            "currently_passing": bool(row["currently_passing"]),
            "streaks_text": str(row["streaks"]),
            "last_close": float(row["last_close"]),
            "last_sma20": float(row["last_sma20"]),
            "chart_url": f"https://www.tradingview.com/chart/?symbol={t}",
            "candles": candles,
            "sma20": sma20_pts,
            "sma50": sma50_pts,
            "qualifying_dates": sorted(qualifying_dates),
        })

    return out


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>TOP retro review — {since} ({n} tickers)</title>
<style>
  :root {{
    --bg: #0e1117; --panel: #161b22; --text: #e6edf3; --muted: #7d8590;
    --accent: #58a6ff; --green: #26a69a; --red: #ef5350; --border: #30363d;
    --gold: #ffd166; --purple: #bb86fc;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; height: 100%; background: var(--bg); color: var(--text);
    font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
  #app {{ display: grid; grid-template-columns: 1fr 340px; grid-template-rows: 56px 1fr; height: 100vh; }}
  header {{ grid-column: 1 / -1; display: flex; align-items: center; justify-content: space-between;
    padding: 0 18px; border-bottom: 1px solid var(--border); background: var(--panel); }}
  header .left {{ display: flex; align-items: baseline; gap: 14px; min-width: 0; }}
  header .ticker {{ font-size: 22px; font-weight: 700; color: var(--accent); letter-spacing: 0.5px; }}
  header .company-name {{ font-size: 14px; color: var(--text); max-width: 360px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  header .sector-pill {{ font-size: 11px; color: var(--muted); padding: 2px 8px;
    background: #21262d; border: 1px solid var(--border); border-radius: 10px;
    white-space: nowrap; }}
  header .badge {{ font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 10px; }}
  header .badge.pass {{ background: rgba(38,166,154,0.2); color: var(--green); border: 1px solid rgba(38,166,154,0.4); }}
  header .badge.fail {{ background: rgba(100,100,100,0.15); color: var(--muted); border: 1px solid var(--border); }}
  header .right {{ color: var(--muted); font-size: 12px; }}
  header kbd {{ background: #21262d; border: 1px solid var(--border); border-radius: 4px;
    padding: 2px 6px; font-family: ui-monospace, monospace; font-size: 11px; margin: 0 2px; }}
  #chart {{ position: relative; }}
  #chart .overlay {{ position: absolute; top: 12px; left: 14px; font-size: 12px; color: var(--muted);
    background: rgba(22,27,34,0.85); padding: 6px 10px; border-radius: 4px; pointer-events: none;
    border: 1px solid var(--border); z-index: 2; }}
  #chart .overlay .legend {{ display: flex; gap: 14px; margin-top: 4px; flex-wrap: wrap; }}
  #chart .overlay .legend span {{ display: inline-flex; align-items: center; gap: 4px; }}
  #chart .overlay .legend .swatch {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
  #measure-rect {{ position: absolute; pointer-events: none; display: none; z-index: 5; }}
  #measure-label {{ position: absolute; pointer-events: none; display: none; color: #0e1117;
    font-size: 12px; font-weight: 600; padding: 6px 9px; border-radius: 4px;
    font-family: ui-monospace, monospace; z-index: 6; box-shadow: 0 2px 8px rgba(0,0,0,0.4);
    white-space: nowrap; line-height: 1.4; }}
  aside {{ border-left: 1px solid var(--border); background: var(--panel); overflow-y: auto;
    padding: 18px; font-size: 13px; }}
  aside h2 {{ margin: 0 0 6px 0; font-size: 11px; color: var(--muted); text-transform: uppercase;
    letter-spacing: 0.8px; font-weight: 600; }}
  aside section {{ margin-bottom: 20px; }}
  aside .row {{ display: flex; justify-content: space-between; padding: 4px 0;
    border-bottom: 1px dotted #21262d; gap: 8px; }}
  aside .row .k {{ color: var(--muted); flex-shrink: 0; }}
  aside .row .v {{ font-family: ui-monospace, monospace; text-align: right; }}
  aside .row .v.good {{ color: var(--green); }}
  aside .row .v.bad {{ color: var(--red); }}
  aside .row .v.gold {{ color: var(--gold); }}
  aside .streak-list {{ font-family: ui-monospace, monospace; font-size: 12px;
    line-height: 1.8; color: var(--text); }}
  aside .streak-list .item {{ padding: 3px 0; border-bottom: 1px dotted #21262d; display: flex;
    justify-content: space-between; gap: 8px; }}
  aside .streak-list .item .dates {{ color: var(--accent); }}
  aside .streak-list .item .len {{ color: var(--muted); }}
  aside .company-summary {{ font-size: 12.5px; line-height: 1.5; color: var(--text);
    margin-top: 4px; }}
  aside .company-meta {{ font-size: 11px; color: var(--muted); margin: 4px 0 8px;
    text-transform: uppercase; letter-spacing: 0.6px; font-weight: 600; }}
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
      <div class="company-name" id="hdr-name"></div>
      <span class="sector-pill" id="hdr-sector"></span>
      <span class="badge" id="hdr-badge"></span>
    </div>
    <div class="right">
      <kbd>←</kbd><kbd>→</kbd> nav &middot; <kbd>shift</kbd>+drag measure &middot; <kbd>esc</kbd> clear &middot; <kbd>t</kbd> TradingView &middot; <kbd>g</kbd> go to #
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
        <span><span class="swatch" style="background:rgba(38,166,154,0.22)"></span>TOP window</span>
      </div>
    </div>
  </div>
  <aside id="sidebar"></aside>
</div>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script>
const DATA = {payload};
const SINCE = "{since}";

const chartEl = document.getElementById('chart');
const chart = LightweightCharts.createChart(chartEl, {{
  layout: {{ background: {{ type: 'solid', color: '#0e1117' }}, textColor: '#e6edf3' }},
  grid: {{ vertLines: {{ color: '#21262d' }}, horzLines: {{ color: '#21262d' }} }},
  rightPriceScale: {{ borderColor: '#30363d' }},
  timeScale: {{ borderColor: '#30363d', timeVisible: false }},
  crosshair: {{ mode: 1 }},
}});

// Z-order: series render in add-order. Shade goes FIRST so candles draw on top.
// Use a separate priceScale + constant value so every qualifying bar has
// the SAME visual height regardless of price level.
const shadeSeries = chart.addHistogramSeries({{
  color: 'rgba(38,166,154,0.22)',
  priceScaleId: 'shade',
  lastValueVisible: false,
  priceLineVisible: false,
}});
chart.priceScale('shade').applyOptions({{
  scaleMargins: {{ top: 0, bottom: 0 }},
  visible: false,
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
function fmtPct(n, d=1) {{ return fmt(n, d) + '%'; }}

function parseStreaks(s) {{
  if (!s) return [];
  return s.split(';').map(p => p.trim()).filter(Boolean).map(seg => {{
    const m = seg.match(/(\S+)→(\S+)\((\d+)d\)/);
    return m ? {{ start: m[1], end: m[2], days: parseInt(m[3]) }} : null;
  }}).filter(Boolean);
}}

function escapeHtml(s) {{
  if (!s) return '';
  return s.replace(/[&<>"']/g, ch => ({{ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":"&#39;" }}[ch]));
}}

function render() {{
  const c = DATA[idx];
  document.getElementById('progress').textContent = `${{idx + 1}} / ${{DATA.length}}`;
  document.getElementById('hdr-ticker').textContent = c.ticker;
  document.getElementById('hdr-name').textContent = c.name || '';
  const sectorPill = document.getElementById('hdr-sector');
  if (c.sector || c.industry) {{
    sectorPill.textContent = [c.sector, c.industry].filter(Boolean).join(' · ');
    sectorPill.style.display = '';
  }} else {{
    sectorPill.style.display = 'none';
  }}
  const badge = document.getElementById('hdr-badge');
  if (c.currently_passing) {{
    badge.textContent = 'PASSING';
    badge.className = 'badge pass';
  }} else {{
    badge.textContent = 'not passing';
    badge.className = 'badge fail';
  }}

  const lastCandle = c.candles[c.candles.length - 1];
  document.getElementById('ov-close').textContent =
    `${{c.ticker}}  $${{fmt(c.last_close)}}  (${{lastCandle ? lastCandle.time : '—'}})`;

  candleSeries.setData(c.candles);
  sma20Series.setData(c.sma20);
  sma50Series.setData(c.sma50);

  // Constant value = uniform bar height. With scaleMargins {{top:0,bottom:0}}
  // and visible:false, the bars fill the full chart vertical range.
  const qualSet = new Set(c.qualifying_dates);
  const shadeData = c.candles
    .filter(b => qualSet.has(b.time))
    .map(b => ({{ time: b.time, value: 1 }}));
  shadeSeries.setData(shadeData);

  chart.timeScale().fitContent();
  clearMeasure();

  const streaks = parseStreaks(c.streaks_text);
  const streakRows = streaks.map(s =>
    `<div class="item"><span class="dates">${{s.start}} → ${{s.end}}</span><span class="len">${{s.days}}d</span></div>`
  ).join('');

  const companyHtml = (c.summary || c.sector || c.industry)
    ? `<section>
        <h2>Company</h2>
        <div class="company-meta">${{escapeHtml([c.sector, c.industry].filter(Boolean).join(' · '))}}</div>
        <div class="company-summary">${{escapeHtml(c.summary) || '<span style="color:var(--muted)">no description on file</span>'}}</div>
       </section>`
    : '';

  document.getElementById('sidebar').innerHTML = `
    ${{companyHtml}}
    <section>
      <h2>Retrospective (since ${{SINCE}})</h2>
      <div class="row"><span class="k">First TOP signal</span><span class="v gold">${{c.first_qual}}</span></div>
      <div class="row"><span class="k">Pass rate</span><span class="v">${{fmtPct(c.pass_pct, 1)}}</span></div>
      <div class="row"><span class="k">Qualifying days</span><span class="v">${{c.pass_days}}</span></div>
      <div class="row"><span class="k">Longest streak</span><span class="v good">${{c.longest_streak}}d</span></div>
    </section>
    <section>
      <h2>Qualifying streaks</h2>
      <div class="streak-list">${{streakRows || '<span style="color:var(--muted)">none recorded</span>'}}</div>
    </section>
    <section>
      <h2>Current state</h2>
      <div class="row"><span class="k">Status</span><span class="v ${{c.currently_passing ? 'good' : 'bad'}}">${{c.currently_passing ? 'PASSING' : 'not passing'}}</span></div>
      <div class="row"><span class="k">Last close</span><span class="v">$${{fmt(c.last_close)}}</span></div>
      <div class="row"><span class="k">SMA20</span><span class="v">$${{fmt(c.last_sma20)}}</span></div>
      <div class="row"><span class="k">Dist above SMA20</span><span class="v ${{(c.last_close/c.last_sma20 - 1) >= 0 ? 'good' : 'bad'}}">${{fmtPct((c.last_close/c.last_sma20 - 1)*100)}}</span></div>
    </section>
    <section>
      <a class="tv" href="${{c.chart_url}}" target="_blank" rel="noopener">Open in TradingView ↗</a>
    </section>
  `;
}}

function go(delta) {{
  idx = (idx + delta + DATA.length) % DATA.length;
  render();
}}

// --- Measure tool ---
let measureAnchor = null, measureEnd = null, measuring = false;
const measureRect = document.getElementById('measure-rect');
const measureLabel = document.getElementById('measure-label');

function clearMeasure() {{
  measureAnchor = measureEnd = null; measuring = false;
  measureRect.style.display = 'none'; measureLabel.style.display = 'none';
}}
function chartCoords(e) {{ const r = chartEl.getBoundingClientRect(); return {{ x: e.clientX - r.left, y: e.clientY - r.top }}; }}
function pointFromEvent(e) {{
  const {{ x, y }} = chartCoords(e);
  return {{ x, y, price: candleSeries.coordinateToPrice(y), time: chart.timeScale().coordinateToTime(x) }};
}}
function drawMeasure() {{
  if (!measureAnchor || !measureEnd) return;
  const a = measureAnchor, b = measureEnd;
  if (a.price == null || b.price == null) return;
  const x1 = Math.min(a.x,b.x), x2 = Math.max(a.x,b.x);
  const y1 = Math.min(a.y,b.y), y2 = Math.max(a.y,b.y);
  const dPrice = b.price - a.price, pct = dPrice/a.price*100, isUp = dPrice >= 0;
  let bars = 0;
  if (a.time && b.time) {{
    const tMin = String(a.time) < String(b.time) ? String(a.time) : String(b.time);
    const tMax = String(a.time) < String(b.time) ? String(b.time) : String(a.time);
    bars = DATA[idx].candles.filter(k => k.time >= tMin && k.time <= tMax).length;
  }}
  const col = isUp ? '#26a69a' : '#ef5350';
  measureRect.style.cssText = `display:block;left:${{x1}}px;top:${{y1}}px;width:${{x2-x1}}px;height:${{y2-y1}}px;background:${{isUp?'rgba(38,166,154,0.15)':'rgba(239,83,80,0.15)'}};border:1px dashed ${{col}};position:absolute;pointer-events:none;z-index:5;`;
  const sign = isUp ? '+' : '';
  measureLabel.style.cssText = `display:block;left:${{Math.min(b.x+10,chartEl.clientWidth-130)}}px;top:${{Math.min(b.y+10,chartEl.clientHeight-50)}}px;background:${{col}};position:absolute;pointer-events:none;z-index:6;color:#0e1117;font-size:12px;font-weight:600;padding:6px 9px;border-radius:4px;font-family:ui-monospace,monospace;white-space:nowrap;line-height:1.4;`;
  measureLabel.innerHTML = `<div>${{sign}}${{dPrice.toFixed(2)}} (${{sign}}${{pct.toFixed(2)}}%)</div><div style="font-weight:500;opacity:.9;font-size:11px">${{bars}} bars · ${{a.price.toFixed(2)}} → ${{b.price.toFixed(2)}}</div>`;
}}

document.addEventListener('keydown', e => {{ if (e.key === 'Shift') chart.applyOptions({{ handleScroll: {{ pressedMouseMove: false }} }}); }});
document.addEventListener('keyup', e => {{ if (e.key === 'Shift') {{ chart.applyOptions({{ handleScroll: {{ pressedMouseMove: true }} }}); measuring = false; }} }});
chartEl.addEventListener('mousedown', e => {{
  if (!e.shiftKey) {{ if (measureAnchor) clearMeasure(); return; }}
  e.preventDefault(); e.stopPropagation(); measuring = true;
  measureAnchor = measureEnd = pointFromEvent(e); drawMeasure();
}}, true);
chartEl.addEventListener('mousemove', e => {{ if (!measuring) return; measureEnd = pointFromEvent(e); drawMeasure(); }});
window.addEventListener('mouseup', () => {{ if (measuring) measuring = false; }});

function goTo() {{
  const n = prompt(`Go to # (1-${{DATA.length}}):`);
  if (!n) return;
  const v = parseInt(n);
  if (!isNaN(v) && v >= 1 && v <= DATA.length) {{ idx = v - 1; render(); }}
}}

document.addEventListener('keydown', e => {{
  if (e.target.tagName === 'INPUT') return;
  switch(e.key) {{
    case 'ArrowRight': case 'j': case 'l': case ' ': go(1); e.preventDefault(); break;
    case 'ArrowLeft':  case 'k': case 'h':           go(-1); e.preventDefault(); break;
    case 't': case 'T': window.open(DATA[idx].chart_url, '_blank'); break;
    case 'g': case 'G': goTo(); break;
    case 'Home': idx = 0; render(); break;
    case 'End':  idx = DATA.length-1; render(); break;
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
    parser.add_argument("--since", default="2026-01-01")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()

    retro_path = TMP_DIR / f"top_retrospective_{args.since}.csv"
    if not retro_path.exists():
        sys.exit(f"ERROR: {retro_path} not found. Run scan_retrospective.py --since {args.since} first.")

    ohlcv_path = latest_match("ohlcv_*.parquet")
    if not ohlcv_path:
        sys.exit("ERROR: no .tmp/ohlcv_*.parquet found.")

    out_path = args.out or (TMP_DIR / f"review_retro_{args.since}.html")

    print(f"Reading retro CSV  : {retro_path}", file=sys.stderr)
    print(f"Reading OHLCV      : {ohlcv_path}", file=sys.stderr)

    retro = pd.read_csv(retro_path)
    retro["first_qual_date"] = pd.to_datetime(retro["first_qual_date"]).dt.date
    # CSV is already sorted (currently-passing first, longest streak); preserve order.

    ohlcv = pd.read_parquet(ohlcv_path)
    ohlcv["date"] = pd.to_datetime(ohlcv["date"]).dt.date

    # Load cached signals if available — avoids recomputing v3 history for 464 tickers.
    signals_by_ticker: dict[str, set[str]] = {}
    signals_path = latest_match("signals_*.parquet")
    if signals_path:
        print(f"Reading signals    : {signals_path}", file=sys.stderr)
        sigs = pd.read_parquet(signals_path)
        sigs["date"] = pd.to_datetime(sigs["date"]).dt.date
        passing = sigs[sigs["pass"]]
        for t, grp in passing.groupby("ticker"):
            signals_by_ticker[t] = {d.isoformat() for d in grp["date"]}
    else:
        print("  (no signals parquet — will recompute on the fly)", file=sys.stderr)

    # Load company info cache if present
    company_by_ticker: dict[str, dict] = {}
    company_path = TMP_DIR / "company_info.parquet"
    if company_path.exists():
        print(f"Reading company   : {company_path}", file=sys.stderr)
        ci = pd.read_parquet(company_path)
        for _, r in ci.iterrows():
            company_by_ticker[r["ticker"]] = {
                "name": r.get("name", "") or "",
                "sector": r.get("sector", "") or "",
                "industry": r.get("industry", "") or "",
                "summary": r.get("summary", "") or "",
            }
        missing = [t for t in retro["ticker"] if t not in company_by_ticker]
        if missing:
            print(f"  WARNING: {len(missing)} retro tickers have no company info "
                  f"(run: python tools/fetch_company_info.py)", file=sys.stderr)
    else:
        print("  (no company_info.parquet — sidebar will omit descriptions; "
              "run fetch_company_info.py)", file=sys.stderr)

    print(f"Building payload for {len(retro)} tickers...", file=sys.stderr)
    payload = build_payload(retro, ohlcv, signals_by_ticker, company_by_ticker)

    html = HTML_TEMPLATE.format(
        payload=json.dumps(payload, separators=(",", ":")),
        n=len(payload),
        since=args.since,
    )
    out_path.write_text(html, encoding="utf-8")
    print(f"Wrote {out_path}  ({len(payload)} tickers, {out_path.stat().st_size // 1024} KB)",
          file=sys.stderr)

    if args.open:
        webbrowser.open(f"file://{out_path.resolve()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
