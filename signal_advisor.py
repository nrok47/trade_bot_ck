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
SETTINGS_FILE  = "advisor_settings.json"
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


# ── Settings (hot-reload ทุก scan) ────────────────────────────────────────────

def _read_settings() -> dict:
    """อ่าน advisor_settings.json ทุกครั้ง — แก้ไฟล์แล้วมีผลทันทีรอบถัดไป."""
    defaults: dict = {
        "symbols":         list(SYMBOLS),
        "score_threshold": SCORE_THRESHOLD,
        "leverage":        DEFAULT_LEVERAGE,
        "tp_roe_pct":      TP_ROE_PCT,
        "sl_hard_roe_pct": SL_HARD_ROE_PCT,
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SETTINGS_FILE)
    if not os.path.exists(path):
        return defaults
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        merged = {**defaults, **saved}
        syms = merged.get("symbols")
        if isinstance(syms, list) and syms:
            merged["symbols"] = [str(s).upper().strip() for s in syms if str(s).strip()]
        else:
            merged["symbols"] = defaults["symbols"]
        return merged
    except Exception:
        return defaults


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


def _calc_levels(
    entry: float, side: str, atr_val: float, leverage: int,
    tp_roe_pct: float = TP_ROE_PCT,
    sl_roe_pct: float = SL_HARD_ROE_PCT,
) -> tuple[float, float, float]:
    fee = TAKER_FEE * 2
    tp_move   = max(tp_roe_pct / 100 / leverage + fee, atr_val / entry * 2.5 if atr_val else 0)
    sl_move   = sl_roe_pct / 100 / leverage
    soft_move = SL_SOFT_ROE_PCT / 100 / leverage
    if side == "LONG":
        return entry*(1+tp_move), entry*(1-soft_move), entry*(1-sl_move)
    return entry*(1-tp_move), entry*(1+soft_move), entry*(1+sl_move)


# ── Main scan ─────────────────────────────────────────────────────────────────

def scan(leverage: int | None = None, settings: dict | None = None) -> dict:
    if settings is None:
        settings = _read_settings()
    symbols      = settings["symbols"]
    lev          = leverage if leverage is not None else int(float(settings["leverage"]))
    score_thresh = float(settings["score_threshold"])
    tp_roe       = float(settings["tp_roe_pct"])
    sl_roe       = float(settings["sl_hard_roe_pct"])

    fng = _get_fng()
    fng_score = 2.0 if fng <= 25 else (-2.0 if fng >= 75 else 0.0)

    result: dict = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "fng": fng,
        "fng_label": _fng_label(fng),
        "fng_score": fng_score,
        "settings": settings,
        "symbols": {},
    }

    for symbol in symbols:
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
        eff_thresh = score_thresh * regime_mult
        price = candles_15m.closes[-1] if candles_15m else 0.0

        if total >= eff_thresh:
            direction = "LONG"
        elif total <= -eff_thresh:
            direction = "SHORT"
        else:
            direction = "SKIP"

        tp = sl_soft = sl_hard = None
        if direction != "SKIP" and price > 0:
            tp, sl_soft, sl_hard = _calc_levels(price, direction, atr_5m, lev, tp_roe, sl_roe)

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


def print_scan(data: dict) -> None:
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{
  --bg:#0d1117;--surface:#161b22;--surface2:#1c2333;
  --border:#30363d;--border2:#21262d;
  --text:#e6edf3;--muted:#8b949e;--muted2:#6e7681;
  --green:#3fb950;--green-dim:#0d2e15;
  --red:#f85149;--red-dim:#2e0d0d;
  --yellow:#d29922;--yellow-dim:#2d2208;
  --blue:#58a6ff;--blue-dim:#0d1f3a;
  --radius:10px;--radius-sm:6px;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Inter','Segoe UI',sans-serif;padding:20px;max-width:1280px;margin:0 auto;font-size:14px;line-height:1.5;}

/* Header */
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid var(--border);flex-wrap:wrap;gap:12px;}
.header h1{font-size:1.15rem;font-weight:700;color:#fff;letter-spacing:2px;margin-bottom:3px;}
.ts{font-size:0.72rem;color:var(--muted);}
.header-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
.countdown{font-size:0.72rem;color:var(--muted);min-width:72px;text-align:right;font-variant-numeric:tabular-nums;}

/* Buttons */
.btn{background:#1f6feb;color:#fff;border:none;padding:7px 16px;border-radius:var(--radius-sm);font-family:inherit;font-size:0.8rem;font-weight:600;cursor:pointer;transition:background 0.15s,transform 0.1s;white-space:nowrap;}
.btn:hover{background:#388bfd;}
.btn:active{transform:scale(0.97);}
.btn:disabled{background:var(--border2);color:var(--muted);cursor:not-allowed;transform:none;}
.btn-sm{padding:5px 10px;font-size:0.76rem;}

/* Badges */
.badge{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:20px;font-size:0.75rem;font-weight:600;white-space:nowrap;}
.bdg-fear{background:#4a0d0d;color:#fca5a5;border:1px solid #7f1d1d;}
.bdg-greed{background:#0d2e15;color:#86efac;border:1px solid #14532d;}
.bdg-neutral{background:#0d1f3a;color:#93c5fd;border:1px solid #1e3a5f;}
.bdg-long{background:var(--green-dim);color:var(--green);border:1px solid #1a4d20;}
.bdg-short{background:var(--red-dim);color:var(--red);border:1px solid #4d1a1a;}
.bdg-skip{background:var(--border2);color:var(--muted);border:1px solid var(--border);}
.bdg-trending{background:var(--green-dim);color:var(--green);border:1px solid #1a4d20;font-size:0.68rem;padding:2px 7px;}
.bdg-weak{background:var(--yellow-dim);color:var(--yellow);border:1px solid #4d3608;font-size:0.68rem;padding:2px 7px;}
.bdg-ranging{background:var(--red-dim);color:var(--red);border:1px solid #4d1a1a;font-size:0.68rem;padding:2px 7px;}

/* Cards */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-bottom:20px;}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px;cursor:pointer;transition:all 0.15s;position:relative;overflow:hidden;}
.card::before{content:'';position:absolute;top:0;left:0;width:3px;height:100%;}
.card.long::before{background:var(--green);}
.card.short::before{background:var(--red);}
.card.skip::before{background:var(--muted2);}
.card:hover{border-color:var(--blue);background:var(--surface2);}
.card.active{border-color:var(--blue);background:var(--surface2);box-shadow:0 0 0 1px #1f6feb30;}
.card-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;}
.card-sym{font-size:0.7rem;color:var(--muted);font-weight:500;letter-spacing:0.5px;margin-bottom:3px;}
.card-price{font-size:1.2rem;color:#fff;font-weight:700;font-variant-numeric:tabular-nums;}
.card-score-row{display:flex;justify-content:space-between;font-size:0.75rem;color:var(--muted);margin-bottom:5px;}
.bar-wrap{background:var(--border2);border-radius:3px;height:5px;overflow:hidden;margin-bottom:8px;}
.bar-fill{height:100%;border-radius:3px;transition:width 0.5s ease;}
.tf-dots{display:flex;gap:6px;margin-bottom:10px;}
.tf-dot{display:flex;flex-direction:column;align-items:center;gap:3px;}
.tf-dot-c{width:8px;height:8px;border-radius:50%;}
.tf-dot-l{font-size:0.58rem;color:var(--muted2);}
.card-footer{display:flex;align-items:center;justify-content:space-between;}
.card-advice{font-size:0.72rem;font-weight:600;margin-top:0;}
.adv-long{color:var(--green);}
.adv-short{color:var(--red);}
.adv-near{color:var(--yellow);}
.adv-wait{color:var(--muted);}
.divider{height:1px;background:var(--border2);margin:10px 0;}

/* Box */
.box{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:18px;margin-bottom:16px;}
.box-title{font-size:0.7rem;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;display:flex;align-items:center;gap:8px;}
.chart-wrap{position:relative;height:280px;}

/* Detail */
.detail-hdr{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap;}
select{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:7px 12px;border-radius:var(--radius-sm);font-family:inherit;font-size:0.85rem;cursor:pointer;outline:none;font-weight:600;}
select:focus{border-color:var(--blue);}

/* Regime strip */
.regime-strip{display:grid;grid-template-columns:repeat(auto-fill,minmax(115px,1fr));gap:1px;background:var(--border2);border-radius:var(--radius-sm);overflow:hidden;margin-bottom:16px;}
.ritem{background:#0d1117;padding:10px 14px;}
.rlabel{font-size:0.63rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.8px;margin-bottom:4px;}
.rval{font-size:0.86rem;color:var(--text);font-weight:500;}

/* TF table */
.tf-wrap{overflow-x:auto;margin-bottom:14px;}
table{width:100%;border-collapse:collapse;font-size:0.78rem;min-width:560px;}
th{color:var(--muted);text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);font-weight:500;font-size:0.65rem;text-transform:uppercase;letter-spacing:0.5px;white-space:nowrap;}
td{padding:9px 10px;border-bottom:1px solid var(--border2);vertical-align:middle;white-space:nowrap;}
tr:last-child td{border-bottom:none;}
tr:hover td{background:var(--surface2);}
.tf-name{color:var(--blue);font-weight:700;font-size:0.82rem;}
.pos{color:var(--green);font-weight:700;}
.neg{color:var(--red);font-weight:700;}
.bull{color:var(--green);}
.bear{color:var(--red);}
.neut{color:var(--muted);}
.sc-cell{display:flex;align-items:center;gap:6px;}
.sc-bar{width:36px;height:4px;background:var(--border2);border-radius:2px;overflow:hidden;flex-shrink:0;}
.sc-bar-f{height:100%;border-radius:2px;}

/* Levels */
.levels{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-top:14px;}
.litem{background:#0d1117;border-radius:var(--radius-sm);padding:12px 14px;border-left:3px solid transparent;}
.litem.tp{border-left-color:var(--green);}
.litem.sl-s{border-left-color:var(--yellow);}
.litem.sl-h{border-left-color:var(--red);}
.litem.entry{border-left-color:var(--blue);}
.llabel{font-size:0.63rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:5px;}
.lprice{font-size:0.95rem;font-weight:700;font-variant-numeric:tabular-nums;}
.lpct{font-size:0.7rem;color:var(--muted);margin-top:2px;}
.lrr{font-size:0.65rem;color:var(--muted2);margin-top:2px;}
.no-sig{text-align:center;padding:32px;color:var(--muted);font-size:0.85rem;}
.no-sig-sub{display:inline-block;margin-top:8px;background:var(--surface2);padding:4px 14px;border-radius:20px;font-size:0.73rem;color:var(--muted2);}

/* Collapsible */
.ctitle{cursor:pointer;user-select:none;display:flex;align-items:center;gap:8px;}
.caret{font-size:0.6rem;color:var(--muted);transition:transform 0.2s;display:inline-block;}
.ctitle.open .caret{transform:rotate(90deg);}
.chint{font-size:0.65rem;color:var(--blue);margin-left:auto;}

/* Settings */
.chip{display:inline-flex;align-items:center;gap:5px;background:var(--surface2);border:1px solid var(--border);border-radius:16px;padding:4px 10px;font-size:0.78rem;font-weight:500;}
.chip-x{cursor:pointer;color:var(--muted2);font-size:0.8rem;line-height:1;}
.chip-x:hover{color:var(--red);}
.cfg-in{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:6px 9px;border-radius:var(--radius-sm);font-family:inherit;font-size:0.84rem;outline:none;width:90px;}
.cfg-in:focus{border-color:var(--blue);}
.cfg-in-w{width:155px;}

@media(max-width:700px){
  .cards{grid-template-columns:repeat(2,1fr);}
  .regime-strip{grid-template-columns:repeat(2,1fr);}
  .levels{grid-template-columns:repeat(2,1fr);}
  body{padding:12px;}
}
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div>
    <h1>⚡ SIGNAL ADVISOR</h1>
    <div class="ts" id="ts"></div>
  </div>
  <div class="header-right">
    <span id="fngBadge" class="badge"></span>
    <span id="countdown" class="countdown"></span>
    <button type="button" class="btn" id="runBtn" onclick="runScan()">⟳ Scan</button>
  </div>
</div>

<!-- Symbol Cards -->
<div class="cards" id="cards"></div>

<!-- Score History -->
<div class="box">
  <div class="box-title">
    📈 Score History
    <span style="font-weight:400;color:var(--muted);">ล่าสุด <span id="histLen"></span> รอบ</span>
  </div>
  <div class="chart-wrap"><canvas id="scoreChart"></canvas></div>
</div>

<!-- Detail -->
<div class="box">
  <div class="detail-hdr">
    <span style="font-size:0.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:1px;">รายละเอียด</span>
    <select id="symSel" onchange="renderDetail(this.value)"></select>
    <span id="detailBadge"></span>
    <span id="detailNote" style="font-size:0.75rem;color:var(--muted);"></span>
  </div>
  <div id="detailContent"></div>
</div>

<script>
const LATEST  = __LATEST__;
const HISTORY = __HISTORY__;
const SYMBOLS = Object.keys(LATEST.symbols);
const _COL_MAP = {BTCUSDT:"#f97316",DOGEUSDT:"#eab308",BNBUSDT:"#f59e0b",XRPUSDT:"#3b82f6"};
const _FALL    = ["#a78bfa","#34d399","#f472b6","#60a5fa","#fb923c","#4ade80","#c084fc","#38bdf8"];
const SYM_COL  = Object.fromEntries(SYMBOLS.map((s,i)=>[s,_COL_MAP[s]||_FALL[i%_FALL.length]]));

(function(){
  const sel=document.getElementById("symSel");
  SYMBOLS.forEach(s=>{const o=document.createElement("option");o.value=o.textContent=s;sel.appendChild(o);});
})();

function fmtP(p){
  if(!p&&p!==0) return "—";
  if(p>=1000) return p.toLocaleString("en",{minimumFractionDigits:1,maximumFractionDigits:1});
  if(p>=1)    return p.toFixed(4);
  return p.toFixed(6);
}
function dirBadge(d){
  const m={LONG:["bdg-long","▲ LONG"],SHORT:["bdg-short","▼ SHORT"],SKIP:["bdg-skip","— WAIT"]};
  const [c,t]=m[d]||m.SKIP;
  return `<span class="badge ${c}">${t}</span>`;
}
function regimeBadge(r){
  const c=r==="TRENDING"?"bdg-trending":r.includes("RANGING")?"bdg-ranging":"bdg-weak";
  return `<span class="badge ${c}">${r.replace("_"," ")}</span>`;
}
function getAdvice(d){
  const p=Math.abs(d.score)/d.eff_thresh;
  if(d.direction==="LONG")  return["adv-long","▲ เข้า LONG ได้"];
  if(d.direction==="SHORT") return["adv-short","▼ เข้า SHORT ได้"];
  if(p>=0.75) return["adv-near","⚡ ใกล้ threshold"];
  if(p>=0.40) return["adv-wait","⏳ รอจังหวะ"];
  return["adv-wait","— สัญญาณอ่อน"];
}
function colorCell(s){
  if(!s) return'<span class="neut">—</span>';
  s=s.toString();
  if(/^(BULL|OVERSOLD|ABOVE[^_]|SURGE|BELOW_LOWER)/.test(s)) return`<span class="bull">${s}</span>`;
  if(/^(BEAR|OVERBOUGHT|BELOW[^_]|ABOVE_UPPER)/.test(s))     return`<span class="bear">${s}</span>`;
  return`<span class="neut">${s}</span>`;
}
function tfDotColor(sc,eff){
  if(!sc) return"#6e7681";
  const r=sc/(eff||1);
  if(r>=0.5)  return"#3fb950";
  if(r>=0.2)  return"#d29922";
  if(r<=-0.5) return"#f85149";
  if(r<=-0.2) return"#e06030";
  return"#6e7681";
}

// Header
document.getElementById("ts").textContent = "อัพเดต: "+LATEST.ts+" UTC";
const fb=document.getElementById("fngBadge");
const fv=LATEST.fng;
fb.textContent=`F&G ${fv} — ${LATEST.fng_label}`;
fb.className="badge "+(fv<45?"bdg-fear":fv>55?"bdg-greed":"bdg-neutral");

// Cards
const cardsEl=document.getElementById("cards");
SYMBOLS.forEach(sym=>{
  const d=LATEST.symbols[sym];
  const el=document.createElement("div");
  el.className=`card ${d.direction.toLowerCase()}`;
  el.id="card-"+sym;
  el.onclick=()=>{document.getElementById("symSel").value=sym;renderDetail(sym);};

  const pct=Math.min(Math.abs(d.score)/d.eff_thresh*100,100);
  const barC=d.direction==="LONG"?"var(--green)":d.direction==="SHORT"?"var(--red)":"var(--muted2)";
  const left=Math.max(d.eff_thresh-Math.abs(d.score),0).toFixed(2);
  const nearLbl=pct>=100?"✓ ผ่านแล้ว":`เหลือ ${left}`;
  const[advC,advT]=getAdvice(d);
  const tfs=["3m","5m","15m"];
  const dots=tfs.map(tf=>{
    const sc=d.tf_details&&d.tf_details[tf]?d.tf_details[tf].score:0;
    return`<div class="tf-dot"><div class="tf-dot-c" style="background:${tfDotColor(sc,d.eff_thresh/3)};"></div><div class="tf-dot-l">${tf}</div></div>`;
  }).join("");
  const sym_s=sym.replace("USDT","");

  el.innerHTML=`
    <div class="card-top">
      <div>
        <div class="card-sym">${sym_s}<span style="color:var(--muted2);font-size:0.62rem;">/USDT</span></div>
        <div class="card-price">${fmtP(d.price)}</div>
      </div>
      ${dirBadge(d.direction)}
    </div>
    <div class="card-score-row">
      <span style="font-weight:600;">${d.score>0?"+":""}${d.score.toFixed(2)}</span>
      <span style="color:var(--muted2);">/ ±${d.eff_thresh.toFixed(1)}</span>
    </div>
    <div class="bar-wrap"><div class="bar-fill" style="width:${pct.toFixed(1)}%;background:${barC}"></div></div>
    <div style="display:flex;justify-content:space-between;font-size:0.65rem;color:var(--muted2);margin-bottom:10px;">
      <span>${pct.toFixed(0)}%</span><span>${nearLbl}</span>
    </div>
    <div class="card-footer">
      <div class="tf-dots">${dots}</div>
      ${regimeBadge(d.regime)}
    </div>
    <div class="divider"></div>
    <div class="card-advice ${advC}">${advT}</div>`;
  cardsEl.appendChild(el);
});

// Chart
document.getElementById("histLen").textContent=HISTORY.length;
const labels=HISTORY.map(h=>h.ts.slice(5,16));
const datasets=SYMBOLS.map(sym=>({
  label:sym.replace("USDT",""),
  data:HISTORY.map(h=>h.symbols&&h.symbols[sym]?h.symbols[sym].score:null),
  borderColor:SYM_COL[sym],
  backgroundColor:SYM_COL[sym]+"18",
  borderWidth:2,
  pointRadius:HISTORY.length>40?0:3,
  pointHoverRadius:5,
  tension:0.3,fill:false,
}));
const thr=HISTORY.map(h=>{const f=h.symbols&&Object.values(h.symbols)[0];return f?f.eff_thresh:3.0;});
datasets.push({label:"thr+",data:thr,borderColor:"#ffffff20",borderWidth:1,borderDash:[5,5],pointRadius:0,fill:false});
datasets.push({label:"thr-",data:thr.map(v=>-v),borderColor:"#ffffff20",borderWidth:1,borderDash:[5,5],pointRadius:0,fill:false});

new Chart(document.getElementById("scoreChart"),{
  type:"line",data:{labels,datasets},
  options:{
    responsive:true,maintainAspectRatio:false,
    interaction:{mode:"index",intersect:false},
    plugins:{
      legend:{labels:{color:"#8b949e",filter:i=>!i.text.startsWith("thr"),boxWidth:10,padding:14,font:{size:11}}},
      tooltip:{backgroundColor:"#1c2333",borderColor:"#30363d",borderWidth:1,
        titleColor:"#e6edf3",bodyColor:"#8b949e",padding:10,
        callbacks:{
          title:i=>i[0].label,
          label:ctx=>{
            if(ctx.dataset.label.startsWith("thr")) return null;
            const v=ctx.parsed.y;return` ${ctx.dataset.label}: ${v>0?"+":""}${v.toFixed(2)}`;
          }
        }
      }
    },
    scales:{
      x:{ticks:{color:"#8b949e",maxTicksLimit:8,maxRotation:0,font:{size:10}},grid:{color:"#21262d"}},
      y:{ticks:{color:"#8b949e",font:{size:10}},grid:{color:"#21262d"},suggestedMin:-10,suggestedMax:10}
    }
  }
});

// Detail
function renderDetail(sym){
  SYMBOLS.forEach(s=>{const c=document.getElementById("card-"+s);if(c)c.classList.toggle("active",s===sym);});
  const d=LATEST.symbols[sym]; if(!d) return;
  document.getElementById("detailBadge").innerHTML=dirBadge(d.direction);
  document.getElementById("detailNote").textContent=`score ${d.score>0?"+":""}${d.score.toFixed(2)} / need ±${d.eff_thresh.toFixed(2)}`;
  document.getElementById("symSel").value=sym;

  const rc=d.regime==="TRENDING"?"var(--green)":d.regime.includes("RANGING")?"var(--red)":"var(--yellow)";
  const hurstNote=d.hurst>0.55?"trending":d.hurst<0.45?"mean-rev":"random";
  let html=`
  <div class="regime-strip">
    <div class="ritem"><div class="rlabel">Symbol</div><div class="rval" style="font-weight:700">${sym}</div></div>
    <div class="ritem"><div class="rlabel">Price</div><div class="rval">${fmtP(d.price)}</div></div>
    <div class="ritem"><div class="rlabel">Regime</div><div class="rval" style="color:${rc}">${d.regime.replace("_"," ")}</div></div>
    <div class="ritem"><div class="rlabel">ADX</div><div class="rval">${d.adx.toFixed(1)} <span style="font-size:0.7rem;color:var(--muted)">(+${d.plus_di.toFixed(0)}/-${d.minus_di.toFixed(0)})</span></div></div>
    <div class="ritem"><div class="rlabel">Hurst</div><div class="rval">${d.hurst.toFixed(3)} <span style="font-size:0.68rem;color:var(--muted)">${hurstNote}</span></div></div>
    <div class="ritem"><div class="rlabel">ATR 5m</div><div class="rval">${d.atr_5m?d.atr_5m.toFixed(5):"—"}</div></div>
  </div>
  <div class="tf-wrap">
  <table>
    <thead><tr><th>TF</th><th>Score</th><th>EMA 9/21</th><th>CDC 12/26</th><th>RSI</th><th>Bollinger</th><th>VWAP</th><th>Volume</th><th>ATR</th></tr></thead>
    <tbody>`;

  const maxSc=Math.max(...["3m","5m","15m"].map(tf=>d.tf_details[tf]?Math.abs(d.tf_details[tf].score):0),0.01);
  ["3m","5m","15m"].forEach(tf=>{
    const t=d.tf_details[tf]; if(!t) return;
    const sc=t.score;
    const bw=Math.min(Math.abs(sc)/maxSc*100,100).toFixed(0);
    const bc=sc>0?"var(--green)":sc<0?"var(--red)":"var(--muted2)";
    html+=`<tr>
      <td class="tf-name">${tf}</td>
      <td><div class="sc-cell"><span class="${sc>0?"pos":sc<0?"neg":"neut"}">${sc>0?"+":""}${sc.toFixed(2)}</span><div class="sc-bar"><div class="sc-bar-f" style="width:${bw}%;background:${bc}"></div></div></div></td>
      <td>${colorCell(t.ema)}</td><td>${colorCell(t.cdc)}</td><td>${colorCell(t.rsi)}</td>
      <td>${colorCell(t.bb)}</td><td>${colorCell(t.vwap)}</td><td>${colorCell(t.vol)}</td>
      <td class="neut">${t.atr||"—"}</td>
    </tr>`;
  });
  html+="</tbody></table></div>";

  if(d.direction!=="SKIP"&&d.tp){
    const tpPct=(d.tp-d.price)/d.price*100;
    const slPct=(d.sl_hard-d.price)/d.price*100;
    const ssPct=d.sl_soft!=null?(d.sl_soft-d.price)/d.price*100:null;
    const rr=Math.abs(tpPct/slPct).toFixed(2);
    const sgn=v=>v>=0?"+":"";
    html+=`<div class="levels">
      <div class="litem entry">
        <div class="llabel">Entry</div>
        <div class="lprice" style="color:var(--blue)">${fmtP(d.price)}</div>
        <div class="lpct">ราคาตลาด</div>
      </div>
      <div class="litem tp">
        <div class="llabel">Take Profit</div>
        <div class="lprice" style="color:var(--green)">${fmtP(d.tp)}</div>
        <div class="lpct">${sgn(tpPct)}${Math.abs(tpPct).toFixed(2)}%</div>
        <div class="lrr">RR ${rr}:1</div>
      </div>
      ${ssPct!=null?`<div class="litem sl-s">
        <div class="llabel">Soft SL</div>
        <div class="lprice" style="color:var(--yellow)">${fmtP(d.sl_soft)}</div>
        <div class="lpct">${sgn(ssPct)}${Math.abs(ssPct).toFixed(2)}%</div>
        <div class="lrr">re-check trend</div>
      </div>`:""}
      <div class="litem sl-h">
        <div class="llabel">Hard SL</div>
        <div class="lprice" style="color:var(--red)">${fmtP(d.sl_hard)}</div>
        <div class="lpct">${sgn(slPct)}${Math.abs(slPct).toFixed(2)}%</div>
        <div class="lrr">ตัดขาดทุน</div>
      </div>
    </div>`;
  } else if(d.direction==="SKIP"){
    const p=(Math.abs(d.score)/d.eff_thresh*100).toFixed(0);
    html+=`<div class="no-sig">ยังไม่มี signal<br><span class="no-sig-sub">score ${d.score>0?"+":""}${d.score.toFixed(2)} / ±${d.eff_thresh.toFixed(2)} — ${p}% ของ threshold</span></div>`;
  }
  document.getElementById("detailContent").innerHTML=html;
}
renderDetail(SYMBOLS[0]);

// Run & Auto-refresh
let _running=false;
async function runScan(){
  if(_running) return;
  _running=true; _autoSec=0;
  const btn=document.getElementById("runBtn");
  btn.disabled=true; btn.textContent="⏳ Scanning...";
  document.getElementById("countdown").textContent="กำลังรัน...";
  try{
    const r=await fetch("run_signal.php");
    const j=await r.json();
    if(j.ok){location.reload();}
    else{alert("Error: "+j.msg);_running=false;btn.disabled=false;btn.textContent="↻ Scan";}
  }catch(e){
    alert("เชื่อมต่อไม่ได้ — เปิดผ่าน XAMPP (http://localhost/...)");
    _running=false;btn.disabled=false;btn.textContent="↻ Scan";
  }
}
let _autoSec=180;
function _tick(){
  if(_running) return;
  if(_autoSec<=0){runScan();return;}
  const m=Math.floor(_autoSec/60),s=_autoSec%60;
  document.getElementById("countdown").textContent=`auto ${m}:${s.toString().padStart(2,"0")}`;
  _autoSec--;
}
_tick(); setInterval(_tick,1000);

// Collapsible helper
window.toggleSection=function(titleEl,contentId){
  const c=document.getElementById(contentId);
  const open=c.style.display==="none";
  c.style.display=open?"block":"none";
  titleEl.classList.toggle("open",open);
  titleEl.querySelector(".chint").textContent=open?"คลิกเพื่อย่อ":"คลิกเพื่อขยาย";
};

// Run Log
(function(){
  if(!HISTORY||HISTORY.length===0) return;
  const box=document.createElement("div");
  box.innerHTML=`
    <div class="box">
      <div class="box-title ctitle" onclick="toggleSection(this,'logContent')">
        <span class="caret">▶</span>
        📋 Run Log
        <span style="font-size:0.7rem;color:var(--muted);font-weight:400;">${HISTORY.length} รอบ</span>
        <span class="chint">คลิกเพื่อขยาย</span>
      </div>
      <div id="logContent" style="display:none;">
        <div style="overflow-x:auto;margin-top:8px;">
          <table><thead><tr id="logHead"></tr></thead><tbody id="logBody"></tbody></table>
        </div>
      </div>
    </div>`;
  document.body.appendChild(box);

  const head=document.getElementById("logHead");
  let hc=`<th>เวลา</th><th>F&G</th>`;
  SYMBOLS.forEach(s=>{hc+=`<th>${s.replace("USDT","")}</th><th>dir</th>`;});
  head.innerHTML=hc;

  const tbody=document.getElementById("logBody");
  [...HISTORY].reverse().forEach(h=>{
    const tr=document.createElement("tr");
    let cells=`<td class="neut">${h.ts.slice(5)}</td><td class="neut">${h.fng}</td>`;
    SYMBOLS.forEach(s=>{
      const d=h.symbols&&h.symbols[s];
      const sc=d?d.score:null,dir=d?d.direction:"—";
      const cls=sc===null?"neut":sc>0?"pos":sc<0?"neg":"neut";
      cells+=`<td class="${cls}">${sc!==null?(sc>0?"+":"")+sc.toFixed(2):"—"}</td>`;
      cells+=`<td class="${dir==="LONG"?"bull":dir==="SHORT"?"bear":"neut"}">${dir==="LONG"?"▲":dir==="SHORT"?"▼":"—"}</td>`;
    });
    tr.innerHTML=cells; tbody.appendChild(tr);
  });
})();

// Settings
(function(){
  const cfg=(LATEST.settings||{});
  let _syms=SYMBOLS.slice();
  const box=document.createElement("div");
  box.innerHTML=`
    <div class="box">
      <div class="box-title ctitle" onclick="toggleSection(this,'settingsContent')">
        <span class="caret">▶</span>
        ⚙ ตั้งค่า
        <span style="font-size:0.7rem;color:var(--muted);font-weight:400;">เหรียญ & พารามิเตอร์</span>
        <span class="chint">คลิกเพื่อขยาย</span>
      </div>
      <div id="settingsContent" style="display:none;margin-top:14px;">
        <div style="margin-bottom:16px;">
          <div class="rlabel" style="margin-bottom:8px;">เหรียญที่ scan</div>
          <div id="symChips" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;"></div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
            <input id="newSymInput" type="text" placeholder="เช่น ETHUSDT, SOLUSDT" class="cfg-in cfg-in-w" onkeydown="if(event.key==='Enter')_addSym()">
            <button onclick="_addSym()" class="btn btn-sm">+ เพิ่มเหรียญ</button>
          </div>
        </div>
        <div style="height:1px;background:var(--border2);margin-bottom:16px;"></div>
        <div style="margin-bottom:16px;">
          <div class="rlabel" style="margin-bottom:10px;">พารามิเตอร์</div>
          <div style="display:flex;flex-wrap:wrap;gap:16px;">
            <div><div class="rlabel" style="margin-bottom:5px;">Score Threshold</div><input id="cfgThreshold" type="number" step="0.5" min="1" max="15" class="cfg-in"></div>
            <div><div class="rlabel" style="margin-bottom:5px;">Leverage (x)</div><input id="cfgLeverage" type="number" step="1" min="1" max="50" class="cfg-in"></div>
            <div><div class="rlabel" style="margin-bottom:5px;">TP ROE %</div><input id="cfgTp" type="number" step="5" min="5" max="300" class="cfg-in"></div>
            <div><div class="rlabel" style="margin-bottom:5px;">Hard SL ROE %</div><input id="cfgSl" type="number" step="5" min="3" max="100" class="cfg-in"></div>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
          <button onclick="_saveSettings()" class="btn">💾 บันทึกและ Scan ใหม่</button>
          <span id="saveMsg" style="font-size:0.75rem;color:var(--muted);"></span>
        </div>
      </div>
    </div>`;
  document.body.appendChild(box);

  function _chips(){
    const el=document.getElementById("symChips");if(!el)return;
    el.innerHTML="";
    _syms.forEach(s=>{
      const c=document.createElement("span");c.className="chip";
      c.innerHTML=`${s}<span class="chip-x" onclick="_removeSym('${s}')">✕</span>`;
      el.appendChild(c);
    });
    document.getElementById("cfgThreshold").value=cfg.score_threshold||3;
    document.getElementById("cfgLeverage").value=cfg.leverage||8;
    document.getElementById("cfgTp").value=cfg.tp_roe_pct||30;
    document.getElementById("cfgSl").value=cfg.sl_hard_roe_pct||15;
  }
  const _origToggle=window.toggleSection;
  window.toggleSection=function(el,id){
    _origToggle(el,id);
    if(id==="settingsContent"&&document.getElementById(id).style.display==="block") _chips();
  };
  window._addSym=function(){
    const inp=document.getElementById("newSymInput");
    const sym=inp.value.trim().toUpperCase();inp.value="";
    if(!sym) return;
    if(_syms.includes(sym)){document.getElementById("saveMsg").textContent="มี "+sym+" อยู่แล้ว";return;}
    if(!/^[A-Z0-9]{3,20}$/.test(sym)){document.getElementById("saveMsg").textContent="ชื่อไม่ถูกต้อง เช่น ETHUSDT";return;}
    _syms.push(sym);document.getElementById("saveMsg").textContent="";_chips();
  };
  window._removeSym=function(sym){
    if(_syms.length<=1){document.getElementById("saveMsg").textContent="ต้องมีอย่างน้อย 1 เหรียญ";return;}
    _syms=_syms.filter(s=>s!==sym);_chips();
  };
  window._saveSettings=async function(){
    const p={
      symbols:_syms,
      score_threshold:parseFloat(document.getElementById("cfgThreshold").value)||3,
      leverage:parseInt(document.getElementById("cfgLeverage").value)||8,
      tp_roe_pct:parseFloat(document.getElementById("cfgTp").value)||30,
      sl_hard_roe_pct:parseFloat(document.getElementById("cfgSl").value)||15,
    };
    document.getElementById("saveMsg").textContent="กำลังบันทึก...";
    try{
      const r=await fetch("save_advisor_settings.php",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(p)});
      const j=await r.json();
      if(j.ok){document.getElementById("saveMsg").textContent="✓ บันทึกแล้ว — กำลัง Scan ใหม่...";setTimeout(()=>runScan(),600);}
      else{document.getElementById("saveMsg").textContent="Error: "+(j.msg||"unknown");}
    }catch(e){document.getElementById("saveMsg").textContent="เชื่อมต่อไม่ได้ — เปิดผ่าน XAMPP";}
  };
})();
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
    parser.add_argument("--report",    action="store_true", help="สร้าง HTML report + เปิด browser")
    parser.add_argument("--watch",     action="store_true", help="refresh console ต่อเนื่อง")
    parser.add_argument("--interval",  type=int,   default=60)
    parser.add_argument("--leverage",  type=int,   default=None,  help="override leverage เช่น 10")
    parser.add_argument("--symbols",   type=str,   default="",    help="เหรียญที่ต้องการ scan เช่น BTCUSDT,ETHUSDT,SOLUSDT")
    parser.add_argument("--threshold", type=float, default=None,  help="override score threshold เช่น 4.0")
    args = parser.parse_args()

    # โหลด settings แล้ว override ด้วย CLI ถ้ามี
    settings = _read_settings()
    if args.symbols:
        settings["symbols"] = [s.upper().strip() for s in args.symbols.split(",") if s.strip()]
    if args.leverage is not None:
        settings["leverage"] = args.leverage
    if args.threshold is not None:
        settings["score_threshold"] = args.threshold

    if args.report:
        syms_str = ",".join(settings["symbols"])
        print(f"กำลัง scan {syms_str} ...")
        data = scan(settings=settings)
        history = load_history()
        history = save_history(history, data)
        append_csv_log(data)
        html = generate_html(data, history)
        report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), REPORT_FILE)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Report: {report_path}")
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
                data = scan(settings=settings)
                print_scan(data)
                time.sleep(args.interval)
            except KeyboardInterrupt:
                break

    else:
        data = scan(settings=settings)
        print_scan(data)


if __name__ == "__main__":
    main()
