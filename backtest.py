"""
Backtest — ทดสอบ strategy กับข้อมูลย้อนหลัง
=============================================
ดึง OHLCV จาก Binance แล้วจำลอง grid engine ทีละ candle

ใช้งาน:
  python backtest.py                         # ใช้ค่าจาก .env
  python backtest.py --days 7                # ย้อนหลัง 7 วัน
  python backtest.py --days 30 --tf 15m      # 30 วัน TF 15m
  python backtest.py --symbol DOGEUSDT       # เปลี่ยนเหรียญ

ผลลัพธ์:
  - จำนวน fills, profit, max drawdown
  - กราฟ P&L ใน terminal (ASCII)
  - บันทึกผลลง backtest_result.json
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from typing import Optional

from binance.client import Client

from config import config
from indicators import (
    CandleData, TrendSignal, analyze_trend, ema, rsi, lookback_high_low,
)


# ── Data fetch ────────────────────────────────────────────────────────────────

def fetch_klines(symbol: str, interval: str, days: int) -> CandleData:
    """ดึง OHLCV ย้อนหลัง N วันจาก Binance Futures."""
    tf_minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30}
    mins = tf_minutes.get(interval, 5)
    limit = min(days * 24 * 60 // mins, 1500)

    client = Client()
    raw = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    opens   = [float(k[1]) for k in raw]
    highs   = [float(k[2]) for k in raw]
    lows    = [float(k[3]) for k in raw]
    closes  = [float(k[4]) for k in raw]
    volumes = [float(k[5]) for k in raw]
    timestamps = [int(k[0]) for k in raw]
    return CandleData(opens, highs, lows, closes, volumes), timestamps


# ── Grid Simulator ────────────────────────────────────────────────────────────

@dataclass
class BTOrder:
    price: float
    side: str        # "BUY" | "SELL"
    qty: float
    level: int
    status: str = "OPEN"


@dataclass
class BTResult:
    total_fills: int = 0
    buy_fills: int = 0
    sell_fills: int = 0
    realized_profit: float = 0.0
    max_drawdown: float = 0.0
    peak_profit: float = 0.0
    resets: int = 0
    pnl_series: list[float] = field(default_factory=list)
    fill_log: list[dict] = field(default_factory=list)


def _calc_range(closes: list, highs: list, lows: list,
                lookback: int, buffer: float) -> tuple[float, float]:
    high, low = lookback_high_low(highs, lows, lookback)
    margin = (high - low) * buffer
    return high + margin, low - margin


def run_backtest(
    symbol: str,
    interval: str,
    days: int,
    grid_count: int,
    usdt_per_grid: float,
    leverage: int,
    ema_short: int,
    ema_long: int,
    rsi_period: int,
    ema_min_gap_pct: float,
    trend_confirm_bars: int,
    min_reset_interval: float,
    lookback_bars: int,
    buffer_pct: float,
    stop_loss_pct: float,
    grid_mode: str,
) -> BTResult:
    print(f"\n📊 Backtest: {symbol} | TF={interval} | {days}d | {grid_count} grids")
    print("กำลังดึงข้อมูลจาก Binance...")

    candles, timestamps = fetch_klines(symbol, interval, days)
    n = len(candles.closes)
    print(f"ข้อมูล {n} candles  ({time.strftime('%Y-%m-%d', time.localtime(timestamps[0]/1000))} → {time.strftime('%Y-%m-%d', time.localtime(timestamps[-1]/1000))})")

    result = BTResult()
    notional = usdt_per_grid * leverage
    orders: list[BTOrder] = []
    current_direction = "BOTH"
    last_reset_time = 0.0
    confirmed_dir_queue: list[str] = []
    confirmed_direction = "NEUTRAL"
    last_upper = last_lower = 0.0

    warmup = max(ema_long + 5, lookback_bars + 5, 30)

    for i in range(warmup, n):
        window = CandleData(
            candles.opens[:i+1],
            candles.highs[:i+1],
            candles.lows[:i+1],
            candles.closes[:i+1],
            candles.volumes[:i+1],
        )
        price = candles.closes[i]
        high_i = candles.highs[i]
        low_i = candles.lows[i]
        ts = timestamps[i] / 1000

        # ── Trend signal ──────────────────────────────────────────────────────
        sig = analyze_trend(window, ema_short, ema_long, rsi_period, ema_min_gap_pct)
        confirmed_dir_queue.append(sig.direction)
        if len(confirmed_dir_queue) > trend_confirm_bars:
            confirmed_dir_queue.pop(0)
        if (len(confirmed_dir_queue) == trend_confirm_bars
                and len(set(confirmed_dir_queue)) == 1):
            confirmed_direction = confirmed_dir_queue[-1]

        if grid_mode == "both":
            new_dir = "BOTH"
        elif confirmed_direction == "BULL":
            new_dir = "LONG"
        elif confirmed_direction == "BEAR":
            new_dir = "SHORT"
        else:
            new_dir = "BOTH"

        # ── Grid init / reset ─────────────────────────────────────────────────
        need_init = not orders
        need_reset = new_dir != current_direction and (ts - last_reset_time) >= min_reset_interval

        if need_init or need_reset:
            if need_reset:
                result.resets += 1
                last_reset_time = ts
            orders.clear()
            current_direction = new_dir

            upper, lower = _calc_range(
                window.closes, window.highs, window.lows, lookback_bars, buffer_pct
            )
            last_upper, last_lower = upper, lower
            step = (upper - lower) / grid_count
            levels = [round(lower + j * step, 6) for j in range(grid_count + 1)]

            for j, lvl in enumerate(levels):
                if new_dir in ("LONG", "BOTH") and lvl < price:
                    sl = round(lvl * (1 - stop_loss_pct / 100), 6) if stop_loss_pct > 0 else None
                    orders.append(BTOrder(price=lvl, side="BUY", qty=notional/lvl, level=j))
                elif new_dir in ("SHORT", "BOTH") and lvl > price:
                    orders.append(BTOrder(price=lvl, side="SELL", qty=notional/lvl, level=j))

        # ── Check fills ───────────────────────────────────────────────────────
        step = (last_upper - last_lower) / grid_count if (last_upper - last_lower) > 0 else 0.01
        levels = [round(last_lower + j * step, 6) for j in range(grid_count + 1)]

        for o in list(orders):
            if o.status != "OPEN":
                continue
            filled = False
            if o.side == "BUY" and low_i <= o.price:
                filled = True
            elif o.side == "SELL" and high_i >= o.price:
                filled = True

            if filled:
                o.status = "FILLED"
                result.total_fills += 1
                if o.side == "BUY":
                    result.buy_fills += 1
                    # Place SELL at next level
                    if o.level + 1 < len(levels):
                        sell_price = levels[o.level + 1]
                        orders.append(BTOrder(price=sell_price, side="SELL",
                                              qty=o.qty, level=o.level + 1))
                else:  # SELL
                    result.sell_fills += 1
                    buy_price = levels[o.level - 1] if o.level > 0 else o.price
                    profit = (o.price - buy_price) * o.qty
                    result.realized_profit += profit
                    result.fill_log.append({
                        "ts": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)),
                        "price": o.price,
                        "profit": round(profit, 4),
                        "total": round(result.realized_profit, 4),
                    })
                    # Re-place BUY
                    if current_direction in ("LONG", "BOTH"):
                        orders.append(BTOrder(price=buy_price, side="BUY",
                                              qty=o.qty, level=o.level - 1))

        # ── Track drawdown ────────────────────────────────────────────────────
        result.pnl_series.append(round(result.realized_profit, 4))
        if result.realized_profit > result.peak_profit:
            result.peak_profit = result.realized_profit
        dd = result.peak_profit - result.realized_profit
        if dd > result.max_drawdown:
            result.max_drawdown = dd

    return result


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(result: BTResult, capital: float) -> None:
    pnl_pct = result.realized_profit / capital * 100 if capital else 0
    dd_pct  = result.max_drawdown   / capital * 100 if capital else 0
    win_rate = (result.sell_fills / result.total_fills * 100) if result.total_fills else 0

    print("\n" + "="*55)
    print(f"{'BACKTEST RESULT':^55}")
    print("="*55)
    print(f"  Fills รวม    : {result.total_fills:>6}  (BUY={result.buy_fills} SELL={result.sell_fills})")
    print(f"  Resets       : {result.resets:>6}")
    print(f"  กำไรสุทธิ    : {'+'if result.realized_profit>=0 else ''}${result.realized_profit:.4f}  ({pnl_pct:+.2f}%)")
    print(f"  Max Drawdown : -${result.max_drawdown:.4f}  (-{dd_pct:.2f}%)")
    print(f"  Win rate     : {win_rate:.1f}%")

    # ASCII P&L chart
    if result.pnl_series:
        print("\n  P&L Chart:")
        _print_ascii_chart(result.pnl_series)

    # Last 10 fills
    if result.fill_log:
        print(f"\n  Last {min(10, len(result.fill_log))} fills:")
        for f in result.fill_log[-10:]:
            sign = "+" if f["profit"] >= 0 else ""
            print(f"    {f['ts']}  SELL @${f['price']:.4f}"
                  f"  {sign}${f['profit']:.4f}  total=${f['total']:.4f}")
    print("="*55)


def _print_ascii_chart(series: list[float], width: int = 50, height: int = 8) -> None:
    if len(series) < 2:
        return
    # downsample
    step = max(1, len(series) // width)
    pts = [series[i] for i in range(0, len(series), step)]
    mn, mx = min(pts), max(pts)
    rng = mx - mn if mx != mn else 1
    rows = []
    for row in range(height, -1, -1):
        threshold = mn + (row / height) * rng
        line = ""
        for v in pts:
            line += "█" if v >= threshold else " "
        label = f"${threshold:7.4f} |" if row % 2 == 0 else "         |"
        rows.append(f"  {label}{line}")
    for r in rows:
        print(r)
    print("          +" + "─" * len(pts))


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Grid Bot Backtest")
    parser.add_argument("--days",    type=int,   default=7,            help="จำนวนวันย้อนหลัง (default 7)")
    parser.add_argument("--tf",      type=str,   default=config.TIMEFRAME, help="Timeframe เช่น 5m, 15m")
    parser.add_argument("--symbol",  type=str,   default=config.SYMBOL,    help="เหรียญ เช่น XRPUSDT")
    parser.add_argument("--grids",   type=int,   default=config.GRID_COUNT)
    parser.add_argument("--usdt",    type=float, default=config.USDT_PER_GRID)
    parser.add_argument("--sl",      type=float, default=config.STOP_LOSS_PCT, help="Stop loss % (0=ปิด)")
    parser.add_argument("--save",    action="store_true", help="บันทึกผลลง backtest_result.json")
    args = parser.parse_args()

    capital = args.grids * args.usdt

    result = run_backtest(
        symbol=args.symbol,
        interval=args.tf,
        days=args.days,
        grid_count=args.grids,
        usdt_per_grid=args.usdt,
        leverage=config.LEVERAGE,
        ema_short=config.EMA_SHORT,
        ema_long=config.EMA_LONG,
        rsi_period=config.RSI_PERIOD,
        ema_min_gap_pct=config.EMA_MIN_GAP_PCT,
        trend_confirm_bars=config.TREND_CONFIRM_BARS,
        min_reset_interval=config.MIN_RESET_INTERVAL_SECONDS,
        lookback_bars=config.LOOKBACK_BARS,
        buffer_pct=config.BUFFER_PCT,
        stop_loss_pct=args.sl,
        grid_mode=config.GRID_MODE,
    )

    print_report(result, capital)

    if args.save:
        out = {
            "symbol": args.symbol, "tf": args.tf, "days": args.days,
            "capital": capital, "fills": result.total_fills,
            "profit": round(result.realized_profit, 4),
            "profit_pct": round(result.realized_profit / capital * 100, 2) if capital else 0,
            "max_drawdown": round(result.max_drawdown, 4),
            "resets": result.resets,
            "fill_log": result.fill_log,
        }
        with open("backtest_result.json", "w") as f:
            json.dump(out, f, indent=2)
        print("\n💾 บันทึกผลลง backtest_result.json")
