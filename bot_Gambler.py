"""
Gambler Bot — Directional swing trader (LONG / SHORT)
=====================================================
Strategy: Multi-TF confluence scoring → binary entry → hold until TP or SL

Signals used (3m + 5m + 15m, last 12h):
  • EMA 9/21  — trend direction per TF
  • RSI 14    — oversold / overbought
  • Bollinger — band touch / breakout
  • Volume    — surge amplifier (×1.3 when current vol > 2× avg)
  • ATR 14    — sets minimum TP distance

Money Management:
  Capital   = 30% of USDT balance
  Leverage  = 8×
  Entry     = Market Order (Taker)
  TP        = 30% ROE  (price move = 30%/8 + round-trip fee = ~3.83%)
  SL soft   = 10% ROE  → re-check trend → close if invalidated
  SL hard   = 30% ROE  → force close always

Cooldown: 5 min after any close before re-entering.

Usage:
  python bot_Gambler.py               # DRY_RUN follows .env
  python bot_Gambler.py --dry-run     # force dry run
  python bot_Gambler.py --symbol DOGEUSDT
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
import requests
from dataclasses import dataclass, asdict
from typing import Optional

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from binance.client import Client
from binance.exceptions import BinanceAPIException

from binance_client import binance
from config import config
from indicators import (
    CandleData, ema, rsi as calc_rsi, atr as calc_atr, bollinger_bands,
    adx as calc_adx, vwap as calc_vwap,
)

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE = "gambler_bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("gambler")

# ╔══════════════════════════════════════════════════════════════════════╗
# ║  CONFIGURATION — แก้ตรงนี้เพื่อปรับค่าเริ่มต้นของบอท               ║
# ║  (ปรับขณะรันได้ผ่าน Dashboard → บันทึกลง gambler_settings.json)    ║
# ╚══════════════════════════════════════════════════════════════════════╝

# ── Money Management ──────────────────────────────────────────────────────────
LEVERAGE         = 8       # leverage (1–20); เปลี่ยนต้องรีสตาร์ทบอท
CAPITAL_PCT      = 0.30    # สัดส่วน balance ต่อไม้  0.30 = 30%
TAKER_FEE        = 0.0004  # futures taker fee ต่อขา (0.04%)
TP_ROE_PCT       = 30.0    # เป้ากำไร ROE% ต่อไม้
SL_SOFT_ROE_PCT  = 10.0    # Soft SL: re-check trend แล้วค่อยตัดสิน
SL_HARD_ROE_PCT  = 15.0    # Hard SL: ปิดทันทีไม่มีข้อแม้ (RR 2:1 vs TP 30%)

# ── Strategy Tuning ───────────────────────────────────────────────────────────
SCORE_THRESHOLD  = 2.5     # คะแนนขั้นต่ำก่อนเปิดไม้ (max ~15)  ลดจาก 3.5 → 2.5 เพื่อเข้าบ่อยขึ้น ~30%
EMA_GAP_PCT      = 0.03    # % ช่องว่าง EMA9/21 ถึงนับเป็น trend
RSI_OVERSOLD     = 40      # RSI ต่ำกว่านี้ → bullish signal
RSI_OVERBOUGHT   = 60      # RSI สูงกว่านี้ → bearish signal
VOL_SURGE_MULT   = 2.0     # volume ต้องสูงกว่า avg × ค่านี้ ถึงนับ surge

# ── Fear & Greed Index (Trinity layer) ───────────────────────────────────────
FNG_WEIGHT        = 2.0    # max score ±2.0 จาก F&G
FNG_EXTREME_FEAR  = 25     # ≤ นี้ → contrarian BUY signal
FNG_EXTREME_GREED = 75     # ≥ นี้ → contrarian SELL signal
FNG_CACHE_SECS    = 300    # cache API call 5 นาที

_fng_cache: tuple[float, int] = (0.0, 50)  # (timestamp, value)

# ── Timing ────────────────────────────────────────────────────────────────────
POLL_INTERVAL    = 30      # วินาที ระหว่างแต่ละรอบ
COOLDOWN_SECS    = 300     # วินาที รอหลังปิดไม้ก่อนจะเปิดใหม่

# ── Guardrails (ล็อคความเสี่ยง — ข้ามไม่ได้) ─────────────────────────────────
MAX_SESSION_LOSS_USDT = 100.0 # หยุดเทรดถ้าขาดทุนสะสม session เกิน $100 (~1 hard-SL)
MAX_TRADES_PER_HOUR   = 3     # สูงสุด 3 ไม้ต่อชั่วโมง ป้องกัน churn

# ── Regime Detection (ADX + Hurst) ───────────────────────────────────────────
ADX_TRENDING   = 25     # ADX > นี้ = trend แรง
ADX_WEAK       = 20     # ADX 20-25 = trend อ่อน
ADX_RANGING    = 15     # ADX < นี้ = ranging แน่นอน
HURST_RANGING  = 0.45   # H < นี้ = mean-reverting / ranging
HURST_TRENDING = 0.55   # H > นี้ = trending

# ── Dynamic Exit Rules ────────────────────────────────────────────────────────
TRAIL_ACTIVATE_ROE    = 10.0  # เปิด trailing หลัง ROE peak ≥ ค่านี้
TRAIL_GIVEBACK_PCT    = 0.40  # ปิดเมื่อ ROE ถอยลง 40% จาก peak
MIN_ROE_FOR_FLIP_EXIT = 5.0   # ต้องมีกำไร ≥ ค่านี้ ถึง early-exit เมื่อ trend กลับ

# ── ATR-based exits (Pine Script style) ──────────────────────────────────────
ATR_TP_MULT     = 2.5    # TP = 2.5 × ATR
ATR_SL_MULT     = 1.5    # SL_hard = 1.5 × ATR (capped by SL_HARD_ROE_PCT)
ATR_PERIOD_EXIT = 14

# ── Timeframe Weights (12h lookback) ─────────────────────────────────────────
TF_CONFIG: dict[str, dict] = {
    "3m":  {"limit": 240, "weight": 1.0},
    "5m":  {"limit": 144, "weight": 1.5},
    "15m": {"limit":  48, "weight": 2.0},  # 15m นับหนักที่สุด
}

# ── Internal files (ไม่ต้องแตะ) ──────────────────────────────────────────────
STATE_FILE    = "gambler_state.json"
LIVE_FILE     = "gambler_live.json"
COPILOT_FILE  = "gambler_copilot.json"
SETTINGS_FILE = "gambler_settings.json"
HISTORY_FILE  = "gambler_history.json"
TRADES_FILE   = "gambler_trades.json"
KELLY_FILE    = "gambler_kelly.json"
BACKTEST_FILE = "gambler_backtest.json"
HISTORY_MAX   = 200   # 200 × 30s ≈ 100 นาที

# defaults ที่ dashboard ใช้แสดงเมื่อยังไม่เคย save settings
_SETTINGS_DEFAULTS = {
    "capital_pct":     CAPITAL_PCT,
    "leverage":        LEVERAGE,
    "tp_roe_pct":      TP_ROE_PCT,
    "sl_hard_roe_pct": SL_HARD_ROE_PCT,
    "score_threshold": SCORE_THRESHOLD,  # 2.5
}


def _read_settings_bot() -> dict:
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE) as f:
                data = json.load(f)
            return {**_SETTINGS_DEFAULTS, **data}
    except Exception:
        pass
    return dict(_SETTINGS_DEFAULTS)


def _copilot_enabled() -> bool:
    try:
        if os.path.exists(COPILOT_FILE):
            with open(COPILOT_FILE) as f:
                return bool(json.load(f).get("enabled", False))
    except Exception:
        pass
    return False  # default OFF; เปิดได้จาก Dashboard (ต้องมี ANTHROPIC_API_KEY)


def _kelly_enabled() -> bool:
    try:
        if os.path.exists(KELLY_FILE):
            with open(KELLY_FILE) as f:
                return bool(json.load(f).get("enabled", False))
    except Exception:
        pass
    return False


def _apply_kelly(s: dict) -> dict:
    """Override capital_pct with ¼ Kelly fraction if Kelly is ON and backtest data exists."""
    if not _kelly_enabled():
        return s
    try:
        if not os.path.exists(BACKTEST_FILE):
            logger.debug("Kelly ON แต่ไม่มี backtest data — ใช้ capital_pct เดิม")
            return s
        with open(BACKTEST_FILE) as f:
            m = json.load(f).get("metrics", {})
        wins     = m.get("wins",         0)
        total    = m.get("total",        0)
        avg_win  = m.get("avg_win_roe",  0.0)
        avg_loss = m.get("avg_loss_roe", 0.0)
        if total < 10 or avg_win <= 0 or avg_loss <= 0:
            return s
        p       = wins / total
        b       = avg_win / avg_loss
        kelly_f = (p * b - (1 - p)) / b
        if kelly_f <= 0:
            logger.debug("Kelly ติดลบ (f=%.3f) — ใช้ capital_pct เดิม", kelly_f)
            return s
        frac = min(kelly_f * 0.25, 0.60)   # ¼ Kelly, cap 60%
        s = dict(s)
        s["capital_pct"] = frac
        logger.info("KELLY: f*=%.1f%%  ¼Kelly=%.1f%%  → capital_pct=%.1f%%",
                    kelly_f * 100, frac * 100, frac * 100)
        return s
    except Exception as exc:
        logger.warning("Kelly error: %s — ใช้ capital_pct เดิม", exc)
        return s


# ── Position dataclass ────────────────────────────────────────────────────────

@dataclass
class Position:
    side: str            # "LONG" | "SHORT"
    entry_price: float
    qty: float
    margin: float        # USDT margin committed
    tp_price: float
    sl_soft_price: float
    sl_hard_price: float
    entry_time: float    # unix timestamp
    peak_roe: float = 0.0   # highest ROE reached during this position's lifetime
    # context at entry — used for trade log analytics
    score_at_entry:  float = 0.0
    regime_at_entry: str   = ""
    adx_at_entry:    float = 0.0
    hurst_at_entry:  float = 0.0

    def roe(self, price: float) -> float:
        """Unrealised ROE % (excludes fees)."""
        pnl = (price - self.entry_price) if self.side == "LONG" else (self.entry_price - price)
        return pnl * self.qty / self.margin * 100 if self.margin else 0.0

    def save(self) -> None:
        with open(STATE_FILE, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls) -> Optional["Position"]:
        if not os.path.exists(STATE_FILE):
            return None
        try:
            with open(STATE_FILE) as f:
                d = json.load(f)
            # tolerate older state files missing newer fields
            d.setdefault("peak_roe",        0.0)
            d.setdefault("score_at_entry",  0.0)
            d.setdefault("regime_at_entry", "")
            d.setdefault("adx_at_entry",    0.0)
            d.setdefault("hurst_at_entry",  0.0)
            p = cls(**d)
            logger.info("Restored position: %s @ %.4f  qty=%.4f  peak_roe=%.2f%%",
                        p.side, p.entry_price, p.qty, p.peak_roe)
            return p
        except Exception as exc:
            logger.warning("Cannot restore state: %s", exc)
            return None

    @classmethod
    def clear(cls) -> None:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)


# ── Fear & Greed helpers ──────────────────────────────────────────────────────

def _fng_label(v: int) -> str:
    if v <= 25: return "Extreme Fear"
    if v <= 45: return "Fear"
    if v <= 55: return "Neutral"
    if v <= 75: return "Greed"
    return "Extreme Greed"


def get_fear_and_greed() -> int:
    """Fetch F&G index with 5-min cache. Returns cached/50 on error."""
    global _fng_cache
    if time.time() - _fng_cache[0] < FNG_CACHE_SECS:
        return _fng_cache[1]
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=5).json()
        val = int(r["data"][0]["value"])
        _fng_cache = (time.time(), val)
        logger.debug("F&G updated: %d (%s)", val, _fng_label(val))
        return val
    except Exception as exc:
        logger.debug("F&G fetch failed: %s — using cached %d", exc, _fng_cache[1])
        return _fng_cache[1] or 50


# ── Trade log ────────────────────────────────────────────────────────────────

def _log_trade(pos: "Position", exit_price: float, roe: float, reason: str) -> None:
    """Append one closed-trade record to gambler_trades.json."""
    try:
        duration_min = round((time.time() - pos.entry_time) / 60, 1)
        record = {
            "exit_time":    time.strftime("%Y-%m-%d %H:%M:%S"),
            "entry_time":   time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pos.entry_time)),
            "duration_min": duration_min,
            "side":         pos.side,
            "entry_price":  pos.entry_price,
            "exit_price":   round(exit_price, 6),
            "roe":          round(roe, 2),
            "pnl_usdt":     round(roe / 100 * pos.margin, 2),
            "reason":       reason,
            "score_at_entry":  round(pos.score_at_entry, 3),
            "regime_at_entry": pos.regime_at_entry,
            "adx_at_entry":    round(pos.adx_at_entry, 1),
            "hurst_at_entry":  round(pos.hurst_at_entry, 3),
            "peak_roe":        round(pos.peak_roe, 2),
        }
        trades: list = []
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE) as f:
                trades = json.load(f)
        trades.append(record)
        with open(TRADES_FILE, "w") as f:
            json.dump(trades, f, indent=2)
    except Exception as exc:
        logger.debug("_log_trade error: %s", exc)


# ── Hurst Exponent ────────────────────────────────────────────────────────────

def hurst_exponent(prices: list[float], min_lag: int = 2, max_lag: int = 20) -> float:
    """
    Estimate Hurst exponent via variance of log-returns at multiple lags.
    H > 0.55 → trending  |  H ≈ 0.5 → random walk  |  H < 0.45 → ranging/mean-reverting
    """
    if len(prices) < max_lag + 2:
        return 0.5
    try:
        log_rets = [math.log(prices[i] / prices[i-1])
                    for i in range(1, len(prices)) if prices[i-1] > 0]
        lags = list(range(min_lag, min(max_lag, len(log_rets) // 2)))
        if len(lags) < 3:
            return 0.5
        log_lags, log_stds = [], []
        for lag in lags:
            diffs = [log_rets[i + lag] - log_rets[i] for i in range(len(log_rets) - lag)]
            var = sum(d * d for d in diffs) / len(diffs)
            if var > 0:
                log_lags.append(math.log(lag))
                log_stds.append(math.log(var) / 2)   # log(std) = log(var)/2
        n = len(log_lags)
        if n < 2:
            return 0.5
        mx = sum(log_lags) / n
        my = sum(log_stds)  / n
        num = sum((log_lags[i] - mx) * (log_stds[i] - my) for i in range(n))
        den = sum((log_lags[i] - mx) ** 2 for i in range(n))
        return round(num / den, 4) if den != 0 else 0.5
    except Exception:
        return 0.5


# ── Regime Detection ─────────────────────────────────────────────────────────

def detect_regime(adx_val: float, hurst_val: float) -> tuple[str, float]:
    """
    Combine ADX + Hurst → (regime_label, threshold_multiplier).

    | Regime         | ADX         | Hurst        | Mult |
    |----------------|-------------|--------------|------|
    | TRENDING       | > 25        | > 0.55       | 1.0  |
    | TRENDING       | > 25        | any          | 1.0  |
    | WEAK_TREND     | 20–25       | any          | 1.2  |
    | RANGING        | < 20 or H<0.45 | —         | 1.4  |
    | RANGING_STRONG | < 15 and H<0.45 | —        | 1.6  |

    Higher multiplier = harder threshold = fewer (better quality) entries.
    """
    strong_ranging = adx_val < ADX_RANGING and hurst_val < HURST_RANGING
    any_ranging    = adx_val < ADX_WEAK    or  hurst_val < HURST_RANGING

    if strong_ranging:
        return "RANGING_STRONG", 1.6
    elif any_ranging:
        return "RANGING", 1.4
    elif adx_val < ADX_TRENDING:
        return "WEAK_TREND", 1.2
    else:
        return "TRENDING", 1.0


# ── Scoring per timeframe ─────────────────────────────────────────────────────

def _score_tf(candles: CandleData, weight: float) -> tuple[float, dict]:
    """
    Score one TF against 4 signals.
    Returns (weighted_score, info_dict).
    Max raw score ≈ (1.0 + 0.8 + 0.7) × weight × 1.3 (surge)
    """
    closes  = candles.closes
    highs   = candles.highs
    lows    = candles.lows
    volumes = candles.volumes
    price   = closes[-1]
    score   = 0.0
    info: dict = {}

    # 1. EMA 9/21 trend
    ema_s_vals = ema(closes, config.EMA_SHORT)
    ema_l_vals = ema(closes, config.EMA_LONG)
    if ema_s_vals and ema_l_vals:
        es, el = ema_s_vals[-1], ema_l_vals[-1]
        gap = (es - el) / el * 100 if el else 0
        if gap > EMA_GAP_PCT:
            score += 1.0 * weight
            info["ema"] = f"BULL gap={gap:.3f}%"
        elif gap < -EMA_GAP_PCT:
            score -= 1.0 * weight
            info["ema"] = f"BEAR gap={gap:.3f}%"
        else:
            info["ema"] = f"NEUTRAL gap={gap:.3f}%"

    # 1b. CDC Action Zone (EMA 12/26) — backbone trend confirmation
    cdc_s_vals = ema(closes, 12)
    cdc_l_vals = ema(closes, 26)
    if cdc_s_vals and cdc_l_vals:
        cs, cl = cdc_s_vals[-1], cdc_l_vals[-1]
        if cs > cl:
            score += 0.5 * weight
            info["cdc"] = f"BULL {cs:.4f}>{cl:.4f}"
        else:
            score -= 0.5 * weight
            info["cdc"] = f"BEAR {cs:.4f}<{cl:.4f}"

    # 2. RSI
    rsi_val = calc_rsi(closes, config.RSI_PERIOD)
    if rsi_val < RSI_OVERSOLD:
        score += 0.8 * weight
        info["rsi"] = f"OVERSOLD {rsi_val:.1f}"
    elif rsi_val > RSI_OVERBOUGHT:
        score -= 0.8 * weight
        info["rsi"] = f"OVERBOUGHT {rsi_val:.1f}"
    else:
        info["rsi"] = f"neutral {rsi_val:.1f}"

    # 3. Bollinger Bands (20, 2σ)
    bb = bollinger_bands(closes, 20, 2.0)
    if price < bb.lower:
        score += 0.7 * weight
        info["bb"] = f"BELOW_LOWER bw={bb.bandwidth:.4f}"
    elif price > bb.upper:
        score -= 0.7 * weight
        info["bb"] = f"ABOVE_UPPER bw={bb.bandwidth:.4f}"
    else:
        pos_pct = (price - bb.lower) / (bb.upper - bb.lower) * 100 if bb.upper != bb.lower else 50
        info["bb"] = f"inside {pos_pct:.0f}% bw={bb.bandwidth:.4f}"

    # 3b. VWAP bias — price vs rolling 100-bar VWAP
    vwap_val = calc_vwap(highs, lows, closes, volumes, period=min(100, len(closes)))
    if vwap_val > 0:
        gap_vwap = (price - vwap_val) / vwap_val * 100
        if gap_vwap > 0.05:
            score += 0.3 * weight
            info["vwap"] = f"ABOVE +{gap_vwap:.3f}%"
        elif gap_vwap < -0.05:
            score -= 0.3 * weight
            info["vwap"] = f"BELOW {gap_vwap:.3f}%"
        else:
            info["vwap"] = f"at {vwap_val:.4f}"

    # 4. Volume Surge — amplify score if current bar's volume > 2× avg of prev 20
    vol_ratio = 1.0
    if len(volumes) >= 22:
        avg_vol = sum(volumes[-21:-1]) / 20
        if avg_vol > 0:
            vol_ratio = volumes[-1] / avg_vol
            if vol_ratio >= VOL_SURGE_MULT:
                score *= 1.3
                info["vol"] = f"SURGE x{vol_ratio:.1f}"
            else:
                info["vol"] = f"normal x{vol_ratio:.1f}"

    # ATR (info only — used for min TP distance)
    atr_val = calc_atr(highs, lows, closes, 14)
    info["atr"] = round(atr_val, 6)

    return score, info


def compute_signal(symbol: str, threshold: float = SCORE_THRESHOLD) -> tuple[str, float, dict, float]:
    """
    Fetch all TFs, aggregate scores, return:
      (direction, total_score, details_per_tf, atr_5m)
    direction: "LONG" | "SHORT" | "SKIP"
    """
    total = 0.0
    details: dict = {}
    atr_5m = 0.0
    candles_15m_ref = None

    for tf, cfg in TF_CONFIG.items():
        candles = binance.get_klines(symbol, tf, cfg["limit"])
        if not candles or len(candles.closes) < 30:
            logger.warning("Insufficient candles: %s %s (%d)",
                           symbol, tf, len(candles.closes) if candles else 0)
            continue
        sc, info = _score_tf(candles, cfg["weight"])
        total += sc
        details[tf] = {"score": round(sc, 3), **info}
        if tf == "5m":
            atr_5m = info.get("atr", 0.0)
        if tf == "15m":
            candles_15m_ref = candles

    # Fear & Greed Index — contrarian psychology filter (Trinity layer)
    fng = get_fear_and_greed()
    fng_score = 0.0
    if fng <= FNG_EXTREME_FEAR:
        fng_score = FNG_WEIGHT    # Extreme Fear = contrarian buy signal
    elif fng >= FNG_EXTREME_GREED:
        fng_score = -FNG_WEIGHT   # Extreme Greed = contrarian sell signal
    total += fng_score
    details["fng"] = {"score": round(fng_score, 2), "value": fng, "label": _fng_label(fng)}

    # Regime Detection — ADX + Hurst from 15m candles
    hurst_val  = 0.5
    adx_val    = 20.0
    plus_di    = 25.0
    minus_di   = 25.0
    if candles_15m_ref:
        hurst_val = hurst_exponent(candles_15m_ref.closes)
        adx_res   = calc_adx(candles_15m_ref.highs, candles_15m_ref.lows,
                              candles_15m_ref.closes, 14)
        adx_val, plus_di, minus_di = adx_res.adx, adx_res.plus_di, adx_res.minus_di

    regime, regime_mult = detect_regime(adx_val, hurst_val)
    details["regime"] = {
        "label":    regime,
        "mult":     regime_mult,
        "adx":      round(adx_val, 1),
        "plus_di":  round(plus_di, 1),
        "minus_di": round(minus_di, 1),
        "hurst":    round(hurst_val, 3),
    }

    eff_threshold = threshold * regime_mult
    if regime_mult != 1.0:
        logger.debug("Regime=%s ADX=%.1f H=%.3f → threshold %.2f → %.2f",
                     regime, adx_val, hurst_val, threshold, eff_threshold)

    if total >= eff_threshold:
        direction = "LONG"
    elif total <= -eff_threshold:
        direction = "SHORT"
    else:
        direction = "SKIP"

    return direction, total, details, atr_5m


# ── TP / SL levels ────────────────────────────────────────────────────────────

def calc_levels(entry: float, side: str, atr_val: float = 0.0,
                tp_roe: float = TP_ROE_PCT, sl_hard_roe: float = SL_HARD_ROE_PCT,
                leverage: int = LEVERAGE) -> tuple[float, float, float]:
    """
    Compute TP, SL_soft, SL_hard as absolute prices.

    ATR-adaptive (Pine Script style):
      TP move      = max(ROE-based,     ATR_TP_MULT × ATR / entry)  — farther target wins
      SL_hard move = min(ROE-based cap, ATR_SL_MULT × ATR / entry)  — tighter stop wins
      SL_soft move = ROE-based 10% (trend re-check trigger, unchanged)

    Falls back to pure ROE-based when atr_val == 0.
    """
    fee_pct          = TAKER_FEE * 2
    tp_roe_move      = tp_roe      / 100 / leverage + fee_pct
    sl_soft_move     = SL_SOFT_ROE_PCT / 100 / leverage
    sl_hard_roe_move = sl_hard_roe / 100 / leverage

    if atr_val > 0:
        atr_tp_move  = atr_val / entry * ATR_TP_MULT
        tp_move      = max(tp_roe_move, atr_tp_move)
        sl_hard_move = sl_hard_roe_move   # fixed ROE cap — ATR too small for low-vol coins
    else:
        tp_move      = tp_roe_move
        sl_hard_move = sl_hard_roe_move

    if side == "LONG":
        return (
            entry * (1 + tp_move),
            entry * (1 - sl_soft_move),
            entry * (1 - sl_hard_move),
        )
    return (
        entry * (1 - tp_move),
        entry * (1 + sl_soft_move),
        entry * (1 + sl_hard_move),
    )


# ── Order helpers ─────────────────────────────────────────────────────────────

def _round_qty(qty: float) -> float:
    info = binance.get_symbol_info()
    return math.floor(qty * 10 ** info.qty_precision) / 10 ** info.qty_precision


def enter_market(side: str, margin: float, ref_price: float, dry_run: bool) -> Optional[tuple[float, float]]:
    """
    Place MARKET order.  Returns (fill_price, filled_qty) or None on failure.
    """
    notional = margin * LEVERAGE
    qty = _round_qty(notional / ref_price)
    if qty <= 0:
        logger.error("Qty rounds to 0 — min notional issue (margin=$%.2f notional=$%.2f)", margin, notional)
        return None

    if dry_run:
        logger.info("[DRY] MARKET %s  %.4f %s  margin=$%.2f  notional=$%.2f",
                    side, qty, config.SYMBOL, margin, notional)
        return ref_price, qty

    try:
        order_side = Client.SIDE_BUY if side == "LONG" else Client.SIDE_SELL
        resp = binance._client.futures_create_order(
            symbol=config.SYMBOL,
            side=order_side,
            type=Client.FUTURE_ORDER_TYPE_MARKET,
            quantity=qty,
        )
        fill = float(resp.get("avgPrice") or ref_price)
        filled_qty = float(resp.get("executedQty") or qty)
        logger.info("MARKET %s filled  qty=%.4f @ %.4f", side, filled_qty, fill)
        return fill, filled_qty
    except BinanceAPIException as exc:
        logger.error("enter_market error: %s", exc)
        return None


def close_market(pos: Position, reason: str, dry_run: bool) -> None:
    """Close open position with reduceOnly market order."""
    if dry_run:
        logger.info("[DRY] CLOSE %s  qty=%.4f  reason=%s", pos.side, pos.qty, reason)
        return
    try:
        close_side = Client.SIDE_SELL if pos.side == "LONG" else Client.SIDE_BUY
        binance._client.futures_create_order(
            symbol=config.SYMBOL,
            side=close_side,
            type=Client.FUTURE_ORDER_TYPE_MARKET,
            quantity=_round_qty(pos.qty),
            reduceOnly=True,
        )
        logger.info("CLOSED %s  qty=%.4f  reason=%s", pos.side, pos.qty, reason)
    except BinanceAPIException as exc:
        logger.error("close_market error: %s", exc)


def setup_leverage(dry_run: bool) -> None:
    if dry_run or not config.is_futures:
        return
    try:
        binance._client.futures_change_leverage(symbol=config.SYMBOL, leverage=LEVERAGE)
        logger.info("Leverage set to %dx", LEVERAGE)
    except BinanceAPIException as exc:
        logger.warning("set leverage: %s", exc)
    try:
        binance._client.futures_change_margin_type(symbol=config.SYMBOL, marginType="ISOLATED")
        logger.info("Margin type: ISOLATED")
    except BinanceAPIException as exc:
        if "4046" not in str(exc):
            logger.warning("set margin type: %s", exc)


# ── Dashboard ─────────────────────────────────────────────────────────────────

def _bar(score: float, width: int = 20) -> str:
    """Simple ASCII score bar centered at 0."""
    half = width // 2
    filled = int(abs(score) / SCORE_THRESHOLD * half)
    filled = min(filled, half)
    if score >= 0:
        return " " * half + "█" * filled + " " * (half - filled)
    else:
        return " " * (half - filled) + "█" * filled + " " * half


def _save_live(price: float, direction: str, score: float, details: dict,
               cooldown_left: float, copilot: Optional[dict] = None,
               guards: Optional[dict] = None,
               pos: Optional["Position"] = None) -> None:
    """Write lightweight state snapshot for the web dashboard."""
    try:
        data: dict = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": config.SYMBOL,
            "price": price,
            "direction": direction,
            "score": round(score, 3),
            "cooldown_left": round(cooldown_left, 0),
            "details": {tf: {k: v for k, v in d.items()} for tf, d in details.items()},
        }
        if copilot:
            data["copilot"] = copilot
        if guards:
            data["guards"] = guards
        with open(LIVE_FILE, "w") as f:
            json.dump(data, f)

        # append per-poll snapshot to rolling history for chart + analysis
        reg = details.get("regime", {})
        entry = {
            "ts":     time.strftime("%Y-%m-%d %H:%M:%S"),  # full datetime
            "price":  round(price, 6),
            "dir":    direction,
            "3m":     round(details.get("3m",  {}).get("score", 0), 3),
            "5m":     round(details.get("5m",  {}).get("score", 0), 3),
            "15m":    round(details.get("15m", {}).get("score", 0), 3),
            "fng":    round(details.get("fng", {}).get("score", 0), 2),
            "total":  round(score, 3),
            "regime": reg.get("label", ""),
            "adx":    reg.get("adx", 0.0),
            "hurst":  reg.get("hurst", 0.5),
            "pos_side": pos.side          if pos else "",
            "pos_roe":  round(pos.roe(price), 2) if pos else None,
        }
        try:
            history: list = []
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE) as fh:
                    history = json.load(fh)
            history.append(entry)
            if len(history) > HISTORY_MAX:
                history = history[-HISTORY_MAX:]
            with open(HISTORY_FILE, "w") as fh:
                json.dump(history, fh)
        except Exception:
            pass

    except Exception as exc:
        logger.debug("_save_live error: %s", exc)


def print_dashboard(pos: Optional[Position], price: float, score: float,
                    direction: str, details: dict, cooldown_left: float,
                    guards: Optional[dict] = None) -> None:
    ts = time.strftime("%H:%M:%S")
    arrow = {"LONG": "▲", "SHORT": "▼", "SKIP": "◆"}.get(direction, "?")
    bar = _bar(score)

    reg = details.get("regime", {})
    hurst_tag = (f"  [{reg.get('label','?')}  ADX={reg.get('adx',0):.0f}"
                 f"  H={reg.get('hurst',0.5):.3f}  mult×{reg.get('mult',1.0):.1f}]") if reg else ""

    print(f"\n{'='*62}")
    print(f"  {ts}  {config.SYMBOL}  ${price:.4f}  [GAMBLER 8x  cap={CAPITAL_PCT*100:.0f}%]{hurst_tag}")
    print(f"  Score [{bar}] {score:+.2f}  →  {arrow} {direction}")
    if cooldown_left > 0:
        print(f"  Cooldown: {cooldown_left:.0f}s remaining")
    if guards:
        blocked = guards.get("blocked_reason", "")
        pnl_str = f"session_pnl=${guards.get('session_pnl', 0):+.2f}"
        hr_str  = f"trades/hr={guards.get('trades_this_hour', 0)}/{MAX_TRADES_PER_HOUR}"
        status  = f"  [GUARDRAIL BLOCKED: {blocked}]" if blocked else ""
        print(f"  Guards: {pnl_str}  {hr_str}{status}")

    if pos:
        roe = pos.roe(price)
        roe_bar = "+" * max(0, int(roe / 5)) if roe >= 0 else "-" * max(0, int(-roe / 5))
        print(f"  OPEN {pos.side} @ {pos.entry_price:.4f}  qty={pos.qty:.4f}"
              f"  ROE={roe:+.2f}% [{roe_bar}]  peak={pos.peak_roe:+.2f}%")
        print(f"  TP={pos.tp_price:.4f}  SL_soft={pos.sl_soft_price:.4f}  SL_hard={pos.sl_hard_price:.4f}")
        # show trailing stop trigger level once armed
        if pos.peak_roe >= TRAIL_ACTIVATE_ROE:
            trail_roe = pos.peak_roe * (1 - TRAIL_GIVEBACK_PCT)
            print(f"  Trailing armed: exit if ROE drops below {trail_roe:+.2f}%")
    else:
        print(f"  No open position")

    fng_d = details.get("fng", {})
    print(f"  ── TF breakdown ──────────────────────────────────────────")
    print(f"  F&G Index: {fng_d.get('value', 50)}  ({fng_d.get('label','?')})  score={fng_d.get('score', 0):+.2f}")
    for tf, d in details.items():
        if tf in ("fng", "hurst", "regime"):
            continue
        wt = TF_CONFIG[tf]["weight"]
        print(f"  {tf} (w={wt:.1f}) score={d.get('score',0):+.3f}"
              f"  EMA:{d.get('ema','?')[:16]}  RSI:{d.get('rsi','?')[:18]}")
        print(f"          BB:{d.get('bb','?')[:22]}  vol:{d.get('vol','?')[:14]}  ATR={d.get('atr',0):.5f}")
    print(f"{'='*62}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(symbol: str, dry_run: bool) -> None:
    logger.info("=== Gambler Bot  symbol=%s  8x  capital=%.0f%%  dry=%s ===",
                symbol, CAPITAL_PCT * 100, dry_run)
    logger.info("TP=%.0f%% ROE  SL_soft=%.0f%%  SL_hard=%.0f%%  fee=%.2f%%/leg  threshold=%.1f",
                TP_ROE_PCT, SL_SOFT_ROE_PCT, SL_HARD_ROE_PCT, TAKER_FEE * 100, SCORE_THRESHOLD)

    setup_leverage(dry_run)
    pos: Optional[Position] = Position.load()
    last_close_time: float = 0.0
    skip_streak = 0

    # Guardrail state (resets on restart)
    session_pnl: float = 0.0         # cumulative closed P&L this session (USDT)
    trade_log:   list[float] = []    # timestamps of all entries (for hourly freq check)
    _last_guard: str = ""            # last guard reason — suppresses repeated log spam

    while True:
        try:
            s = _read_settings_bot()   # re-read each poll so dashboard changes apply immediately
            s = _apply_kelly(s)        # override capital_pct if Kelly is ON
            price   = binance.get_price()
            direction, score, details, atr_5m = compute_signal(symbol, threshold=s["score_threshold"])
            cooldown_left = max(0.0, COOLDOWN_SECS - (time.time() - last_close_time))

            # ── Manage existing position ──────────────────────────────────
            if pos is not None:
                roe = pos.roe(price)
                # track running peak so trailing / flip rules have reference
                if roe > pos.peak_roe:
                    pos.peak_roe = roe
                    pos.save()

                def _close(reason: str) -> None:
                    nonlocal pos, session_pnl
                    realized = roe / 100 * pos.margin
                    session_pnl += realized
                    _log_trade(pos, price, roe, reason)
                    close_market(pos, reason, dry_run)
                    pos = None; Position.clear()

                # Hard SL — always close
                hard_hit = (pos.side == "LONG"  and price <= pos.sl_hard_price) or \
                           (pos.side == "SHORT" and price >= pos.sl_hard_price)
                if hard_hit:
                    logger.warning("HARD SL  ROE=%.2f%%  price=%.4f", roe, price)
                    _close(f"HARD_SL({roe:.1f}%)"); last_close_time = time.time()

                # TP hit
                elif (pos.side == "LONG"  and price >= pos.tp_price) or \
                     (pos.side == "SHORT" and price <= pos.tp_price):
                    logger.info("TP hit!  ROE=%.2f%%  price=%.4f", roe, price)
                    _close(f"TP({roe:.1f}%)"); last_close_time = time.time()

                # Trailing stop — once peak hit activation, lock-in gains on pullback
                elif pos.peak_roe >= TRAIL_ACTIVATE_ROE and \
                     roe <= pos.peak_roe * (1 - TRAIL_GIVEBACK_PCT):
                    giveback = pos.peak_roe - roe
                    logger.info(
                        "TRAIL_STOP  peak=%.2f%%  roe=%.2f%%  giveback=%.2f%%",
                        pos.peak_roe, roe, giveback,
                    )
                    _close(f"TRAIL(peak={pos.peak_roe:.1f}%→{roe:.1f}%)"); last_close_time = time.time()

                # Early TP — trend flipped while in profit, take the money
                elif roe >= MIN_ROE_FOR_FLIP_EXIT and \
                     direction not in ("SKIP", pos.side):
                    logger.info(
                        "TREND_FLIP_TP  dir=%s vs pos=%s  score=%+.2f  ROE=%.2f%%",
                        direction, pos.side, score, roe,
                    )
                    _close(f"FLIP_TP({roe:.1f}%)"); last_close_time = time.time()

                # Soft SL — re-check trend
                elif (pos.side == "LONG"  and price <= pos.sl_soft_price) or \
                     (pos.side == "SHORT" and price >= pos.sl_soft_price):
                    logger.warning("Soft SL  ROE=%.2f%%  re-checking trend...", roe)
                    trend_ok = (pos.side == direction)
                    if trend_ok:
                        logger.info("Trend holds — staying in position  ROE=%.2f%%", roe)
                    else:
                        logger.warning("Trend gone — closing  ROE=%.2f%%", roe)
                        _close(f"SOFT_SL+FLIP({roe:.1f}%)"); last_close_time = time.time()

                # Strong opposite signal — early exit (catches losses too)
                elif direction not in ("SKIP", pos.side) and abs(score) >= SCORE_THRESHOLD * 1.2:
                    logger.warning("Opposite signal (%s score=%.2f) — early exit  ROE=%.2f%%",
                                   direction, score, roe)
                    _close(f"EARLY_EXIT({roe:.1f}%)"); last_close_time = time.time()

            # ── Guardrail check ───────────────────────────────────────────
            now = time.time()
            trade_log[:] = [t for t in trade_log if now - t < 3600]  # keep last 1h
            guard_blocked = ""
            if session_pnl <= -MAX_SESSION_LOSS_USDT:
                guard_blocked = f"daily_loss ${session_pnl:.2f}"
            elif len(trade_log) >= MAX_TRADES_PER_HOUR:
                guard_blocked = f"freq {len(trade_log)}/hr"
            if guard_blocked and guard_blocked != _last_guard:
                logger.warning("GUARDRAIL [%s] — no new entries", guard_blocked)
            _last_guard = guard_blocked

            guards = {
                "session_pnl":     round(session_pnl, 2),
                "trades_this_hour": len(trade_log),
                "blocked_reason":  guard_blocked,
            }

            # ── Enter new position ────────────────────────────────────────
            copilot_result: Optional[dict] = None
            if pos is None and direction != "SKIP" and cooldown_left == 0 and not guard_blocked:
                lev = int(s["leverage"])
                balance = binance.get_balance("USDT")
                margin  = balance * s["capital_pct"]

                logger.info("Settings: cap=%.0f%% lev=%dx TP=%.0f%% SL=%.0f%% threshold=%.1f",
                            s["capital_pct"]*100, lev,
                            s["tp_roe_pct"], s["sl_hard_roe_pct"], s["score_threshold"])

                # Co-pilot: scale margin by AI confidence (0.5× – 1.5×)
                if _copilot_enabled():
                    try:
                        from ai_copilot import evaluate_trade as _cp_eval
                        copilot_result = _cp_eval(direction, score, details, price, atr_5m)
                        confidence = copilot_result.get("confidence", 60)
                        multiplier = 0.5 + (confidence / 100.0) * 1.0
                        margin = margin * multiplier
                        logger.info(
                            "CO-PILOT: confidence=%d%% quality=%s → margin=$%.2f (×%.2f) | %s",
                            confidence, copilot_result.get("quality", "?"),
                            margin, multiplier, copilot_result.get("reasoning", ""),
                        )
                    except Exception as cp_exc:
                        logger.warning("CO-PILOT failed: %s — using base margin", cp_exc)

                if margin * lev < 5:
                    logger.warning("Notional too small ($%.2f) — need balance > $%.2f",
                                   margin * lev, 5 / lev / s["capital_pct"])
                else:
                    result = enter_market(direction, margin, price, dry_run)
                    if result:
                        fill_price, filled_qty = result
                        tp, sl_soft, sl_hard = calc_levels(
                            fill_price, direction, atr_5m,
                            tp_roe=s["tp_roe_pct"],
                            sl_hard_roe=s["sl_hard_roe_pct"],
                            leverage=lev,
                        )
                        _reg = details.get("regime", {})
                        pos = Position(
                            side=direction,
                            entry_price=fill_price,
                            qty=filled_qty,
                            margin=margin,
                            tp_price=tp,
                            sl_soft_price=sl_soft,
                            sl_hard_price=sl_hard,
                            entry_time=time.time(),
                            score_at_entry=score,
                            regime_at_entry=_reg.get("label", ""),
                            adx_at_entry=_reg.get("adx", 0.0),
                            hurst_at_entry=_reg.get("hurst", 0.5),
                        )
                        pos.save()
                        tp_pct  = abs(tp - fill_price) / fill_price * 100
                        slh_pct = abs(sl_hard - fill_price) / fill_price * 100
                        logger.info(
                            "ENTERED %s @ %.4f  TP=%.4f (+%.2f%% price / +%.0f%% ROE)"
                            "  SL_hard=%.4f (-%.2f%% price / -%.0f%% ROE)",
                            direction, fill_price,
                            tp,  tp_pct,  tp_pct  * lev,
                            sl_hard, slh_pct, slh_pct * lev,
                        )
                        trade_log.append(time.time())
                        skip_streak = 0
            elif direction == "SKIP" or cooldown_left > 0 or guard_blocked:
                skip_streak += 1
                if skip_streak % 6 == 1:
                    reason = f"cooldown {cooldown_left:.0f}s" if cooldown_left > 0 else f"score={score:+.2f}"
                    logger.info("Waiting: %s  (streak=%d)", reason, skip_streak)

            _save_live(price, direction, score, details, cooldown_left, copilot_result, guards, pos)
            print_dashboard(pos, price, score, direction, details, cooldown_left, guards)

        except KeyboardInterrupt:
            logger.info("Interrupted")
            if pos and not dry_run:
                logger.warning("! Open position exists — check Binance manually: %s @ %.4f qty=%.4f",
                               pos.side, pos.entry_price, pos.qty)
            break
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                ConnectionResetError, OSError) as exc:
            # network hiccup — log one-liner only, no traceback
            logger.warning("Network error (will retry in %ds): %s", POLL_INTERVAL, exc.__class__.__name__)
        except Exception as exc:
            logger.error("Unexpected error: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gambler Bot — directional swing trader")
    parser.add_argument("--symbol",  default=config.SYMBOL, help="Symbol (default from .env)")
    parser.add_argument("--dry-run", action="store_true",   help="No real orders")
    args = parser.parse_args()

    run(args.symbol, args.dry_run or config.DRY_RUN)
