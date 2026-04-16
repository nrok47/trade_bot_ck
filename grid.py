"""
Grid state machine
==================
ภาพรวม Grid Trading:

  ราคา (USDT)
  ┌──────────────────────── UPPER $100,000
  │  SELL @ 99,000  ←─ ถ้าราคาขึ้นมาถึง ขาย
  │  SELL @ 98,000
  │  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ราคาปัจจุบัน $97,000
  │  BUY  @ 96,000  ←─ ถ้าราคาลงมาถึง ซื้อ
  │  BUY  @ 95,000
  └──────────────────────── LOWER $80,000

กลไก:
  1. แบ่งช่วงราคาเป็น N levels (grid lines)
  2. วาง limit BUY ทุก level ใต้ราคาปัจจุบัน
  3. วาง limit SELL ทุก level เหนือราคาปัจจุบัน
  4. เมื่อ BUY เต็ม → วาง SELL สูงขึ้น 1 grid ทันที
  5. เมื่อ SELL เต็ม → วาง BUY ต่ำลง 1 grid ทันที
  6. กำไรต่อรอบ ≈ grid_spacing × qty
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


@dataclass
class GridOrder:
    level_index: int          # 0 = lowest grid level
    price: float
    side: OrderSide
    order_id: str
    qty: float
    status: str = "NEW"       # NEW | FILLED | CANCELED
    filled_at: Optional[float] = None   # unix timestamp


@dataclass
class GridStats:
    total_buys_filled: int = 0
    total_sells_filled: int = 0
    realized_profit_usdt: float = 0.0
    total_usdt_spent: float = 0.0
    start_time: float = field(default_factory=time.time)

    @property
    def runtime_hours(self) -> float:
        return (time.time() - self.start_time) / 3600


class GridEngine:
    """
    Manages the full lifecycle of a grid:
    - Computes grid levels from config
    - Places initial orders
    - Polls order status and reacts to fills
    """

    def __init__(self) -> None:
        self.levels: list[float] = self._compute_levels()
        self.orders: dict[str, GridOrder] = {}   # order_id → GridOrder
        self.stats = GridStats()
        self._initialized = False

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _compute_levels(self) -> list[float]:
        """Return GRID_COUNT+1 evenly-spaced price levels."""
        step = (config.UPPER_PRICE - config.LOWER_PRICE) / config.GRID_COUNT
        levels = [
            round(config.LOWER_PRICE + i * step, 2)
            for i in range(config.GRID_COUNT + 1)
        ]
        return levels

    def initialize(self, current_price: float) -> None:
        """
        Place initial orders:
        - BUY  at every level strictly below current_price
        - SELL at every level strictly above current_price
        """
        logger.info(
            "Initializing grid: %d levels  %.2f–%.2f  spacing=%.2f",
            len(self.levels), config.LOWER_PRICE, config.UPPER_PRICE, config.grid_spacing,
        )

        for i, price in enumerate(self.levels):
            if price < current_price:
                self._place_buy(i, price)
            elif price > current_price:
                self._place_sell_at_level(i, price, qty=config.USDT_PER_GRID / price)

        self._initialized = True
        logger.info(
            "Grid initialized: %d BUY orders, %d SELL orders",
            sum(1 for o in self.orders.values() if o.side == OrderSide.BUY),
            sum(1 for o in self.orders.values() if o.side == OrderSide.SELL),
        )

    # ── Internal order helpers ────────────────────────────────────────────────

    def _place_buy(self, level_index: int, price: float) -> Optional[GridOrder]:
        order_resp = binance.place_limit_buy(price, config.USDT_PER_GRID)
        if not order_resp:
            return None
        qty = config.USDT_PER_GRID / price
        go = GridOrder(
            level_index=level_index,
            price=price,
            side=OrderSide.BUY,
            order_id=str(order_resp["orderId"]),
            qty=float(order_resp.get("origQty", qty)),
        )
        self.orders[go.order_id] = go
        self.stats.total_usdt_spent += config.USDT_PER_GRID
        return go

    def _place_sell_at_level(
        self, level_index: int, price: float, qty: float
    ) -> Optional[GridOrder]:
        order_resp = binance.place_limit_sell(price, qty)
        if not order_resp:
            return None
        go = GridOrder(
            level_index=level_index,
            price=price,
            side=OrderSide.SELL,
            order_id=str(order_resp["orderId"]),
            qty=float(order_resp.get("origQty", qty)),
        )
        self.orders[go.order_id] = go
        return go

    # ── Poll & react ──────────────────────────────────────────────────────────

    def poll(self) -> list[GridOrder]:
        """
        Check status of all open orders.
        React to fills: place the counterpart order one grid away.
        Returns list of newly-filled orders.
        """
        newly_filled: list[GridOrder] = []

        for order_id, go in list(self.orders.items()):
            if go.status != "NEW":
                continue

            if go.order_id.startswith("DRY_"):
                # In DRY_RUN we simulate fills based on current price
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
                status = binance.get_order_status(order_id)
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
        """React to a filled order by placing the opposite leg."""
        if go.side == OrderSide.BUY:
            self.stats.total_buys_filled += 1
            # Place SELL one grid above
            sell_level = go.level_index + 1
            if sell_level < len(self.levels):
                sell_price = self.levels[sell_level]
                profit = (sell_price - go.price) * go.qty
                logger.info(
                    "BUY filled @ %.2f → placing SELL @ %.2f  expected profit=%.4f USDT",
                    go.price, sell_price, profit,
                )
                self._place_sell_at_level(sell_level, sell_price, go.qty)
            else:
                logger.warning("BUY filled at top level — no SELL level above")

        else:  # SELL filled
            self.stats.total_sells_filled += 1
            # Calculate realized profit from this sell cycle
            buy_level = go.level_index - 1
            if buy_level >= 0:
                buy_price = self.levels[buy_level]
                profit = (go.price - buy_price) * go.qty
                self.stats.realized_profit_usdt += profit
                logger.info(
                    "SELL filled @ %.2f  realized profit=%.4f USDT  total=%.4f USDT",
                    go.price, profit, self.stats.realized_profit_usdt,
                )
                # Re-place BUY at the lower level
                self._place_buy(buy_level, buy_price)
            else:
                logger.warning("SELL filled at bottom level — no BUY level below")

    # ── Summary ───────────────────────────────────────────────────────────────

    def open_orders(self) -> list[GridOrder]:
        return [o for o in self.orders.values() if o.status == "NEW"]

    def filled_orders(self) -> list[GridOrder]:
        return [o for o in self.orders.values() if o.status == "FILLED"]
