"""
Trend-Following Strategy
========================
ดึง candle จาก Binance ตาม timeframe ที่เลือก
แล้วคำนวณ trend signal ทุก N วินาที

Whipsaw Protection:
  - TREND_CONFIRM_BARS: ต้องเห็น signal เดิมติดต่อกัน N candle ก่อนเปลี่ยน
  - MIN_RESET_INTERVAL_SECONDS: รอขั้นต่ำก่อน reset grid อีกครั้ง

Timeframes ที่รองรับ: 3m, 5m, 15m, 30m
"""
from __future__ import annotations

import logging
import time
from collections import deque

from binance_client import binance
from config import config
from indicators import CandleData, TrendSignal, analyze_trend, cdc_action_zone

logger = logging.getLogger(__name__)

_TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
}


class TrendStrategy:
    def __init__(self) -> None:
        self._last_signal: TrendSignal | None = None
        self._confirmed_signal: TrendSignal | None = None  # signal หลัง confirm แล้ว
        self._last_check_time: float = 0.0
        self._last_reset_time: float = 0.0
        self._candle_count: int = 0
        # เก็บ N bar ล่าสุดเพื่อนับ confirmation
        self._recent_directions: deque[str] = deque(maxlen=config.TREND_CONFIRM_BARS)

    @property
    def last_signal(self) -> TrendSignal | None:
        return self._last_signal

    @property
    def confirmed_signal(self) -> TrendSignal | None:
        return self._confirmed_signal

    def _candles_stale(self) -> bool:
        tf_secs = _TF_SECONDS.get(config.TIMEFRAME, 300)
        elapsed = time.time() - self._last_check_time
        return elapsed >= min(tf_secs, config.POLL_INTERVAL_SECONDS * 6)

    def can_reset_now(self) -> bool:
        """คืน True ถ้าผ่าน MIN_RESET_INTERVAL_SECONDS แล้วนับจาก reset ล่าสุด."""
        elapsed = time.time() - self._last_reset_time
        if elapsed < config.MIN_RESET_INTERVAL_SECONDS:
            remaining = config.MIN_RESET_INTERVAL_SECONDS - elapsed
            logger.debug("Whipsaw guard: รอ %.0f วินาทีก่อน reset ได้อีกครั้ง", remaining)
            return False
        return True

    def mark_reset(self) -> None:
        """เรียกหลัง grid reset จริงๆ เพื่อเริ่มนับ cooldown."""
        self._last_reset_time = time.time()

    def analyze(self, force: bool = False) -> TrendSignal:
        """
        คืน TrendSignal ที่ผ่าน confirmation แล้ว
        signal จะเปลี่ยนก็ต่อเมื่อ:
          1. เห็น direction เดิม TREND_CONFIRM_BARS candle ติดกัน
          2. ผ่าน MIN_RESET_INTERVAL_SECONDS แล้ว
        """
        if not force and not self._candles_stale() and self._confirmed_signal:
            return self._confirmed_signal

        candles = binance.get_klines(config.SYMBOL, config.TIMEFRAME, limit=60)
        if candles is None or len(candles.closes) < 22:
            logger.warning("Not enough candle data — keeping last signal")
            return self._confirmed_signal or TrendSignal("NEUTRAL", 0, 0, 50, 0, 0, 0)

        self._candle_count += 1
        if config.SIGNAL_MODE == "cdc":
            cdc = cdc_action_zone(candles, config.CDC_FAST, config.CDC_SLOW, config.CDC_STRICT)
            raw_signal = cdc.as_trend_signal()
            logger.debug(
                "CDC Zone %d (%s) → %s  FastEMA=%.4f  SlowEMA=%.4f",
                cdc.zone, cdc.name, cdc.direction, cdc.fast_ema, cdc.slow_ema,
            )
        else:
            raw_signal = analyze_trend(
                candles,
                ema_short_period=config.EMA_SHORT,
                ema_long_period=config.EMA_LONG,
                rsi_period=config.RSI_PERIOD,
                ema_min_gap_pct=config.EMA_MIN_GAP_PCT,
            )
        self._last_signal = raw_signal
        self._last_check_time = time.time()

        # เพิ่ม direction ล่าสุดเข้า queue
        self._recent_directions.append(raw_signal.direction)

        # ตรวจว่า N bars ล่าสุดทั้งหมดเป็น direction เดียวกันไหม
        if len(self._recent_directions) == config.TREND_CONFIRM_BARS:
            all_same = len(set(self._recent_directions)) == 1
            if all_same:
                new_direction = self._recent_directions[-1]
                old_direction = self._confirmed_signal.direction if self._confirmed_signal else None
                if new_direction != old_direction:
                    logger.info(
                        "Trend confirmed: %s → %s  (%d bars)  "
                        "RSI=%.1f  EMA%d=%.4f  EMA%d=%.4f",
                        old_direction, new_direction,
                        config.TREND_CONFIRM_BARS,
                        raw_signal.rsi_value,
                        config.EMA_SHORT, raw_signal.ema_short,
                        config.EMA_LONG, raw_signal.ema_long,
                    )
                self._confirmed_signal = raw_signal
            else:
                # ยังไม่ confirm — คงสัญญาณเก่าไว้
                if self._confirmed_signal:
                    logger.debug(
                        "Trend ยังไม่ confirm (%s) — คงสัญญาณเดิม (%s) ไว้",
                        "/".join(self._recent_directions),
                        self._confirmed_signal.direction,
                    )
        else:
            # ยังสะสม bars ไม่พอ
            self._confirmed_signal = raw_signal

        return self._confirmed_signal

    @property
    def raw_direction(self) -> str:
        """direction ดิบ (ยังไม่ confirm) — ใช้แสดงบน dashboard เท่านั้น."""
        return self._last_signal.direction if self._last_signal else "NEUTRAL"

    @property
    def confirm_progress(self) -> str:
        """แสดง progress bar ของ confirmation เช่น '██░' สำหรับ 2/3."""
        filled = len(self._recent_directions)
        total = config.TREND_CONFIRM_BARS
        same = len(set(self._recent_directions)) == 1 if self._recent_directions else False
        bar_char = "█" if same else "▒"
        return bar_char * filled + "░" * (total - filled)


strategy = TrendStrategy()
