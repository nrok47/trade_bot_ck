"""
Trend-Following Strategy
========================
ดึง candle จาก Binance ตาม timeframe ที่เลือก
แล้วคำนวณ trend signal ทุก N วินาที

Timeframes ที่รองรับ: 3m, 5m, 15m, 30m
"""
from __future__ import annotations

import logging
import time

from binance_client import binance
from config import config
from indicators import CandleData, TrendSignal, analyze_trend

logger = logging.getLogger(__name__)

# แปลง timeframe string → วินาที (ใช้ตัดสินใจว่าต้อง refresh ไหม)
_TF_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
}


class TrendStrategy:
    def __init__(self) -> None:
        self._last_signal: TrendSignal | None = None
        self._last_check_time: float = 0.0
        self._candle_count: int = 0

    @property
    def last_signal(self) -> TrendSignal | None:
        return self._last_signal

    def _candles_stale(self) -> bool:
        tf_secs = _TF_SECONDS.get(config.TIMEFRAME, 300)
        elapsed = time.time() - self._last_check_time
        # Refresh ทุกครั้งที่ candle ใหม่ปิด (หรือทุก poll interval ถ้า TF สั้น)
        return elapsed >= min(tf_secs, config.POLL_INTERVAL_SECONDS * 6)

    def analyze(self, force: bool = False) -> TrendSignal:
        """
        คืน TrendSignal ปัจจุบัน
        จะ re-fetch candles จาก Binance เมื่อ candle ใหม่ปิดเท่านั้น
        """
        if not force and not self._candles_stale() and self._last_signal:
            return self._last_signal

        candles = binance.get_klines(config.SYMBOL, config.TIMEFRAME, limit=50)
        if candles is None or len(candles.closes) < 22:
            logger.warning("Not enough candle data — keeping last signal")
            return self._last_signal or TrendSignal("NEUTRAL", 0, 0, 50, 0, 0, 0)

        self._candle_count += 1
        signal = analyze_trend(
            candles,
            ema_short_period=config.EMA_SHORT,
            ema_long_period=config.EMA_LONG,
            rsi_period=config.RSI_PERIOD,
        )

        if self._last_signal and signal.direction != self._last_signal.direction:
            logger.info(
                "Trend changed: %s → %s  (RSI=%.1f  EMA%d=%.4f  EMA%d=%.4f)",
                self._last_signal.direction,
                signal.direction,
                signal.rsi_value,
                config.EMA_SHORT, signal.ema_short,
                config.EMA_LONG, signal.ema_long,
            )

        self._last_signal = signal
        self._last_check_time = time.time()
        return signal


strategy = TrendStrategy()
