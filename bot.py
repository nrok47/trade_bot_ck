"""
Binance Grid Trading Bot
========================
กลยุทธ์ Grid Trading:
  - แบ่งช่วงราคาเป็น N ระดับ (grid lines)
  - วาง limit BUY ใต้ราคาปัจจุบัน, limit SELL เหนือราคาปัจจุบัน
  - เมื่อ BUY เต็ม → วาง SELL สูงขึ้น 1 grid (lock กำไร)
  - เมื่อ SELL เต็ม → วาง BUY ต่ำลง 1 grid (วนใหม่)
  - กำไรต่อรอบ ≈ grid_spacing × qty

Usage:
  1. cp .env.example .env  แล้วกรอก API key
  2. ตั้งค่าช่วงราคาใน .env ให้ตรงกับราคาตลาดปัจจุบัน
  3. python bot.py          (DRY_RUN=true โดย default)
  4. ตรวจสอบผลลัพธ์ก่อน แล้วค่อยตั้ง DRY_RUN=false
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
from rich.text import Text
from rich import box

from config import config
from binance_client import binance
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
    mode = "[bold red]LIVE[/bold red]" if not config.DRY_RUN else "[bold yellow]DRY RUN[/bold yellow]"
    content = (
        f"Mode: {mode}   Symbol: [cyan]{config.SYMBOL}[/cyan]   "
        f"ราคาปัจจุบัน: [bold green]${current_price:,.2f}[/bold green]   "
        f"Cycle: {cycle}"
    )
    return Panel(content, title="[bold]Binance Grid Bot[/bold]", border_style="blue")


def _config_panel() -> Panel:
    lines = [
        f"ช่วงราคา   : ${config.LOWER_PRICE:,.0f} – ${config.UPPER_PRICE:,.0f}",
        f"จำนวน Grid : {config.GRID_COUNT} levels  (ห่างกัน ${config.grid_spacing:,.2f})",
        f"USDT/Grid  : ${config.USDT_PER_GRID:.2f}",
        f"ทุนรวม     : ~${config.total_usdt_required:.2f} USDT",
    ]
    return Panel("\n".join(lines), title="Config", border_style="dim")


def _stats_panel(engine: GridEngine) -> Panel:
    s = engine.stats
    open_count = len(engine.open_orders())
    profit_color = "green" if s.realized_profit_usdt >= 0 else "red"
    lines = [
        f"Open orders   : {open_count}",
        f"BUY  filled   : {s.total_buys_filled}",
        f"SELL filled   : {s.total_sells_filled}",
        f"Realized P&L  : [bold {profit_color}]${s.realized_profit_usdt:.4f} USDT[/bold {profit_color}]",
        f"Runtime       : {s.runtime_hours:.2f} ชั่วโมง",
    ]
    return Panel("\n".join(lines), title="Stats", border_style="green")


def _grid_table(engine: GridEngine, current_price: float) -> Table:
    table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
    table.add_column("Level", justify="center", style="dim", width=6)
    table.add_column("ราคา (USDT)", justify="right", width=14)
    table.add_column("ด้าน", justify="center", width=6)
    table.add_column("สถานะ", justify="center", width=12)
    table.add_column("qty", justify="right", width=12)

    # Build a lookup: price → GridOrder (latest)
    price_to_order: dict[float, object] = {}
    for o in engine.orders.values():
        price_to_order[o.price] = o

    for i, price in enumerate(reversed(engine.levels)):
        level_idx = len(engine.levels) - 1 - i
        order = price_to_order.get(price)

        # Highlight current price band
        if price <= current_price < (engine.levels[level_idx + 1] if level_idx + 1 < len(engine.levels) else price + 1):
            price_str = f"[bold green]▶ {price:>12,.2f}[/bold green]"
        else:
            price_str = f"{price:>12,.2f}"

        if order is None:
            table.add_row(str(level_idx), price_str, "–", "[dim]ไม่มี order[/dim]", "–")
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
    # Validate config
    try:
        config.validate()
    except (ValueError, EnvironmentError) as exc:
        console.print(f"[bold red]Config error: {exc}[/bold red]")
        sys.exit(1)

    # Print startup banner
    console.print(
        Panel.fit(
            "[bold green]Binance Grid Trading Bot[/bold green]\n\n"
            f"Symbol  : [cyan]{config.SYMBOL}[/cyan]\n"
            f"ช่วงราคา: ${config.LOWER_PRICE:,.0f} – ${config.UPPER_PRICE:,.0f}\n"
            f"Grid    : {config.GRID_COUNT} levels  (ห่างกัน ${config.grid_spacing:,.2f})\n"
            f"ทุน/Grid: ${config.USDT_PER_GRID:.2f} USDT\n"
            f"Mode    : {'[bold red]LIVE TRADING[/bold red]' if not config.DRY_RUN else '[bold yellow]DRY RUN — ไม่ส่ง order จริง[/bold yellow]'}",
            title="เริ่มต้น",
            border_style="blue",
        )
    )

    if not config.DRY_RUN:
        console.print("[bold red]⚠  LIVE MODE — จะส่ง order จริงใน 5 วินาที (Ctrl+C เพื่อยกเลิก)[/bold red]")
        time.sleep(5)

    # Check current price fits within grid
    current_price = binance.get_price()
    console.print(f"ราคาปัจจุบัน [cyan]{config.SYMBOL}[/cyan]: [bold green]${current_price:,.2f}[/bold green]")

    if not (config.LOWER_PRICE < current_price < config.UPPER_PRICE):
        console.print(
            f"[bold red]⚠  ราคาปัจจุบัน ${current_price:,.2f} อยู่นอกช่วง Grid "
            f"(${config.LOWER_PRICE:,.0f}–${config.UPPER_PRICE:,.0f})[/bold red]\n"
            "[yellow]กรุณาปรับ LOWER_PRICE / UPPER_PRICE ใน .env ให้ครอบคลุมราคาปัจจุบัน[/yellow]"
        )
        sys.exit(1)

    # Initialize grid
    engine = GridEngine()
    engine.initialize(current_price)

    cycle = 0
    with Live(console=console, refresh_per_second=1, screen=False) as live:
        while _running:
            cycle += 1
            current_price = binance.get_price()

            # Safety: stop if loss exceeds limit
            if engine.stats.realized_profit_usdt < -abs(config.MAX_LOSS_USDT):
                console.print(
                    f"[bold red]หยุดบอท: ขาดทุนเกิน ${config.MAX_LOSS_USDT:.2f} USDT[/bold red]"
                )
                break

            # Poll order fills and react
            newly_filled = engine.poll()
            if newly_filled:
                for o in newly_filled:
                    logger.info("Filled: %s @ %.2f", o.side.value, o.price)

            # Build dashboard
            layout = Layout()
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

            # Sleep until next poll
            for _ in range(int(config.POLL_INTERVAL_SECONDS)):
                if not _running:
                    break
                time.sleep(1)

    # Shutdown: cancel all open orders
    console.print("[yellow]กำลังยกเลิก open orders ทั้งหมด...[/yellow]")
    binance.cancel_all_open_orders()

    # Final summary
    s = engine.stats
    profit_color = "green" if s.realized_profit_usdt >= 0 else "red"
    console.print(
        Panel(
            f"[bold]สรุปผลการทำงาน[/bold]\n\n"
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
