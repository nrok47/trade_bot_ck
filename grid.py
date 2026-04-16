"""
Directional Grid Engine
=======================
รองรับ 3 โหมด (ตาม GRID_MODE และ trend signal):

  LONG-only  (BULL trend):
    - วาง BUY limit ใต้ราคาปัจจุบัน
    - เมื่อ BUY fill → วาง SELL สูงขึ้น 1 grid (take profit)
    - ไม่วาง SELL ทางลัด (ไม่ short)

  SHORT-only (BEAR trend):
    - วาง SELL limit เหนือราคาปัจจุบัน (short entry)
    - เมื่อ SELL fill → วาง BUY ต่ำลง 1 grid (take profit for short)
    - ไม่วาง BUY ทางลัด (ไม่ long)

  BOTH (NEUTRAL / GRID_MODE=both):
    - grid สองทางแบบเดิม

เมื่อ trend เปลี่ยน → reset() แล้ว initialize() ใหม่ด้วย direction ใหม่

ภาพรวม Grid (ตัวอย่าง BULL):

  ราคา
  ┌── SELL @ level 4  ← TP สำหรับ BUY ที่ level 3
  │   SELL @ level 3  ← TP สำหรับ BUY ที่ level 2
  │── ── ราคาปัจจุบัน
  │   BUY  @ level 2
  │   BUY  @ level 1
  └── BUY  @ level 0
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from config import config
from binance_client import binance

logger = logging.getLogger(__name__)


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class GridDirection(str, Enum):
    LONG = "LONG"     # BUY-only grid (bull trend)
    SHORT = "SHORT"   # SELL-only grid (bear trend)
    BOTH = "BOTH"     # symmetric grid (neutral / grid_mode=both)


@dataclass
class GridOrder:
    level_index: int
    price: float
    side: OrderSide
    order_id: str
    qty: float
    status: str = "NEW"
    filled_at: Optional[float] = None


@dataclass
class GridStats:
    total_buys_filled: int = 0
    total_sells_filled: int = 0
    realized_profit_usdt: float = 0.0
    total_usdt_spent: float = 0.0
    resets: int = 0
    start_time: float = field(default_factory=time.time)
    daily_start_time: float = field(default_factory=time.time)
    daily_profit_usdt: float = 0.0

    @property
    def runtime_hours(self) -> float:
        return (time.time() - self.start_time) / 3600

    @property
    def daily_runtime_hours(self) -> float:
        return (time.time() - self.daily_start_time) / 3600


class GridEngine:
    def __init__(self) -> None:
        self.levels: list[float] = self._compute_levels()
        self.orders: dict[str, GridOrder] = {}
        self.stats = GridStats()
        self.direction: GridDirection = GridDirection.BOTH
        self._initialized = False

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _compute_levels(self) -> list[float]:
        step = (config.UPPER_PRICE - config.LOWER_PRICE) / config.GRID_COUNT
        return [
            round(config.LOWER_PRICE + i * step, 6)
            for i in range(config.GRID_COUNT + 1)
        ]

    def initialize(self, current_price: float, direction: GridDirection) -> None:
        self.direction = direction
        logger.info(
            "Grid init: direction=%s  %d levels  %.4f–%.4f  spacing=%.4f",
            direction.value, len(self.levels),
            config.LOWER_PRICE, config.UPPER_PRICE, config.grid_spacing,
        )

        for i, price in enumerate(self.levels):
            if direction == GridDirection.LONG:
                # Only BUY orders below current price
                if price < current_price:
                    self._place_buy(i, price)

            elif direction == GridDirection.SHORT:
                # Only SELL (short) orders above current price
                if price > current_price:
                    qty = config.notional_per_grid / price
                    self._place_sell_at_level(i, price, qty)

            else:  # BOTH
                if price < current_price:
                    self._place_buy(i, price)
                elif price > current_price:
                    qty = config.notional_per_grid / price
                    self._place_sell_at_level(i, price, qty)

        self._initialized = True
        buys = sum(1 for o in self.orders.values() if o.side == OrderSide.BUY)
        sells = sum(1 for o in self.orders.values() if o.side == OrderSide.SELL)
        logger.info("Grid ready: %d BUY  %d SELL  direction=%s", buys, sells, direction.value)

    def reset(self) -> None:
        """
        ยกเลิก order ทั้งหมด + ปิด open position (Futures) ก่อนล้าง state
        เรียกเมื่อ trend เปลี่ยน เพื่อป้องกัน position ค้างในทิศตรงข้าม
        """
        logger.info("Grid reset (trend change)  resets=%d", self.stats.resets + 1)

        # 1) ยกเลิก pending orders ก่อน
        for go in list(self.orders.values()):
            if go.status == "NEW":
                binance.cancel_order(go.order_id)

        # 2) ปิด open position ทันที (Futures เท่านั้น)
        #    ถ้าไม่ทำ: Long ค้างอยู่ แต่ trend เปลี่ยนเป็น Bear → ขาดทุนต่อ
        if config.is_futures:
            binance.close_all_positions()

        self.orders.clear()
        self.stats.resets += 1
        self._initialized = False

    # ── Internal order helpers ────────────────────────────────────────────────

    def _has_enough_margin(self) -> bool:
        """เช็ค margin ก่อนเปิดไม้ใหม่ — ป้องกันกรณีทุนตึง."""
        available = binance.get_available_margin()
        required = config.USDT_PER_GRID * 1.05  # บวก 5% buffer
        if available < required:
            logger.warning(
                "Margin ไม่พอ: available=$%.4f  required=$%.4f — ข้ามไม้นี้",
                available, required,
            )
            return False
        return True

    def _place_buy(self, level_index: int, price: float) -> Optional[GridOrder]:
        if not self._has_enough_margin():
            return None
        resp = binance.place_limit_buy(price, config.USDT_PER_GRID)
        if not resp:
            return None
        qty = config.notional_per_grid / price
        go = GridOrder(
            level_index=level_index,
            price=price,
            side=OrderSide.BUY,
            order_id=str(resp["orderId"]),
            qty=float(resp.get("origQty", qty)),
        )
        self.orders[go.order_id] = go
        self.stats.total_usdt_spent += config.USDT_PER_GRID
        return go

    def _place_sell_at_level(
        self, level_index: int, price: float, qty: float
    ) -> Optional[GridOrder]:
        resp = binance.place_limit_sell(price, qty)
        if not resp:
            return None
        go = GridOrder(
            level_index=level_index,
            price=price,
            side=OrderSide.SELL,
            order_id=str(resp["orderId"]),
            qty=float(resp.get("origQty", qty)),
        )
        self.orders[go.order_id] = go
        return go

    # ── Poll & react ──────────────────────────────────────────────────────────

    def poll(self) -> list[GridOrder]:
        """ตรวจ order fills แล้ว react ทันที. คืน list ของ order ที่เพิ่ง fill."""
        newly_filled: list[GridOrder] = []

        for go in list(self.orders.values()):
            if go.status != "NEW":
                continue

            if go.order_id.startswith("DRY_"):
                # จำลอง fill ตามราคาปัจจุบัน
                current_price = binance.get_price()
                if go.side == OrderSide.BUY and current_price <= go.price:
                    go.status = "FILLED"
                    go.filled_at = time.time()
                    newly_filled.append(go)
                elif go.side == OrderSide.SELL and current_price >= go.price:
                    go.status = "FILLED"
                    go.filled_at = time.time()
                    newly_filled.append(go)
            else:
                status = binance.get_order_status(go.order_id)
                if status == "FILLED":
                    go.status = "FILLED"
                    go.filled_at = time.time()
                    newly_filled.append(go)
                elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                    go.status = status

        for go in newly_filled:
            self._on_fill(go)

        return newly_filled

    def _on_fill(self, go: GridOrder) -> None:
        if go.side == OrderSide.BUY:
            self.stats.total_buys_filled += 1
            sell_idx = go.level_index + 1
            if sell_idx < len(self.levels):
                sell_price = self.levels[sell_idx]
                profit = (sell_price - go.price) * go.qty
                logger.info(
                    "BUY @ %.4f filled → SELL @ %.4f  profit≈%.4f USDT",
                    go.price, sell_price, profit,
                )
                self._place_sell_at_level(sell_idx, sell_price, go.qty)
            else:
                logger.warning("BUY filled at top level — no SELL level above")

        else:  # SELL filled
            self.stats.total_sells_filled += 1
            buy_idx = go.level_index - 1

            if self.direction == GridDirection.SHORT:
                # Short closed → profit = sell_price - buy_price
                if buy_idx >= 0:
                    buy_price = self.levels[buy_idx]
                    profit = (go.price - buy_price) * go.qty
                    self.stats.realized_profit_usdt += profit
                    self.stats.daily_profit_usdt += profit
                    logger.info(
                        "SHORT SELL @ %.4f filled → BUY back @ %.4f  profit=%.4f USDT  "
                        "total=%.4f",
                        go.price, buy_price, profit, self.stats.realized_profit_usdt,
                    )
                    # Re-place SELL entry one level up (if available)
                    resell_idx = go.level_index + 1
                    if resell_idx < len(self.levels):
                        resell_price = self.levels[resell_idx]
                        qty = config.notional_per_grid / resell_price
                        self._place_sell_at_level(resell_idx, resell_price, qty)
                    # Place BUY to close short at lower level
                    self._place_buy(buy_idx, buy_price)

            else:  # LONG or BOTH
                if buy_idx >= 0:
                    buy_price = self.levels[buy_idx]
                    profit = (go.price - buy_price) * go.qty
                    self.stats.realized_profit_usdt += profit
                    self.stats.daily_profit_usdt += profit
                    logger.info(
                        "SELL @ %.4f filled  profit=%.4f USDT  total=%.4f",
                        go.price, profit, self.stats.realized_profit_usdt,
                    )
                    # Re-place BUY at the lower level
                    self._place_buy(buy_idx, buy_price)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def open_orders(self) -> list[GridOrder]:
        return [o for o in self.orders.values() if o.status == "NEW"]

    def filled_orders(self) -> list[GridOrder]:
        return [o for o in self.orders.values() if o.status == "FILLED"]

    def reset_daily_profit(self) -> None:
        self.stats.daily_profit_usdt = 0.0
        self.stats.daily_start_time = time.time()
