"""
Dynamic Range Manager
=====================
คำนวณและดูแลกรอบราคา (UPPER/LOWER) ของ Grid แบบ Semi-Auto

ปรัชญาการออกแบบ (ตามที่วิเคราะห์):
  - ไม่ Auto-Adjust ระหว่าง session (เสี่ยงไล่ราคา / ติดดอย)
  - ATR-Based Initialization: คำนวณกรอบให้ตอนเริ่มบอทเท่านั้น
  - แจ้งเตือน (Telegram + log) เมื่อราคาใกล้ขอบกรอบ
  - Auto-Stop: หยุดบอทถ้าราคาหลุดกรอบเกิน X%

กลยุทธ์หา Upper/Lower (เลือกได้ใน config):
  "atr"      — current_price ± (ATR × multiplier)   ← แนะนำ
  "bollinger" — Bollinger Bands ± buffer
  "lookback" — High/Low ในรอบ N candle ± buffer
  "manual"   — ใช้ค่าที่ตั้งใน .env โดยตรง
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from indicators import (
    CandleData,
    atr as calc_atr,
    bollinger_bands,
    lookback_high_low,
)

logger = logging.getLogger(__name__)


class RangeStrategy(str, Enum):
    ATR = "atr"
    BOLLINGER = "bollinger"
    LOOKBACK = "lookback"
    MANUAL = "manual"


@dataclass
class GridRange:
    upper: float
    lower: float
    strategy_used: str
    atr_value: float = 0.0
    note: str = ""

    @property
    def mid(self) -> float:
        return (self.upper + self.lower) / 2

    @property
    def width(self) -> float:
        return self.upper - self.lower

    def summary(self) -> str:
        return (
            f"Range [{self.strategy_used}]: "
            f"${self.lower:.4f} – ${self.upper:.4f}  "
            f"(กว้าง ${self.width:.4f}"
            + (f"  ATR={self.atr_value:.4f}" if self.atr_value else "")
            + ")"
        )


@dataclass
class BoundaryStatus:
    near_upper: bool        # ราคาใกล้ขอบบน (< PRICE_ALERT_PCT จากขอบ)
    near_lower: bool        # ราคาใกล้ขอบล่าง
    breached_upper: bool    # ราคาหลุดเกินขอบบน > AUTO_STOP_PCT
    breached_lower: bool    # ราคาหลุดเกินขอบล่าง
    upper_dist_pct: float   # % ห่างจากขอบบน (ลบ = หลุดไปแล้ว)
    lower_dist_pct: float   # % ห่างจากขอบล่าง

    @property
    def should_stop(self) -> bool:
        return self.breached_upper or self.breached_lower

    @property
    def should_alert(self) -> bool:
        return self.near_upper or self.near_lower


def calculate_range(
    candles: CandleData,
    current_price: float,
    strategy: str = "atr",
    atr_period: int = 14,
    atr_multiplier: float = 2.0,
    bb_period: int = 20,
    bb_std: float = 2.0,
    lookback_bars: int = 96,
    buffer_pct: float = 0.05,
) -> GridRange:
    """
    คำนวณ Upper/Lower สำหรับ Grid

    Args:
        candles: ข้อมูล OHLCV
        current_price: ราคาปัจจุบัน
        strategy: "atr" | "bollinger" | "lookback"
        atr_multiplier: กว้างแค่ไหน เช่น 2.0 = ±2 ATR (แนะนำ 1.5–3.0)
        buffer_pct: บวกเผื่อเพิ่ม % อีก (สำหรับ lookback)
    """
    if strategy == RangeStrategy.ATR or strategy == "atr":
        atr_val = calc_atr(
            candles.highs, candles.lows, candles.closes, atr_period
        )
        upper = current_price + atr_val * atr_multiplier
        lower = current_price - atr_val * atr_multiplier
        note = f"±{atr_multiplier}×ATR({atr_period})"
        return GridRange(
            upper=round(upper, 6),
            lower=round(lower, 6),
            strategy_used="ATR",
            atr_value=round(atr_val, 6),
            note=note,
        )

    elif strategy == RangeStrategy.BOLLINGER or strategy == "bollinger":
        bb = bollinger_bands(candles.closes, bb_period, bb_std)
        upper = bb.upper * (1 + buffer_pct)
        lower = bb.lower * (1 - buffer_pct)
        note = f"BB({bb_period},{bb_std}σ) +{buffer_pct*100:.0f}% buffer"
        return GridRange(
            upper=round(upper, 6),
            lower=round(lower, 6),
            strategy_used="Bollinger",
            note=note,
        )

    elif strategy == RangeStrategy.LOOKBACK or strategy == "lookback":
        high, low = lookback_high_low(candles.highs, candles.lows, lookback_bars)
        upper = high * (1 + buffer_pct)
        lower = low * (1 - buffer_pct)
        note = f"Lookback {lookback_bars} bars ±{buffer_pct*100:.0f}%"
        return GridRange(
            upper=round(upper, 6),
            lower=round(lower, 6),
            strategy_used="Lookback",
            note=note,
        )

    else:
        raise ValueError(f"Unknown range strategy: {strategy}")


def check_boundary(
    current_price: float,
    grid_range: GridRange,
    alert_pct: float = 3.0,
    stop_pct: float = 5.0,
) -> BoundaryStatus:
    """
    ตรวจว่าราคาอยู่ใกล้ขอบหรือหลุดกรอบแล้วไหม

    Args:
        alert_pct: แจ้งเตือนเมื่อราคาใกล้ขอบกรอบภายใน % นี้
        stop_pct:  หยุดบอทเมื่อราคาหลุดกรอบออกไป % นี้
    """
    upper_dist_pct = (grid_range.upper - current_price) / grid_range.upper * 100
    lower_dist_pct = (current_price - grid_range.lower) / grid_range.lower * 100

    near_upper = 0 < upper_dist_pct < alert_pct
    near_lower = 0 < lower_dist_pct < alert_pct

    # หลุดกรอบ = ราคาเกินขอบออกไป stop_pct
    breached_upper = current_price > grid_range.upper * (1 + stop_pct / 100)
    breached_lower = current_price < grid_range.lower * (1 - stop_pct / 100)

    return BoundaryStatus(
        near_upper=near_upper,
        near_lower=near_lower,
        breached_upper=breached_upper,
        breached_lower=breached_lower,
        upper_dist_pct=upper_dist_pct,
        lower_dist_pct=lower_dist_pct,
    )
