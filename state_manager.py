"""
State Manager — บันทึก/โหลด bot state ลง bot_state.json
==========================================================
ป้องกันการสูญเสีย progress เมื่อบอทปิด/crash กลางคัน

บันทึก:
  - grid orders (level, price, side, status, qty)
  - stats (P&L, fills, resets)
  - grid range + direction
  - regrid count, cycle

โหลดกลับตอนเริ่มบอทใหม่ → ต่อจากเดิมได้เลย
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from grid import GridEngine, GridDirection
    from range_manager import GridRange

logger = logging.getLogger(__name__)

STATE_FILE = "bot_state.json"


def save_state(
    engine: "GridEngine",
    grid_range: "GridRange",
    direction: "GridDirection",
    regrid_count: int,
    cycle: int,
) -> None:
    """บันทึก state ปัจจุบันลงไฟล์ JSON."""
    try:
        data = {
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "cycle": cycle,
            "regrid_count": regrid_count,
            "direction": direction.value,
            "grid_range": {
                "upper": grid_range.upper,
                "lower": grid_range.lower,
                "strategy_used": grid_range.strategy_used,
                "atr_value": grid_range.atr_value,
            },
            "stats": {
                "daily_profit_usdt": engine.stats.daily_profit_usdt,
                "realized_profit_usdt": engine.stats.realized_profit_usdt,
                "total_usdt_spent": engine.stats.total_usdt_spent,
                "resets": engine.stats.resets,
                "total_buys_filled": engine.stats.total_buys_filled,
                "total_sells_filled": engine.stats.total_sells_filled,
                "start_time": engine.stats.start_time,
                "daily_start_time": engine.stats.daily_start_time,
            },
            "orders": [
                {
                    "level_index": o.level_index,
                    "price": o.price,
                    "side": o.side.value,
                    "order_id": o.order_id,
                    "qty": o.qty,
                    "status": o.status,
                    "filled_at": o.filled_at,
                }
                for o in engine.orders.values()
            ],
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        logger.warning("save_state failed: %s", exc)


def load_state() -> Optional[dict]:
    """โหลด state จากไฟล์ JSON คืน dict หรือ None ถ้าไม่มี/error."""
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        saved_at = data.get("saved_at", "unknown")
        logger.info("โหลด state เก่าจาก %s (บันทึกเมื่อ %s)", STATE_FILE, saved_at)
        return data
    except Exception as exc:
        logger.warning("load_state failed: %s — เริ่มใหม่", exc)
        return None


def restore_engine(engine: "GridEngine", state: dict) -> None:
    """เติม stats + orders กลับเข้า engine จาก state dict."""
    from grid import GridOrder, OrderSide

    s = state.get("stats", {})
    engine.stats.daily_profit_usdt  = s.get("daily_profit_usdt", 0.0)
    engine.stats.realized_profit_usdt = s.get("realized_profit_usdt", 0.0)
    engine.stats.total_usdt_spent   = s.get("total_usdt_spent", 0.0)
    engine.stats.resets             = s.get("resets", 0)
    engine.stats.total_buys_filled  = s.get("total_buys_filled", 0)
    engine.stats.total_sells_filled = s.get("total_sells_filled", 0)
    if s.get("start_time"):
        engine.stats.start_time = s["start_time"]
    if s.get("daily_start_time"):
        engine.stats.daily_start_time = s["daily_start_time"]

    for o in state.get("orders", []):
        if o["status"] not in ("NEW", "FILLED"):
            continue
        go = GridOrder(
            level_index=o["level_index"],
            price=o["price"],
            side=OrderSide(o["side"]),
            order_id=o["order_id"],
            qty=o["qty"],
            status=o["status"],
            filled_at=o.get("filled_at"),
        )
        engine.orders[go.order_id] = go

    engine._initialized = True
    open_count  = sum(1 for o in engine.orders.values() if o.status == "NEW")
    filled_count = sum(1 for o in engine.orders.values() if o.status == "FILLED")
    logger.info(
        "State restored: %d open orders, %d filled, P&L=%.4f USDT",
        open_count, filled_count, engine.stats.realized_profit_usdt,
    )


def clear_state() -> None:
    """ลบไฟล์ state (เรียกตอนบอทหยุดปกติ)."""
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
        logger.info("State file cleared")
