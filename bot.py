"""
Binance Grid Trading Bot
========================
รองรับทั้ง Spot และ Futures (USDT-M) — เลือกผ่าน MARKET ใน .env

กลยุทธ์ Grid Trading:
  - แบ่งช่วงราคาเป็น N ระดับ
  - วาง BUY ใต้ราคาปัจจุบัน, SELL เหนือราคาปัจจุบัน
  - BUY เต็ม → วาง SELL สูงขึ้น 1 grid (lock กำไร)
  - SELL เต็ม → วาง BUY ต่ำลง 1 grid (วนใหม่)

Usage:
  1. cp .env.example .env
  2. กรอก API key + ตั้งค่า MARKET=spot หรือ futures
  3. python bot.py   (DRY_RUN=true โดย default)
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
from grid import GridEngine, OrderSide

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

# ── Graceful shutdown ─────────────────────────────────────────────────────────
_running = True


def _handle_signal(sig, frame):
    global _running
    console.print("\n[yellow]กำลังหยุดบอท...[/yellow]")
    _running = False


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Dashboard builders ────────────────────────────────────────────────────────

def _header_panel(current_price: float, cycle: int) -> Panel:
    mode_str = "[bold red]LIVE[/bold red]" if not config.DRY_RUN else "[bold yellow]DRY RUN[/bold yellow]"
    market_str = (
        f"[magenta]FUTURES {config.LEVERAGE}x ({config.MARGIN_TYPE})[/magenta]"
        if config.is_futures else "[cyan]SPOT[/cyan]"
    )
    content = (
        f"Mode: {mode_str}   Market: {market_str}   "
        f"Symbol: [bold]{config.SYMBOL}[/bold]   "
        f"ราคา: [bold green]${current_price:,.2f}[/bold green]   "
        f"Cycle: {cycle}"
    )
    return Panel(content, title="[bold]Binance Grid Bot[/bold]", border_style="blue")


def _config_panel() -> Panel:
    lines = [
        f"ช่วงราคา   : ${config.LOWER_PRICE:,.0f} – ${config.UPPER_PRICE:,.0f}",
        f"Grid spacing: ${config.grid_spacing:,.2f}  ({config.GRID_COUNT} levels)",
        f"USDT/Grid   : ${config.USDT_PER_GRID:.2f}",
        f"ทุนรวม (est): ~${config.total_usdt_required:.2f} USDT",
    ]
    if config.is_futures:
        lines.append(f"Leverage    : [magenta]{config.LEVERAGE}x[/magenta]  → notional/grid ${config.USDT_PER_GRID * config.LEVERAGE:.2f}")
    return Panel("\n".join(lines), title="Config", border_style="dim")


def _stats_panel(engine: GridEngine) -> Panel:
    s = engine.stats
    open_count = len(engine.open_orders())
    profit_color = "green" if s.realized_profit_usdt >= 0 else "red"
    lines = [
        f"Open orders : {open_count}",
        f"BUY filled  : {s.total_buys_filled}",
        f"SELL filled : {s.total_sells_filled}",
        f"Realized P&L: [bold {profit_color}]${s.realized_profit_usdt:.4f} USDT[/bold {profit_color}]",
        f"Runtime     : {s.runtime_hours:.2f} ชั่วโมง",
    ]
    return Panel("\n".join(lines), title="Stats", border_style="green")


def _futures_panel(fi: FuturesInfo) -> Panel:
    pnl_color = "green" if fi.unrealized_pnl >= 0 else "red"
    liq_str = (
        f"[bold red]${fi.liquidation_price:,.2f}[/bold red]"
        if fi.liquidation_price > 0 else "[dim]ไม่มี[/dim]"
    )
    funding_color = "red" if fi.funding_rate > 0 else "green"
    lines = [
        f"Leverage      : [magenta]{fi.leverage}x[/magenta]  ({fi.margin_type})",
        f"Position size : {fi.position_size:.6f}",
        f"Unrealized P&L: [bold {pnl_color}]${fi.unrealized_pnl:.4f}[/bold {pnl_color}]",
        f"Liquidation   : {liq_str}",
        f"Funding rate  : [{funding_color}]{fi.funding_rate*100:.4f}%[/{funding_color}] / 8h",
    ]
    return Panel("\n".join(lines), title="[magenta]Futures Info[/magenta]", border_style="magenta")


def _grid_table(engine: GridEngine, current_price: float) -> Table:
    table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
    table.add_column("Level", justify="center", style="dim", width=6)
    table.add_column("ราคา (USDT)", justify="right", width=15)
    table.add_column("ด้าน", justify="center", width=6)
    table.add_column("สถานะ", justify="center", width=12)
    table.add_column("qty", justify="right", width=12)

    price_to_order: dict[float, object] = {}
    for o in engine.orders.values():
        price_to_order[o.price] = o

    for i, price in enumerate(reversed(engine.levels)):
        level_idx = len(engine.levels) - 1 - i
        order = price_to_order.get(price)

        next_price = engine.levels[level_idx + 1] if level_idx + 1 < len(engine.levels) else price + 1
        in_band = price <= current_price < next_price
        price_str = (
            f"[bold green]▶ {price:>12,.2f}[/bold green]"
            if in_band else f"{price:>12,.2f}"
        )

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
    filled = sorted(
        engine.filled_orders(),
        key=lambda o: o.filled_at or 0,
        reverse=True,
    )[:8]

    if not filled:
        return Panel("[dim]ยังไม่มี order ที่เสร็จ[/dim]", title="Recent Fills", border_style="dim")

    lines = []
    for o in filled:
        side_str = "[green]BUY [/green]" if o.side == OrderSide.BUY else "[red]SELL[/red]"
        ts = time.strftime("%H:%M:%S", time.localtime(o.filled_at)) if o.filled_at else "–"
        lines.append(f"{ts}  {side_str} @ ${o.price:>10,.2f}  qty={o.qty:.6f}")

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

    # ── DRY RUN explanation box ───────────────────────────────────────────────
    if config.DRY_RUN:
        console.print(
            Panel(
                "[bold yellow]โหมด DRY RUN (จำลอง) — ไม่ใช้เงินจริง[/bold yellow]\n\n"
                "บอทจะ [bold]แสดงผลเหมือนเทรดจริงทุกอย่าง[/bold] แต่ [bold red]ไม่ส่ง order ไป Binance[/bold red]\n"
                "ใช้ดูก่อนว่า grid levels ถูกต้อง ราคาอยู่ในช่วงไหม บอททำงานปกติไหม\n\n"
                "เมื่อมั่นใจแล้ว → เปลี่ยน [cyan]DRY_RUN=false[/cyan] ใน .env เพื่อเทรดจริง",
                title="ℹ DRY_RUN คืออะไร?",
                border_style="yellow",
            )
        )

    console.print(
        Panel.fit(
            "[bold green]Binance Grid Trading Bot[/bold green]\n\n"
            f"Symbol  : [cyan]{config.SYMBOL}[/cyan]   Market: [magenta]{market_label}[/magenta]\n"
            f"ช่วงราคา: ${config.LOWER_PRICE:,.0f} – ${config.UPPER_PRICE:,.0f}\n"
            f"Grid    : {config.GRID_COUNT} levels  (ห่างกัน ${config.grid_spacing:,.2f})\n"
            f"Margin/Grid: ${config.USDT_PER_GRID:.2f} USDT"
            + (f"  → Notional ${config.notional_per_grid:.2f} USDT ({config.LEVERAGE}x)" if config.is_futures else "") + "\n"
            f"ทุนรวม  : ${config.total_margin_required:.2f} USDT\n"
            f"Mode    : {'[bold red]LIVE TRADING[/bold red]' if not config.DRY_RUN else '[bold yellow]DRY RUN — จำลองเท่านั้น[/bold yellow]'}",
            title="เริ่มต้น",
            border_style="blue",
        )
    )

    # ── Capital warnings ──────────────────────────────────────────────────────
    for w in config.capital_warnings():
        color = "red" if w.startswith("⚠") else "dim"
        console.print(f"[{color}]{w}[/{color}]")

    if not config.DRY_RUN:
        console.print("\n[bold red]⚠  LIVE MODE — จะส่ง order จริงใน 5 วินาที (Ctrl+C เพื่อยกเลิก)[/bold red]")
        time.sleep(5)

    # Setup futures (set leverage + margin type)
    if config.is_futures:
        binance.setup_futures()

    current_price = binance.get_price()
    balance = binance.get_balance()
    console.print(
        f"\nราคาปัจจุบัน [cyan]{config.SYMBOL}[/cyan]: [bold green]${current_price:,.2f}[/bold green]   "
        f"Balance: [bold]${balance:,.2f} USDT[/bold]"
    )

    # ── Balance check ─────────────────────────────────────────────────────────
    if not config.DRY_RUN and balance < config.total_margin_required:
        console.print(
            f"[bold red]⚠  Balance ${balance:.2f} ไม่พอ — ต้องการอย่างน้อย ${config.total_margin_required:.2f} USDT[/bold red]"
        )
        sys.exit(1)

    if not (config.LOWER_PRICE < current_price < config.UPPER_PRICE):
        console.print(
            f"[bold red]⚠  ราคา ${current_price:,.2f} อยู่นอกช่วง Grid "
            f"(${config.LOWER_PRICE:,.0f}–${config.UPPER_PRICE:,.0f})[/bold red]\n"
            "[yellow]กรุณาปรับ LOWER_PRICE / UPPER_PRICE ใน .env[/yellow]"
        )
        sys.exit(1)

    engine = GridEngine()
    engine.initialize(current_price)

    cycle = 0
    futures_info = None

    with Live(console=console, refresh_per_second=1, screen=False) as live:
        while _running:
            cycle += 1
            current_price = binance.get_price()

            if engine.stats.realized_profit_usdt < -abs(config.MAX_LOSS_USDT):
                console.print(f"[bold red]หยุดบอท: ขาดทุนเกิน ${config.MAX_LOSS_USDT:.2f} USDT[/bold red]")
                break

            newly_filled = engine.poll()
            for o in newly_filled:
                logger.info("Filled: %s @ %.2f", o.side.value, o.price)

            if config.is_futures:
                futures_info = binance.get_futures_info()

            # Build layout
            layout = Layout()
            if config.is_futures:
                layout.split_column(
                    Layout(_header_panel(current_price, cycle), size=3),
                    Layout(name="middle", size=8),
                    Layout(name="middle2", size=8),
                    Layout(_grid_table(engine, current_price), name="grid"),
                    Layout(_recent_fills_panel(engine), size=12),
                )
                layout["middle"].split_row(
                    Layout(_config_panel()),
                    Layout(_stats_panel(engine)),
                )
                layout["middle2"].split_row(
                    Layout(_futures_panel(futures_info)),
                    Layout(Panel(
                        "[yellow]⚠  Futures มีความเสี่ยงสูง\n"
                        "ระวัง Liquidation และ Funding Rate\n"
                        "แนะนำ Leverage ≤ 3x สำหรับ Grid Bot[/yellow]",
                        title="คำเตือน", border_style="yellow"
                    )),
                )
            else:
                layout.split_column(
                    Layout(_header_panel(current_price, cycle), size=3),
                    Layout(name="middle", size=8),
                    Layout(_grid_table(engine, current_price), name="grid"),
                    Layout(_recent_fills_panel(engine), size=12),
                )
                layout["middle"].split_row(
                    Layout(_config_panel()),
                    Layout(_stats_panel(engine)),
                )

            live.update(layout)

            for _ in range(int(config.POLL_INTERVAL_SECONDS)):
                if not _running:
                    break
                time.sleep(1)

    console.print("[yellow]กำลังยกเลิก open orders ทั้งหมด...[/yellow]")
    binance.cancel_all_open_orders()

    s = engine.stats
    profit_color = "green" if s.realized_profit_usdt >= 0 else "red"
    console.print(
        Panel(
            f"[bold]สรุปผลการทำงาน[/bold]\n\n"
            f"Market       : {market_label}\n"
            f"Cycles       : {cycle}\n"
            f"BUY filled   : {s.total_buys_filled}\n"
            f"SELL filled  : {s.total_sells_filled}\n"
            f"Realized P&L : [bold {profit_color}]${s.realized_profit_usdt:.4f} USDT[/bold {profit_color}]\n"
            f"Runtime      : {s.runtime_hours:.2f} ชั่วโมง",
            title="จบการทำงาน",
            border_style="yellow",
        )
    )


if __name__ == "__main__":
    run()
