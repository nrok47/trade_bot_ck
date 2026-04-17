"""
Technical indicators — คำนวณจาก Binance Kline data

ตัวชี้วัดที่ใช้:
  EMA          — Exponential Moving Average (ทิศทาง trend)
  RSI          — Relative Strength Index (momentum)
  Bull/Bear    — Elder's Bull & Bear Power
  ATR          — Average True Range (ความผันผวน → ใช้กำหนดกรอบ grid)
  Bollinger    — Bollinger Bands (upper/middle/lower)
  RSI   — Relative Strength Index (momentum, overbought/oversold)
  Bull/Bear Power (Elder) — วัดพลังของ buyer vs seller ในแต่ละ candle
    Bull Power = High - EMA  → บวก = bull ควบคุม
    Bear Power = Low  - EMA  → ลบ  = bear ควบคุม
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass
class CandleData:
    opens: list[float]
    highs: list[float]
    lows: list[float]
    closes: list[float]
    volumes: list[float]

    def __len__(self) -> int:
        return len(self.closes)


# ── EMA ───────────────────────────────────────────────────────────────────────

def ema(values: Sequence[float], period: int) -> list[float]:
    """Exponential Moving Average."""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


# ── RSI ───────────────────────────────────────────────────────────────────────

def rsi(closes: Sequence[float], period: int = 14) -> float:
    """RSI ค่าสุดท้าย (0-100).  >70 = overbought, <30 = oversold."""
    if len(closes) < period + 1:
        return 50.0
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in diffs]
    losses = [abs(min(d, 0)) for d in diffs]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(diffs)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ── Bull / Bear Power ─────────────────────────────────────────────────────────

def bull_bear_power(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    ema_period: int = 13,
) -> tuple[float, float]:
    """
    Elder's Bull & Bear Power (ค่าล่าสุด):
      Bull Power = High[-1] - EMA(Close, period)[-1]
      Bear Power = Low[-1]  - EMA(Close, period)[-1]

    ถ้า Bull Power > 0  AND ขึ้น → bull strong
    ถ้า Bear Power < 0  AND ลง  → bear strong
    """
    ema_vals = ema(closes, ema_period)
    if not ema_vals:
        return 0.0, 0.0
    last_ema = ema_vals[-1]
    bull = highs[-1] - last_ema
    bear = lows[-1] - last_ema
    return bull, bear


# ── Combined Signal ───────────────────────────────────────────────────────────

@dataclass
class TrendSignal:
    direction: str          # "BULL" | "BEAR" | "NEUTRAL"
    ema_short: float
    ema_long: float
    rsi_value: float
    bull_power: float
    bear_power: float
    strength: float         # 0.0–1.0 ความมั่นใจของ signal

    @property
    def is_bull(self) -> bool:
        return self.direction == "BULL"

    @property
    def is_bear(self) -> bool:
        return self.direction == "BEAR"

    def label(self) -> str:
        icons = {"BULL": "▲ BULL", "BEAR": "▼ BEAR", "NEUTRAL": "◆ NEUTRAL"}
        return icons.get(self.direction, self.direction)


def analyze_trend(
    candles: CandleData,
    ema_short_period: int = 9,
    ema_long_period: int = 21,
    rsi_period: int = 14,
    ema_min_gap_pct: float = 0.1,
) -> TrendSignal:
    """
    รวม EMA + RSI + Bull/Bear Power เป็น signal เดียว

    Bull conditions  (ต้องผ่านอย่างน้อย 3/4):
      1. EMA short > EMA long  (uptrend)
      2. RSI > 50
      3. Bull Power > 0
      4. Bear Power กำลังดีขึ้น (ไม่ลงต่ำกว่าค่าก่อน)

    Bear conditions (ต้องผ่านอย่างน้อย 3/4):
      1. EMA short < EMA long  (downtrend)
      2. RSI < 50
      3. Bear Power < 0
      4. Bull Power กำลังแย่ลง
    """
    closes = candles.closes
    highs = candles.highs
    lows = candles.lows

    ema_s_vals = ema(closes, ema_short_period)
    ema_l_vals = ema(closes, ema_long_period)

    if not ema_s_vals or not ema_l_vals:
        return TrendSignal("NEUTRAL", 0, 0, 50, 0, 0, 0)

    ema_s = ema_s_vals[-1]
    ema_l = ema_l_vals[-1]
    rsi_val = rsi(closes, rsi_period)
    bull_p, bear_p = bull_bear_power(highs, lows, closes, ema_short_period)

    # Previous bar's bull/bear power (for trend of the power itself)
    prev_bull_p, prev_bear_p = 0.0, 0.0
    if len(closes) > 1:
        prev_bull_p, prev_bear_p = bull_bear_power(
            highs[:-1], lows[:-1], closes[:-1], ema_short_period
        )

    # EMA gap filter: ต้องห่างกันอย่างน้อย ema_min_gap_pct% ถึงนับเป็น signal
    # ป้องกัน noise cross เมื่อ EMA ห่างกันแค่ $0.0001 ในช่วง sideways
    ema_gap_pct = abs(ema_s - ema_l) / ema_l * 100 if ema_l > 0 else 0
    ema_gap_ok = ema_gap_pct >= ema_min_gap_pct

    # Score bull conditions (0–4)
    bull_score = sum([
        ema_s > ema_l and ema_gap_ok,
        rsi_val > 50,
        bull_p > 0,
        bear_p > prev_bear_p,   # bear power improving (less negative)
    ])

    # Score bear conditions (0–4)
    bear_score = sum([
        ema_s < ema_l and ema_gap_ok,
        rsi_val < 50,
        bear_p < 0,
        bull_p < prev_bull_p,   # bull power weakening
    ])

    if bull_score >= 3 and bull_score > bear_score:
        direction = "BULL"
        strength = bull_score / 4
    elif bear_score >= 3 and bear_score > bull_score:
        direction = "BEAR"
        strength = bear_score / 4
    else:
        direction = "NEUTRAL"
        strength = 0.5

    return TrendSignal(
        direction=direction,
        ema_short=ema_s,
        ema_long=ema_l,
        rsi_value=rsi_val,
        bull_power=bull_p,
        bear_power=bear_p,
        strength=strength,
    )


# ── ATR (Average True Range) ──────────────────────────────────────────────────

def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float:
    """
    ATR ค่าสุดท้าย — วัดความผันผวนเฉลี่ย
    True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
    ยิ่งสูง = ราคาวิ่งแรง, ยิ่งต่ำ = ตลาดนิ่ง
    """
    if len(closes) < period + 1:
        return highs[-1] - lows[-1]

    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)

    # Wilder smoothing (เหมือน EMA แต่ k=1/period)
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period

    return atr_val


# ── Bollinger Bands ───────────────────────────────────────────────────────────

from dataclasses import dataclass as _dc
import math as _math


@_dc
class BollingerBands:
    upper: float
    middle: float   # SMA
    lower: float
    bandwidth: float   # (upper - lower) / middle — ยิ่งกว้าง = volatile มาก


def bollinger_bands(
    closes: Sequence[float],
    period: int = 20,
    num_std: float = 2.0,
) -> BollingerBands:
    """
    Bollinger Bands มาตรฐาน (SMA ± k×σ)
    ใช้ประกอบการกำหนดกรอบ grid:
      upper band = แนวต้านทางสถิติ
      lower band = แนวรับทางสถิติ
    """
    if len(closes) < period:
        mid = closes[-1]
        return BollingerBands(mid * 1.05, mid, mid * 0.95, 0.10)

    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std = _math.sqrt(variance)

    upper = mid + num_std * std
    lower = mid - num_std * std
    bw = (upper - lower) / mid if mid > 0 else 0

    return BollingerBands(upper=upper, middle=mid, lower=lower, bandwidth=bw)


# ── Lookback High / Low ───────────────────────────────────────────────────────

def lookback_high_low(
    highs: Sequence[float],
    lows: Sequence[float],
    lookback: int = 96,   # 96 × 15m = 24h
) -> tuple[float, float]:
    """ราคาสูงสุด/ต่ำสุดในช่วง lookback bars ล่าสุด."""
    n = min(lookback, len(highs))
    return max(highs[-n:]), min(lows[-n:])
