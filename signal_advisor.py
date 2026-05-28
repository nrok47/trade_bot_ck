"""
Signal Advisor — scan 4 เหรียญ แล้วตัดสินใจเองว่าจะเข้าหรือไม่

Usage:
  python signal_advisor.py              # print to console
  python signal_advisor.py --report     # generate HTML + open browser
  python signal_advisor.py --watch      # refresh ทุก 60 วินาที
  python signal_advisor.py --leverage 10 --capital 500
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import webbrowser
from datetime import datetime, timezone

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
from dotenv import load_dotenv
load_dotenv()

from binance_client import binance
from indicators import (
    CandleData, ema, rsi as calc_rsi, atr as calc_atr,
    bollinger_bands, adx as calc_adx, vwap as calc_vwap,
)

# ── Constants ─────────────────────────────────────────────────────────────────

SYMBOLS        = ["BTCUSDT", "DOGEUSDT", "BNBUSDT", "XRPUSDT"]
HISTORY_FILE   = "signal_history.json"
LOG_FILE       = "signal_log.csv"
REPORT_FILE    = "signal_report.html"
HISTORY_MAX    = 120

TF_CONFIG = {
    "3m":  {"limit": 240, "weight": 1.0},
    "5m":  {"limit": 144, "weight": 1.5},
    "15m": {"limit":  48, "weight": 2.0},
}

SCORE_THRESHOLD  = 3.0
EMA_GAP_PCT      = 0.03
RSI_OVERSOLD     = 40
RSI_OVERBOUGHT   = 60
VOL_SURGE_MULT   = 2.0
ADX_TRENDING     = 25
ADX_WEAK         = 20
ADX_RANGING      = 15
HURST_RANGING    = 0.45

DEFAULT_LEVERAGE    = 8
DEFAULT_CAPITAL_PCT = 0.20
TP_ROE_PCT          = 30.0
SL_HARD_ROE_PCT     = 15.0
SL_SOFT_ROE_PCT     = 10.0
TAKER_FEE           = 0.0004

_fng_cache: tuple[float, int] = (0.0, 50)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_fng() -> int:
    global _fng_cache
    ts, val = _fng_cache
    if time.time() - ts < 300:
        return val
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        val = int(r.json()["data"][0]["value"])
        _fng_cache = (time.time(), val)
        return val
    except Exception:
        return 50


def _fng_label(v: int) -> str:
    if v <= 25:  return "Extreme Fear"
    if v <= 45:  return "Fear"
    if v <= 55:  return "Neutral"
    if v <= 75:  return "Greed"
    return "Extreme Greed"


def _hurst(prices: list[float]) -> float:
    if len(prices) < 22:
        return 0.5
    try:
        lr = [math.log(prices[i] / prices[i-1]) for i in range(1, len(prices)) if prices[i-1] > 0]
        lags = list(range(2, min(20, len(lr) // 2)))
        if len(lags) < 3:
            return 0.5
        ll, ls = [], []
        for lag in lags:
            diffs = [lr[i+lag] - lr[i] for i in range(len(lr)-lag)]
            var = sum(d*d for d in diffs) / len(diffs)
            if var > 0:
                ll.append(math.log(lag))
                ls.append(math.log(var) / 2)
        n = len(ll)
        if n < 2:
            return 0.5
        mx, my = sum(ll)/n, sum(ls)/n
        num = sum((ll[i]-mx)*(ls[i]-my) for i in range(n))
        den = sum((ll[i]-mx)**2 for i in range(n))
        return round(max(0.0, min(1.0, num/den if den else 0.5)), 4)
    except Exception:
        return 0.5


def _detect_regime(adx_val: float, hurst_val: float) -> tuple[str, float]:
    if adx_val < ADX_RANGING and hurst_val < HURST_RANGING:
        return "RANGING_STRONG", 1.6
    if adx_val < ADX_WEAK or hurst_val < HURST_RANGING:
        return "RANGING", 1.4
    if adx_val < ADX_TRENDING:
        return "WEAK_TREND", 1.2
    return "TRENDING", 1.0


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_tf(candles: CandleData, weight: float) -> tuple[float, dict]:
    closes, highs, lows, volumes = candles.closes, candles.highs, candles.lows, candles.volumes
    price = closes[-1]
    score = 0.0
    info: dict = {}

    es_vals = ema(closes, 9)
    el_vals = ema(closes, 21)
    if es_vals and el_vals:
        es, el = es_vals[-1], el_vals[-1]
        gap = (es - el) / el * 100 if el else 0
        if gap > EMA_GAP_PCT:
            score += 1.0 * weight
            info["ema"] = f"BULL {gap:+.3f}%"
        elif gap < -EMA_GAP_PCT:
            score -= 1.0 * weight
            info["ema"] = f"BEAR {gap:+.3f}%"
        else:
            info["ema"] = f"NEUTRAL {gap:+.3f}%"

    cs_vals = ema(closes, 12)
    cl_vals = ema(closes, 26)
    if cs_vals and cl_vals:
        cs, cl = cs_vals[-1], cl_vals[-1]
        if cs > cl:
            score += 0.5 * weight
            info["cdc"] = "BULL"
        else:
            score -= 0.5 * weight
            info["cdc"] = "BEAR"

    rsi_val = calc_rsi(closes, 14)
    if rsi_val < RSI_OVERSOLD:
        score += 0.8 * weight
        info["rsi"] = f"OVERSOLD {rsi_val:.0f}"
    elif rsi_val > RSI_OVERBOUGHT:
        score -= 0.8 * weight
        info["rsi"] = f"OVERBOUGHT {rsi_val:.0f}"
    else:
        info["rsi"] = f"neutral {rsi_val:.0f}"

    bb = bollinger_bands(closes, 20, 2.0)
    if price < bb.lower:
        score += 0.7 * weight
        info["bb"] = "BELOW_LOWER"
    elif price > bb.upper:
        score -= 0.7 * weight
        info["bb"] = "ABOVE_UPPER"
    else:
        pos = (price - bb.lower) / (bb.upper - bb.lower) * 100 if bb.upper != bb.lower else 50
        info["bb"] = f"mid {pos:.0f}%"

    vwap_val = calc_vwap(highs, lows, closes, volumes, period=min(100, len(closes)))
    if vwap_val > 0:
        gap_v = (price - vwap_val) / vwap_val * 100
        if gap_v > 0.05:
            score += 0.3 * weight
            info["vwap"] = f"ABOVE {gap_v:+.2f}%"
        elif gap_v < -0.05:
            score -= 0.3 * weight
            info["vwap"] = f"BELOW {gap_v:+.2f}%"
        else:
            info["vwap"] = "at VWAP"

    if len(volumes) >= 22:
        avg_vol = sum(volumes[-21:-1]) / 20
        if avg_vol > 0:
            ratio = volumes[-1] / avg_vol
            if ratio >= VOL_SURGE_MULT:
                score *= 1.3
                info["vol"] = f"SURGE x{ratio:.1f}"
            else:
                info["vol"] = f"x{ratio:.1f}"

    info["atr"] = round(calc_atr(highs, lows, closes, 14), 6)
    return score, info


def _calc_levels(entry: float, side: str, atr_val: float, leverage: int) -> tuple[float, float, float]:
    fee = TAKER_FEE * 2
    tp_move   = max(TP_ROE_PCT / 100 / leverage + fee, atr_val / entry * 2.5 if atr_val else 0)
    sl_move   = SL_HARD_ROE_PCT / 100 / leverage
    soft_move = SL_SOFT_ROE_PCT / 100 / leverage
    if side == "LONG":
        return entry*(1+tp_move), entry*(1-soft_move), entry*(1-sl_move)
    return entry*(1-tp_move), entry*(1+soft_move), entry*(1+sl_move)


# ── Main scan ─────────────────────────────────────────────────────────────────

def scan(leverage: int = DEFAULT_LEVERAGE) -> dict:
    fng = _get_fng()
    fng_score = 2.0 if fng <= 25 else (-2.0 if fng >= 75 else 0.0)

    result: dict = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "fng": fng,
        "fng_label": _fng_label(fng),
        "fng_score": fng_score,
        "symbols": {},
    }

    for symbol in SYMBOLS:
        total = 0.0
        tf_details: dict[str, dict] = {}
        candles_15m = None
        atr_5m = 0.0

        for tf, cfg in TF_CONFIG.items():
            candles = binance.get_klines(symbol, tf, cfg["limit"])
            if not candles or len(candles.closes) < 30:
                continue
            sc, info = _score_tf(candles, cfg["weight"])
            total += sc
            tf_details[tf] = {"score": round(sc, 2), **info}
            if tf == "5m":
                atr_5m = info.get("atr", 0.0)
            if tf == "15m":
                candles_15m = candles

        total += fng_score

        hurst_val, adx_val, plus_di, minus_di = 0.5, 20.0, 25.0, 25.0
        if candles_15m:
            hurst_val = _hurst(candles_15m.closes)
            adx_res   = calc_adx(candles_15m.highs, candles_15m.lows, candles_15m.closes, 14)
            adx_val, plus_di, minus_di = adx_res.adx, adx_res.plus_di, adx_res.minus_di

        regime, regime_mult = _detect_regime(adx_val, hurst_val)
        eff_thresh = SCORE_THRESHOLD * regime_mult
        price = candles_15m.closes[-1] if candles_15m else 0.0

        if total >= eff_thresh:
            direction = "LONG"
        elif total <= -eff_thresh:
            direction = "SHORT"
        else:
            direction = "SKIP"

        tp = sl_soft = sl_hard = None
        if direction != "SKIP" and price > 0:
            tp, sl_soft, sl_hard = _calc_levels(price, direction, atr_5m, leverage)

        result["symbols"][symbol] = {
            "price": round(price, 6),
            "score": round(total, 2),
            "eff_thresh": round(eff_thresh, 2),
            "direction": direction,
            "regime": regime,
            "adx": round(adx_val, 1),
            "plus_di": round(plus_di, 1),
            "minus_di": round(minus_di, 1),
            "hurst": hurst_val,
            "atr_5m": atr_5m,
            "tp": round(tp, 6) if tp else None,
            "sl_soft": round(sl_soft, 6) if sl_soft else None,
            "sl_hard": round(sl_hard, 6) if sl_hard else None,
            "tf_details": tf_details,
        }

    return result


# ── Console print ─────────────────────────────────────────────────────────────

def _fmt_price(p: float) -> str:
    if p >= 1000:  return f"{p:,.1f}"
    if p >= 1:     return f"{p:.4f}"
    return f"{p:.6f}"


def print_scan(data: dict, leverage: int = DEFAULT_LEVERAGE) -> None:
    print(f"\n{'─'*62}")
    print(f"  SIGNAL ADVISOR   {data['ts']} UTC")
    print(f"  F&G: {data['fng']}  ({data['fng_label']})   score: {data['fng_score']:+.1f}")
    print(f"{'─'*62}")

    for sym, d in data["symbols"].items():
        icon = "▲ LONG " if d["direction"] == "LONG" else ("▼ SHORT" if d["direction"] == "SHORT" else "— SKIP ")
        tf_line = "  ".join(f"{tf}:{d['tf_details'].get(tf,{}).get('score',0):+.1f}" for tf in ["3m","5m","15m"])
        print(f"\n  {sym:<10} {icon}   {d['score']:+.2f} / need ±{d['eff_thresh']:.2f}")
        print(f"  Price: {_fmt_price(d['price'])}   Regime: {d['regime']}  ADX:{d['adx']:.0f}  +DI:{d['plus_di']:.0f}/-DI:{d['minus_di']:.0f}")
        print(f"  TF │ {tf_line}")
        if d["direction"] != "SKIP" and d["tp"]:
            tp_pct = abs(d["tp"] - d["price"]) / d["price"] * 100
            sl_pct = abs(d["sl_hard"] - d["price"]) / d["price"] * 100
            print(f"  Entry: {_fmt_price(d['price'])}  TP: {_fmt_price(d['tp'])} ({tp_pct:+.2f}%)  SL: {_fmt_price(d['sl_hard'])} (-{sl_pct:.2f}%)")

    print(f"\n{'─'*62}\n")


# ── History ───────────────────────────────────────────────────────────────────

def load_history() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_history(history: list, entry: dict) -> list:
    history.append(entry)
    history = history[-HISTORY_MAX:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)
    return history


def append_csv_log(data: dict) -> None:
    """Append one row per symbol to signal_log.csv (no cap, opens in Excel)."""
    import csv
    headers = [
        "ts", "fng", "fng_label",
        "symbol", "price", "score", "direction", "eff_thresh",
        "regime", "adx", "plus_di", "minus_di", "hurst",
        "score_3m", "score_5m", "score_15m",
        "tp", "sl_hard",
    ]
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), LOG_FILE)
    write_header = not os.path.exists(log_path)
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        if write_header:
            w.writeheader()
        for sym, d in data["symbols"].items():
            w.writerow({
                "ts":          data["ts"],
                "fng":         data["fng"],
                "fng_label":   data["fng_label"],
                "symbol":      sym,
                "price":       d["price"],
                "score":       d["score"],
                "direction":   d["direction"],
                "eff_thresh":  d["eff_thresh"],
                "regime":      d["regime"],
                "adx":         d["adx"],
                "plus_di":     d["plus_di"],
                "minus_di":    d["minus_di"],
                "hurst":       d["hurst"],
                "score_3m":    d["tf_details"].get("3m", {}).get("score", ""),
                "score_5m":    d["tf_details"].get("5m", {}).get("score", ""),
                "score_15m":   d["tf_details"].get("15m", {}).get("score", ""),
                "tp":          d["tp"] or "",
                "sl_hard":     d["sl_hard"] or "",
            })


# ── HTML Report ───────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Signal Advisor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#c9d1d9;--muted:#8b949e;
  --green:#22c55e;--red:#ef4444;--gray:#6b7280;
  --btc:#f97316;--doge:#eab308;--bnb:#f59e0b;--xrp:#3b82f6;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Courier New',monospace;padding:20px;max-width:1200px;margin:0 auto;}
/* Header */
.header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid var(--border);}
h1{font-size:1.3rem;color:#fff;letter-spacing:3px;margin-bottom:4px;}
.ts{color:var(--muted);font-size:0.75rem;}
.fng-badge{padding:5px 14px;border-radius:12px;font-size:0.82rem;font-weight:bold;white-space:nowrap;}
.fng-fear{background:#7f1d1d;color:#fca5a5;}
.fng-greed{background:#14532d;color:#86efac;}
.fng-neutral{background:#1e3a5f;color:#93c5fd;}
.run-btn{background:#1f6feb;color:#fff;border:none;padding:7px 18px;border-radius:8px;font-family:inherit;font-size:0.82rem;cursor:pointer;font-weight:bold;letter-spacing:1px;transition:background 0.15s;}
.run-btn:hover{background:#388bfd;}
.run-btn:disabled{background:#21262d;color:#8b949e;cursor:not-allowed;}
.header-right{display:flex;align-items:center;gap:10px;}
.countdown{font-size:0.72rem;color:var(--muted);min-width:68px;text-align:right;}
/* Cards */
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px;}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px 16px;cursor:pointer;transition:all 0.15s;}
.card:hover{border-color:#58a6ff;}
.card.active{border-color:#58a6ff;background:#1c2333;}
.card.long{border-left:3px solid var(--green);}
.card.short{border-left:3px solid var(--red);}
.card.skip{border-left:3px solid var(--gray);}
.card-sym{font-size:0.8rem;color:var(--muted);margin-bottom:4px;}
.card-price{font-size:1.15rem;color:#fff;font-weight:bold;margin-bottom:6px;}
.card-dir{font-size:0.88rem;font-weight:bold;margin-bottom:6px;}
.card-score{font-size:0.75rem;color:var(--muted);margin-bottom:6px;}
.bar-wrap{background:#21262d;border-radius:4px;height:6px;overflow:hidden;margin-bottom:4px;}
.bar-fill{height:100%;border-radius:4px;transition:width 0.4s ease;}
.bar-label{display:flex;justify-content:space-between;font-size:0.68rem;color:var(--muted);}
.long-txt{color:var(--green);}
.short-txt{color:var(--red);}
.skip-txt{color:var(--gray);}
.adv-long{color:var(--green);font-size:0.72rem;margin-top:6px;font-weight:bold;}
.adv-short{color:var(--red);font-size:0.72rem;margin-top:6px;font-weight:bold;}
.adv-near{color:#eab308;font-size:0.72rem;margin-top:6px;}
.adv-wait{color:var(--muted);font-size:0.72rem;margin-top:6px;}
/* Chart */
.chart-box{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:18px;margin-bottom:20px;}
.section-title{font-size:0.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;}
.chart-wrap{position:relative;height:260px;}
/* Detail */
.detail-box{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:18px;}
.detail-top{display:flex;align-items:center;gap:14px;margin-bottom:14px;flex-wrap:wrap;}
select{background:#21262d;border:1px solid var(--border);color:var(--text);padding:7px 12px;border-radius:6px;font-family:inherit;font-size:0.88rem;cursor:pointer;outline:none;}
select:focus{border-color:#58a6ff;}
.detail-sig{font-size:0.9rem;font-weight:bold;}
.detail-note{font-size:0.78rem;color:var(--muted);margin-left:4px;}
/* Regime row */
.regime-row{display:flex;gap:0;background:#0d1117;border-radius:6px;margin-bottom:14px;overflow:hidden;}
.ritem{flex:1;padding:10px 14px;border-right:1px solid var(--border);}
.ritem:last-child{border-right:none;}
.rlabel{font-size:0.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:3px;}
.rval{font-size:0.88rem;color:#fff;}
/* TF Table */
table{width:100%;border-collapse:collapse;font-size:0.78rem;}
th{color:var(--muted);text-align:left;padding:6px 10px;border-bottom:1px solid var(--border);font-weight:normal;font-size:0.7rem;text-transform:uppercase;}
td{padding:8px 10px;border-bottom:1px solid #21262d;vertical-align:middle;}
tr:last-child td{border-bottom:none;}
.tf-name{color:#58a6ff;font-weight:bold;}
.pos{color:var(--green);font-weight:bold;}
.neg{color:var(--red);font-weight:bold;}
.bull{color:var(--green);}
.bear{color:var(--red);}
.neut{color:var(--muted);}
/* Levels */
.levels{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;background:#0d1117;border-radius:6px;padding:14px;margin-top:14px;}
.litem{display:flex;flex-direction:column;}
.llabel{font-size:0.68rem;color:var(--muted);text-transform:uppercase;margin-bottom:3px;}
.lprice{font-size:0.92rem;font-weight:bold;}
.lpct{font-size:0.72rem;color:var(--muted);margin-top:1px;}
.no-signal{color:var(--muted);text-align:center;padding:24px;font-style:italic;}
@media(max-width:700px){.cards{grid-template-columns:repeat(2,1fr);}.regime-row{flex-wrap:wrap;}.levels{grid-template-columns:repeat(3,1fr);}}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>⚡ SIGNAL ADVISOR</h1>
    <div class="ts" id="ts"></div>
  </div>
  <div class="header-right">
    <span id="countdown" class="countdown"></span>
    <button type="button" class="run-btn" id="runBtn" onclick="runScan()">⟳ รัน</button>
    <div id="fngBadge" class="fng-badge"></div>
  </div>
</div>

<div class="cards" id="cards"></div>

<div class="chart-box">
  <div class="section-title">Score History — ล่าสุด <span id="histLen"></span> รอบ</div>
  <div class="chart-wrap"><canvas id="scoreChart"></canvas></div>
</div>

<div class="detail-box">
  <div class="detail-top">
    <select id="symSel" onchange="renderDetail(this.value)">
      <option>BTCUSDT</option><option>DOGEUSDT</option><option>BNBUSDT</option><option>XRPUSDT</option>
    </select>
    <div>
      <span class="detail-sig" id="detailSig"></span>
      <span class="detail-note" id="detailNote"></span>
    </div>
  </div>
  <div id="detailContent"></div>
</div>

<script>
const LATEST  = __LATEST__;
const HISTORY = __HISTORY__;
const SYMBOLS = ["BTCUSDT","DOGEUSDT","BNBUSDT","XRPUSDT"];
const SYM_COL = {BTCUSDT:"#f97316",DOGEUSDT:"#eab308",BNBUSDT:"#f59e0b",XRPUSDT:"#3b82f6"};

function fmtP(p){
  if(!p) return "—";
  if(p>=1000) return p.toLocaleString("en",{minimumFractionDigits:1,maximumFractionDigits:1});
  if(p>=1)    return p.toFixed(4);
  return p.toFixed(6);
}
function dirCls(d){return d==="LONG"?"long-txt":d==="SHORT"?"short-txt":"skip-txt";}
function dirIcon(d){return d==="LONG"?"▲ LONG":d==="SHORT"?"▼ SHORT":"— SKIP";}
function getAdvice(d){
  const pct = Math.abs(d.score) / d.eff_thresh;
  if(d.direction==="LONG")  return ["adv-long","▲ เตรียมเข้า LONG"];
  if(d.direction==="SHORT") return ["adv-short","▼ เตรียมเข้า SHORT"];
  if(pct>=0.75) return ["adv-near","⚡ ใกล้ threshold — เฝ้าดู"];
  if(pct>=0.40) return ["adv-wait","⏳ รอจังหวะ"];
  return ["adv-wait","— สัญญาณอ่อน"];
}
function colorCell(s){
  if(!s) return '<span class="neut">—</span>';
  s = s.toString();
  if(/^(BULL|OVERSOLD|ABOVE|SURGE|BELOW_LOWER)/.test(s)) return `<span class="bull">${s}</span>`;
  if(/^(BEAR|OVERBOUGHT|BELOW|ABOVE_UPPER)/.test(s))     return `<span class="bear">${s}</span>`;
  return `<span class="neut">${s}</span>`;
}

// Header
document.getElementById("ts").textContent = LATEST.ts + " UTC";
const fb = document.getElementById("fngBadge");
fb.textContent = `F&G: ${LATEST.fng} — ${LATEST.fng_label}`;
fb.className = "fng-badge " + (LATEST.fng<=45?"fng-fear":LATEST.fng>=55?"fng-greed":"fng-neutral");

// Cards
const cardsEl = document.getElementById("cards");
SYMBOLS.forEach(sym=>{
  const d = LATEST.symbols[sym];
  const el = document.createElement("div");
  el.className = `card ${d.direction.toLowerCase()}`;
  el.id = "card-"+sym;
  el.onclick = ()=>{ document.getElementById("symSel").value=sym; renderDetail(sym); };
  const pct   = Math.min(Math.abs(d.score) / d.eff_thresh * 100, 100).toFixed(1);
  const barCol = d.direction==="LONG"?"var(--green)":d.direction==="SHORT"?"var(--red)":"var(--gray)";
  const remaining = Math.max(d.eff_thresh - Math.abs(d.score), 0).toFixed(2);
  const nearLabel = pct >= 100 ? "✓ ผ่าน threshold" : `เหลือ ${remaining}`;
  const [advCls, advTxt] = getAdvice(d);
  el.innerHTML = `
    <div class="card-sym">${sym}</div>
    <div class="card-price">${fmtP(d.price)}</div>
    <div class="card-dir ${dirCls(d.direction)}">${dirIcon(d.direction)}</div>
    <div class="card-score">score ${d.score>0?"+":""}${d.score.toFixed(2)} / ±${d.eff_thresh.toFixed(2)}</div>
    <div class="bar-wrap"><div class="bar-fill" style="width:${pct}%;background:${barCol}"></div></div>
    <div class="bar-label"><span>${pct}%</span><span>${nearLabel}</span></div>
    <div class="${advCls}">${advTxt}</div>`;
  cardsEl.appendChild(el);
});

// Chart
document.getElementById("histLen").textContent = HISTORY.length;
const labels = HISTORY.map(h=>h.ts.slice(5,16).replace("T"," "));
const datasets = SYMBOLS.map(sym=>({
  label: sym,
  data: HISTORY.map(h=>h.symbols&&h.symbols[sym]?h.symbols[sym].score:null),
  borderColor: SYM_COL[sym],
  borderWidth: 2,
  pointRadius: HISTORY.length>30?0:3,
  pointHoverRadius: 5,
  tension: 0.3,
  fill: false,
}));
// Threshold reference
const thr = HISTORY.map(h=>{
  const first = h.symbols&&Object.values(h.symbols)[0];
  return first?first.eff_thresh:3.0;
});
datasets.push({label:"__thr+",data:thr,borderColor:"#ffffff28",borderWidth:1,borderDash:[6,4],pointRadius:0,fill:false});
datasets.push({label:"__thr-",data:thr.map(v=>-v),borderColor:"#ffffff28",borderWidth:1,borderDash:[6,4],pointRadius:0,fill:false});

new Chart(document.getElementById("scoreChart"),{
  type:"line",
  data:{labels,datasets},
  options:{
    responsive:true, maintainAspectRatio:false,
    interaction:{mode:"index",intersect:false},
    plugins:{
      legend:{labels:{color:"#8b949e",filter:i=>!i.text.startsWith("__"),boxWidth:10,padding:14}},
      tooltip:{
        backgroundColor:"#161b22",borderColor:"#30363d",borderWidth:1,
        titleColor:"#c9d1d9",bodyColor:"#8b949e",
        callbacks:{label:ctx=>{
          if(ctx.dataset.label.startsWith("__")) return null;
          const v=ctx.parsed.y;
          return ` ${ctx.dataset.label}: ${v>0?"+":""}${v.toFixed(2)}`;
        }}
      }
    },
    scales:{
      x:{ticks:{color:"#8b949e",maxTicksLimit:10,maxRotation:0},grid:{color:"#21262d"}},
      y:{ticks:{color:"#8b949e"},grid:{color:"#21262d"},suggestedMin:-9,suggestedMax:9}
    }
  }
});

// Detail
function renderDetail(sym){
  SYMBOLS.forEach(s=>{
    const c=document.getElementById("card-"+s);
    if(c) c.classList.toggle("active",s===sym);
  });
  const d = LATEST.symbols[sym];
  if(!d) return;

  document.getElementById("detailSig").innerHTML =
    `<span class="${dirCls(d.direction)}">${dirIcon(d.direction)}</span>`;
  document.getElementById("detailNote").textContent =
    `score ${d.score>0?"+":""}${d.score.toFixed(2)} / need ±${d.eff_thresh.toFixed(2)}`;

  const rc = d.regime==="TRENDING"?"#22c55e":d.regime.includes("RANGING")?"#ef4444":"#eab308";
  let html = `
  <div class="regime-row">
    <div class="ritem"><div class="rlabel">Price</div><div class="rval">${fmtP(d.price)}</div></div>
    <div class="ritem"><div class="rlabel">Regime</div><div class="rval" style="color:${rc}">${d.regime}</div></div>
    <div class="ritem"><div class="rlabel">ADX</div><div class="rval">${d.adx.toFixed(1)}</div></div>
    <div class="ritem"><div class="rlabel">+DI / −DI</div><div class="rval">${d.plus_di.toFixed(0)} / ${d.minus_di.toFixed(0)}</div></div>
    <div class="ritem"><div class="rlabel">Hurst</div><div class="rval">${d.hurst.toFixed(3)}</div></div>
  </div>
  <table>
    <thead><tr><th>TF</th><th>Score</th><th>EMA 9/21</th><th>CDC 12/26</th><th>RSI</th><th>Bollinger</th><th>VWAP</th><th>Volume</th><th>ATR</th></tr></thead>
    <tbody>`;

  ["3m","5m","15m"].forEach(tf=>{
    const t = d.tf_details[tf];
    if(!t) return;
    const sc = t.score;
    html += `<tr>
      <td class="tf-name">${tf}</td>
      <td class="${sc>0?"pos":sc<0?"neg":"neut"}">${sc>0?"+":""}${sc.toFixed(2)}</td>
      <td>${colorCell(t.ema)}</td>
      <td>${colorCell(t.cdc)}</td>
      <td>${colorCell(t.rsi)}</td>
      <td>${colorCell(t.bb)}</td>
      <td>${colorCell(t.vwap)}</td>
      <td>${colorCell(t.vol)}</td>
      <td class="neut">${t.atr||"—"}</td>
    </tr>`;
  });
  html += "</tbody></table>";

  if(d.direction!=="SKIP" && d.tp){
    const tpPct = Math.abs((d.tp-d.price)/d.price*100).toFixed(2);
    const slPct = Math.abs((d.sl_hard-d.price)/d.price*100).toFixed(2);
    html += `
    <div class="levels">
      <div class="litem"><div class="llabel">Entry (ตลาด)</div><div class="lprice" style="color:#fff">${fmtP(d.price)}</div></div>
      <div class="litem"><div class="llabel">Take Profit</div><div class="lprice" style="color:var(--green)">${fmtP(d.tp)}</div><div class="lpct">+${tpPct}%</div></div>
      <div class="litem"><div class="llabel">Stop Loss</div><div class="lprice" style="color:var(--red)">${fmtP(d.sl_hard)}</div><div class="lpct">−${slPct}%</div></div>
    </div>`;
  } else if(d.direction==="SKIP"){
    html += `<div class="no-signal">ยังไม่มี signal — รอ score ผ่าน threshold ±${d.eff_thresh.toFixed(2)}</div>`;
  }

  document.getElementById("detailContent").innerHTML = html;
}

renderDetail("BTCUSDT");

let _running = false;
async function runScan(){
  if(_running) return;
  _running = true;
  _autoSec = 0;
  const btn = document.getElementById("runBtn");
  btn.disabled = true;
  btn.textContent = "กำลังรัน...";
  document.getElementById("countdown").textContent = "กำลังรัน...";
  try {
    const r = await fetch("run_signal.php");
    const j = await r.json();
    if(j.ok){ location.reload(); }
    else { alert("Error: " + j.msg); _running=false; btn.disabled=false; btn.textContent="⟳ รัน"; }
  } catch(e) {
    alert("เชื่อมต่อไม่ได้ — เปิดผ่าน XAMPP (http://localhost/...) ไม่ใช่ file://");
    _running=false; btn.disabled=false; btn.textContent="⟳ รัน";
  }
}

// Auto-refresh ทุก 3 นาที
let _autoSec = 180;
function _tick(){
  if(_running) return;
  if(_autoSec <= 0){ runScan(); return; }
  const m = Math.floor(_autoSec/60);
  const s = _autoSec % 60;
  document.getElementById("countdown").textContent = `auto ${m}:${s.toString().padStart(2,"0")}`;
  _autoSec--;
}
_tick();
setInterval(_tick, 1000);

// ── Log Table ──────────────────────────────────────────────────────────────
(function(){
  if(!HISTORY || HISTORY.length === 0) return;
  const box = document.createElement("div");
  box.style.cssText = "margin-top:20px;";
  box.innerHTML = `
    <div class="chart-box">
      <div class="section-title" style="cursor:pointer;user-select:none" onclick="toggleLog()">
        ▶ Run Log — ${HISTORY.length} รอบที่บันทึกไว้  <span style="font-size:0.68rem;color:#58a6ff">(คลิกเพื่อขยาย)</span>
      </div>
      <div id="logTable" style="display:none;overflow-x:auto;margin-top:12px;">
        <table>
          <thead><tr>
            <th>เวลา</th><th>F&G</th>
            <th>BTC score</th><th>dir</th>
            <th>DOGE score</th><th>dir</th>
            <th>BNB score</th><th>dir</th>
            <th>XRP score</th><th>dir</th>
          </tr></thead>
          <tbody id="logBody"></tbody>
        </table>
      </div>
    </div>`;
  document.body.appendChild(box);

  const syms = ["BTCUSDT","DOGEUSDT","BNBUSDT","XRPUSDT"];
  const tbody = document.getElementById("logBody");
  [...HISTORY].reverse().forEach(h=>{
    const tr = document.createElement("tr");
    let cells = `<td class="neut">${h.ts.slice(5)}</td><td class="neut">${h.fng}</td>`;
    syms.forEach(s=>{
      const d = h.symbols&&h.symbols[s];
      const sc = d?d.score:null;
      const dir = d?d.direction:"—";
      const cls = sc===null?"neut":sc>0?"pos":sc<0?"neg":"neut";
      cells += `<td class="${cls}">${sc!==null?(sc>0?"+":"")+sc.toFixed(2):"—"}</td>`;
      cells += `<td class="${dir==="LONG"?"bull":dir==="SHORT"?"bear":"neut"}">${
        dir==="LONG"?"▲":dir==="SHORT"?"▼":"—"}</td>`;
    });
    tr.innerHTML = cells;
    tbody.appendChild(tr);
  });
})();

function toggleLog(){
  const el = document.getElementById("logTable");
  if(!el) return;
  const open = el.style.display==="none";
  el.style.display = open?"block":"none";
  el.previousElementSibling.textContent =
    (open?"▼":"▶") + ` Run Log — ${HISTORY.length} รอบที่บันทึกไว้  ` +
    (open?"(คลิกเพื่อย่อ)":"(คลิกเพื่อขยาย)");
  el.previousElementSibling.querySelector("span").style.color="#58a6ff";
}
</script>
</body>
</html>"""


def generate_html(data: dict, history: list) -> str:
    html = _HTML
    html = html.replace("__LATEST__",  json.dumps(data,    ensure_ascii=False))
    html = html.replace("__HISTORY__", json.dumps(history, ensure_ascii=False))
    return html


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Signal Advisor")
    parser.add_argument("--report",   action="store_true", help="สร้าง HTML report + เปิด browser")
    parser.add_argument("--watch",    action="store_true", help="refresh console ต่อเนื่อง")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--leverage", type=int, default=DEFAULT_LEVERAGE)
    args = parser.parse_args()

    if args.report:
        print("กำลัง scan...")
        data = scan(leverage=args.leverage)
        history = load_history()
        history = save_history(history, data)
        append_csv_log(data)
        html = generate_html(data, history)
        report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), REPORT_FILE)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Report: {report_path}")
        # คำนวณ relative path จาก htdocs เพื่อเปิดผ่าน localhost
        htdocs = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
        try:
            rel = os.path.relpath(report_path, htdocs).replace(os.sep, "/")
            url = f"http://localhost/{rel}"
        except ValueError:
            url = f"file:///{report_path.replace(os.sep, '/')}"
        webbrowser.open(url)

    elif args.watch:
        print(f"Watch mode — refresh ทุก {args.interval}s  (Ctrl+C หยุด)")
        while True:
            try:
                data = scan(leverage=args.leverage)
                print_scan(data, args.leverage)
                time.sleep(args.interval)
            except KeyboardInterrupt:
                break

    else:
        data = scan(leverage=args.leverage)
        print_scan(data, args.leverage)


if __name__ == "__main__":
    main()
