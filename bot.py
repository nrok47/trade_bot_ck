"""
Binance Trend-Following Grid Bot
=================================
ดู TF 3m/5m/15m/30m → ตรวจ Bull/Bear trend → วาง grid ตามทิศทาง

  BULL → Long-only grid (BUY ใต้ราคา, SELL เหนือเป็น TP)
  BEAR → Short-only grid (SELL เหนือราคา, BUY ต่ำเป็น TP)
  NEUTRAL → หยุดรอ signal ชัดขึ้น

หยุดอัตโนมัติเมื่อกำไรถึงเป้า (DAILY_PROFIT_TARGET_PCT) หรือขาดทุน MAX_LOSS_USDT

Usage:
  cp .env.example .env   → กรอก API key + ตั้งค่า
  python bot.py          → รัน (DRY_RUN=true โดย default)
"""
from __future__ import annotations

import logging
import signal
import sys
import time

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich import box

from config import config
from binance_client import binance, FuturesInfo
from grid import GridEngine, GridDirection, OrderSide
from strategy import strategy
from indicators import TrendSignal

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("grid_bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("bot")
console = Console()

_running = True


def _handle_signal(sig, frame):
    global _running
    console.print("\n[yellow]กำลังหยุดบอท...[/yellow]")
    _running = False


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Signal → GridDirection ────────────────────────────────────────────────────

def _signal_to_direction(sig: TrendSignal) -> GridDirection:
    if config.GRID_MODE == "both":
        return GridDirection.BOTH
    if sig.is_bull:
        return GridDirection.LONG
    if sig.is_bear:
        return GridDirection.SHORT
    return GridDirection.BOTH  # NEUTRAL → symmetric (or could skip)


# ── Dashboard panels ──────────────────────────────────────────────────────────

def _trend_panel(sig: TrendSignal) -> Panel:
    dir_color = {"BULL": "green", "BEAR": "red", "NEUTRAL": "yellow"}
    color = dir_color.get(sig.direction, "white")
    bull_color = "green" if sig.bull_power > 0 else "red"
    bear_color = "red" if sig.bear_power < 0 else "green"
    lines = [
        f"Signal   : [bold {color}]{sig.label()}[/bold {color}]   "
        f"Strength: {'█' * int(sig.strength * 5)}{'░' * (5 - int(sig.strength * 5))} {sig.strength*100:.0f}%",
        f"EMA{config.EMA_SHORT:<2}    : {sig.ema_short:.4f}",
        f"EMA{config.EMA_LONG:<2}    : {sig.ema_long:.4f}",
        f"RSI({config.RSI_PERIOD})  : {sig.rsi_value:.1f}"
        + (" [red](overbought)[/red]" if sig.rsi_value > 70
           else " [green](oversold)[/green]" if sig.rsi_value < 30 else ""),
        f"Bull Pwr : [{bull_color}]{sig.bull_power:+.4f}[/{bull_color}]   "
        f"Bear Pwr: [{bear_color}]{sig.bear_power:+.4f}[/{bear_color}]",
        f"TF       : [cyan]{config.TIMEFRAME}[/cyan]",
    ]
    border = dir_color.get(sig.direction, "white")
    return Panel("\n".join(lines), title=f"[bold]Trend Signal ({config.TIMEFRAME})[/bold]",
                 border_style=border)


def _header_panel(current_price: float, cycle: int, direction: GridDirection) -> Panel:
    mode_str = "[bold red]LIVE[/bold red]" if not config.DRY_RUN else "[bold yellow]DRY RUN[/bold yellow]"
    market_str = (
        f"[magenta]FUTURES {config.LEVERAGE}x ({config.MARGIN_TYPE})[/magenta]"
        if config.is_futures else "[cyan]SPOT[/cyan]"
    )
    dir_colors = {GridDirection.LONG: "green", GridDirection.SHORT: "red", GridDirection.BOTH: "yellow"}
    dir_labels = {GridDirection.LONG: "▲ LONG", GridDirection.SHORT: "▼ SHORT", GridDirection.BOTH: "◆ BOTH"}
    d_color = dir_colors.get(direction, "white")
    d_label = dir_labels.get(direction, str(direction))
    content = (
        f"Mode: {mode_str}   Market: {market_str}   "
        f"Grid: [bold {d_color}]{d_label}[/bold {d_color}]   "
        f"Symbol: [bold]{config.SYMBOL}[/bold]   "
        f"ราคา: [bold green]${current_price:,.4f}[/bold green]   "
        f"Cycle: {cycle}"
    )
    return Panel(content, title="[bold]Binance Trend Grid Bot[/bold]", border_style="blue")


def _stats_panel(engine: GridEngine, daily_target: float) -> Panel:
    s = engine.stats
    open_count = len(engine.open_orders())
    p_color = "green" if s.realized_profit_usdt >= 0 else "red"
    dp_color = "green" if s.daily_profit_usdt >= 0 else "red"
    pct = (s.daily_profit_usdt / config.total_margin_required * 100) if config.total_margin_required else 0
    bar_filled = min(int(pct / config.DAILY_PROFIT_TARGET_PCT * 10), 10)
    bar = f"{'█' * bar_filled}{'░' * (10 - bar_filled)}"
    lines = [
        f"Open orders   : {open_count}   Grid resets: {s.resets}",
        f"BUY / SELL    : {s.total_buys_filled} / {s.total_sells_filled}",
        f"Total P&L     : [bold {p_color}]${s.realized_profit_usdt:.4f} USDT[/bold {p_color}]",
        f"วันนี้ P&L    : [bold {dp_color}]${s.daily_profit_usdt:.4f} USDT[/bold {dp_color}]  ({pct:.1f}%)",
        f"เป้าวันนี้    : [{dp_color}]{bar}[/{dp_color}] ${daily_target:.2f}",
        f"Runtime       : {s.runtime_hours:.2f}h",
    ]
    return Panel("\n".join(lines), title="Stats", border_style="green")


def _futures_panel(fi: FuturesInfo) -> Panel:
    pnl_color = "green" if fi.unrealized_pnl >= 0 else "red"
    liq_str = (
        f"[bold red]${fi.liquidation_price:,.4f}[/bold red]"
        if fi.liquidation_price > 0 else "[dim]ไม่มี[/dim]"
    )
    fr_color = "red" if fi.funding_rate > 0 else "green"
    lines = [
        f"Leverage  : [magenta]{fi.leverage}x[/magenta]  ({fi.margin_type})",
        f"Position  : {fi.position_size:.6f}",
        f"Unreal PnL: [bold {pnl_color}]${fi.unrealized_pnl:.4f}[/bold {pnl_color}]",
        f"Liq price : {liq_str}",
        f"Funding   : [{fr_color}]{fi.funding_rate*100:.4f}%[/{fr_color}] / 8h",
    ]
    return Panel("\n".join(lines), title="[magenta]Futures[/magenta]", border_style="magenta")


def _grid_table(engine: GridEngine, current_price: float) -> Table:
    table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
    table.add_column("Lvl", justify="center", style="dim", width=4)
    table.add_column("ราคา", justify="right", width=14)
    table.add_column("ด้าน", justify="center", width=6)
    table.add_column("สถานะ", justify="center", width=10)
    table.add_column("qty", justify="right", width=12)

    price_to_order: dict[float, object] = {}
    for o in engine.orders.values():
        price_to_order[o.price] = o

    for i, price in enumerate(reversed(engine.levels)):
        level_idx = len(engine.levels) - 1 - i
        order = price_to_order.get(price)
        next_price = engine.levels[level_idx + 1] if level_idx + 1 < len(engine.levels) else price + 1
        in_band = price <= current_price < next_price
        price_str = f"[bold green]▶ {price:>10,.4f}[/bold green]" if in_band else f"{price:>10,.4f}"

        if order is None:
            table.add_row(str(level_idx), price_str, "–", "[dim]–[/dim]", "–")
        else:
            side_str = "[green]BUY[/green]" if order.side == OrderSide.BUY else "[red]SELL[/red]"
            if order.status == "FILLED":
                status_str = "[bold green]FILLED[/bold green]"
            elif order.status == "NEW":
                status_str = "[yellow]OPEN[/yellow]"
            else:
                status_str = f"[dim]{order.status}[/dim]"
            table.add_row(str(level_idx), price_str, side_str, status_str, f"{order.qty:.6f}")

    return table


def _recent_fills_panel(engine: GridEngine) -> Panel:
    filled = sorted(engine.filled_orders(), key=lambda o: o.filled_at or 0, reverse=True)[:6]
    if not filled:
        return Panel("[dim]ยังไม่มี order ที่เสร็จ[/dim]", title="Recent Fills", border_style="dim")
    lines = []
    for o in filled:
        side_str = "[green]BUY [/green]" if o.side == OrderSide.BUY else "[red]SELL[/red]"
        ts = time.strftime("%H:%M:%S", time.localtime(o.filled_at)) if o.filled_at else "–"
        lines.append(f"{ts}  {side_str} @ ${o.price:>8,.4f}  qty={o.qty:.6f}")
    return Panel("\n".join(lines), title="Recent Fills", border_style="magenta")


# ── Main ──────────────────────────────────────────────────────────────────────

def run() -> None:
    try:
        config.validate()
    except (ValueError, EnvironmentError) as exc:
        console.print(f"[bold red]Config error: {exc}[/bold red]")
        sys.exit(1)

    market_label = (
        f"FUTURES {config.LEVERAGE}x ({config.MARGIN_TYPE})"
        if config.is_futures else "SPOT"
    )

    if config.DRY_RUN:
        console.print(Panel(
            "[bold yellow]โหมด DRY RUN — ไม่ใช้เงินจริง[/bold yellow]\n\n"
            "บอทจะ [bold]แสดงผลเหมือนเทรดจริงทุกอย่าง[/bold] แต่ [bold red]ไม่ส่ง order ไป Binance[/bold red]\n"
            "ดูก่อนว่า trend signal ถูกไหม grid levels เหมาะสมไหม\n\n"
            "เมื่อมั่นใจ → เปลี่ยน [cyan]DRY_RUN=false[/cyan] ใน .env",
            title="ℹ DRY_RUN", border_style="yellow",
        ))

    console.print(Panel.fit(
        "[bold green]Binance Trend-Following Grid Bot[/bold green]\n\n"
        f"Symbol    : [cyan]{config.SYMBOL}[/cyan]   Market: [magenta]{market_label}[/magenta]\n"
        f"Timeframe : [cyan]{config.TIMEFRAME}[/cyan]   "
        f"EMA: {config.EMA_SHORT}/{config.EMA_LONG}   RSI: {config.RSI_PERIOD}\n"
        f"ช่วงราคา  : ${config.LOWER_PRICE:.4f} – ${config.UPPER_PRICE:.4f}  "
        f"({config.GRID_COUNT} grids, ห่าง ${config.grid_spacing:.4f})\n"
        f"Margin/Grid: ${config.USDT_PER_GRID:.2f}"
        + (f"  → Notional ${config.notional_per_grid:.2f} USDT" if config.is_futures else "") + "\n"
        f"เป้าวันนี้ : {config.DAILY_PROFIT_TARGET_PCT:.0f}%  = ${config.daily_profit_target_usdt:.2f} USDT\n"
        f"Mode      : {'[bold red]LIVE[/bold red]' if not config.DRY_RUN else '[bold yellow]DRY RUN[/bold yellow]'}",
        title="Config", border_style="blue",
    ))

    for w in config.capital_warnings():
        console.print(f"[{'red' if '⚠' in w else 'dim'}]{w}[/{'red' if '⚠' in w else 'dim'}]")

    if not config.DRY_RUN:
        console.print("\n[bold red]⚠  LIVE MODE — จะส่ง order จริงใน 5 วินาที (Ctrl+C ยกเลิก)[/bold red]")
        time.sleep(5)

    if config.is_futures:
        binance.setup_futures()

    current_price = binance.get_price()
    balance = binance.get_balance()
    console.print(
        f"\nราคา [cyan]{config.SYMBOL}[/cyan]: [bold green]${current_price:,.4f}[/bold green]   "
        f"Balance: [bold]${balance:,.2f} USDT[/bold]"
    )

    if not config.DRY_RUN and balance < config.total_margin_required:
        console.print(
            f"[bold red]⚠  Balance ${balance:.2f} ไม่พอ — ต้องการ ${config.total_margin_required:.2f} USDT[/bold red]"
        )
        sys.exit(1)

    if not (config.LOWER_PRICE < current_price < config.UPPER_PRICE):
        console.print(
            f"[bold red]⚠  ราคา ${current_price:.4f} อยู่นอก Grid range[/bold red]\n"
            "[yellow]ปรับ LOWER_PRICE / UPPER_PRICE ใน .env ให้ครอบคลุมราคาปัจจุบัน[/yellow]"
        )
        sys.exit(1)

    # ── Initial trend analysis ────────────────────────────────────────────────
    console.print("กำลังวิเคราะห์ trend...")
    sig = strategy.analyze(force=True)
    console.print(
        f"Trend: [bold]{'▲ BULL' if sig.is_bull else '▼ BEAR' if sig.is_bear else '◆ NEUTRAL'}[/bold]  "
        f"RSI={sig.rsi_value:.1f}  EMA{config.EMA_SHORT}={sig.ema_short:.4f}  EMA{config.EMA_LONG}={sig.ema_long:.4f}"
    )

    engine = GridEngine()
    current_direction = _signal_to_direction(sig)
    engine.initialize(current_price, current_direction)

    daily_target = config.daily_profit_target_usdt
    cycle = 0
    futures_info = None
    last_midnight = time.strftime("%Y-%m-%d")

    with Live(console=console, refresh_per_second=1, screen=False) as live:
        while _running:
            cycle += 1
            current_price = binance.get_price()

            # ── Reset daily P&L at midnight ───────────────────────────────────
            today = time.strftime("%Y-%m-%d")
            if today != last_midnight:
                engine.reset_daily_profit()
                last_midnight = today
                logger.info("วันใหม่ — รีเซ็ต daily P&L")

            # ── Daily profit target check ─────────────────────────────────────
            if engine.stats.daily_profit_usdt >= daily_target:
                console.print(
                    f"\n[bold green]🎯 ถึงเป้าวันนี้! ${engine.stats.daily_profit_usdt:.4f} USDT "
                    f"({engine.stats.daily_profit_usdt / config.total_margin_required * 100:.1f}%)[/bold green]"
                )
                console.print("[yellow]หยุดบอทสำหรับวันนี้ — รันใหม่พรุ่งนี้[/yellow]")
                break

            # ── Loss limit check ──────────────────────────────────────────────
            if engine.stats.realized_profit_usdt < -abs(config.MAX_LOSS_USDT):
                console.print(f"[bold red]หยุดบอท: ขาดทุนเกิน ${config.MAX_LOSS_USDT:.2f} USDT[/bold red]")
                break

            # ── Re-analyze trend ──────────────────────────────────────────────
            sig = strategy.analyze()
            new_direction = _signal_to_direction(sig)

            if engine._initialized and new_direction != current_direction:
                logger.info("Trend เปลี่ยน: %s → %s — รีเซ็ต grid", current_direction.value, new_direction.value)
                engine.reset()
                current_direction = new_direction
                engine.initialize(current_price, current_direction)

            # ── Poll order fills ──────────────────────────────────────────────
            newly_filled = engine.poll()
            for o in newly_filled:
                logger.info("Filled: %s @ %.4f", o.side.value, o.price)

            if config.is_futures:
                futures_info = binance.get_futures_info()

            # ── Build dashboard ───────────────────────────────────────────────
            layout = Layout()
            if config.is_futures:
                layout.split_column(
                    Layout(_header_panel(current_price, cycle, current_direction), size=3),
                    Layout(name="row1", size=9),
                    Layout(name="row2", size=9),
                    Layout(_grid_table(engine, current_price), name="grid"),
                    Layout(_recent_fills_panel(engine), size=10),
                )
                layout["row1"].split_row(
                    Layout(_trend_panel(sig)),
                    Layout(_stats_panel(engine, daily_target)),
                )
                layout["row2"].split_row(
                    Layout(_futures_panel(futures_info)),
                    Layout(Panel(
                        "[yellow]⚠  Futures เสี่ยงสูง\n"
                        f"Leverage {config.LEVERAGE}x — ระวัง Liquidation\n"
                        "Funding rate จ่ายทุก 8h\n"
                        "แนะนำ Leverage ≤ 5x สำหรับ grid bot[/yellow]",
                        title="คำเตือน", border_style="yellow"
                    )),
                )
            else:
                layout.split_column(
                    Layout(_header_panel(current_price, cycle, current_direction), size=3),
                    Layout(name="row1", size=9),
                    Layout(_grid_table(engine, current_price), name="grid"),
                    Layout(_recent_fills_panel(engine), size=10),
                )
                layout["row1"].split_row(
                    Layout(_trend_panel(sig)),
                    Layout(_stats_panel(engine, daily_target)),
                )

            live.update(layout)

            for _ in range(int(config.POLL_INTERVAL_SECONDS)):
                if not _running:
                    break
                time.sleep(1)

    # ── Shutdown ──────────────────────────────────────────────────────────────
    console.print("[yellow]ยกเลิก open orders ทั้งหมด...[/yellow]")
    binance.cancel_all_open_orders()

    s = engine.stats
    p_color = "green" if s.realized_profit_usdt >= 0 else "red"
    console.print(Panel(
        f"[bold]สรุปผล[/bold]\n\n"
        f"Market    : {market_label}\n"
        f"Timeframe : {config.TIMEFRAME}\n"
        f"Cycles    : {cycle}   Grid resets: {s.resets}\n"
        f"BUY/SELL  : {s.total_buys_filled} / {s.total_sells_filled}\n"
        f"วันนี้    : ${s.daily_profit_usdt:.4f} USDT\n"
        f"รวมทั้งหมด: [bold {p_color}]${s.realized_profit_usdt:.4f} USDT[/bold {p_color}]\n"
        f"Runtime   : {s.runtime_hours:.2f}h",
        title="จบการทำงาน", border_style="yellow",
    ))


if __name__ == "__main__":
    run()
