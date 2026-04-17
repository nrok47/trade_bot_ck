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

import argparse
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
from range_manager import calculate_range, check_boundary, GridRange
import notifier
import state_manager

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
    raw_dir = strategy.raw_direction
    raw_color = dir_color.get(raw_dir, "white")
    confirm_bar = strategy.confirm_progress
    lines = [
        f"Confirmed: [bold {color}]{sig.label()}[/bold {color}]   "
        f"Strength: {'█' * int(sig.strength * 5)}{'░' * (5 - int(sig.strength * 5))} {sig.strength*100:.0f}%",
        f"Raw bar  : [{raw_color}]{raw_dir}[/{raw_color}]   "
        f"Confirm: {confirm_bar} ({config.TREND_CONFIRM_BARS} bars needed)",
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


def _header_panel(current_price: float, cycle: int, direction: GridDirection,
                   session_start: float = 0.0, session_mode: bool = False) -> Panel:
    mode_str = "[bold red]LIVE[/bold red]" if not config.DRY_RUN else "[bold yellow]DRY RUN[/bold yellow]"
    market_str = (
        f"[magenta]FUTURES {config.LEVERAGE}x ({config.MARGIN_TYPE})[/magenta]"
        if config.is_futures else "[cyan]SPOT[/cyan]"
    )
    dir_colors = {GridDirection.LONG: "green", GridDirection.SHORT: "red", GridDirection.BOTH: "yellow"}
    dir_labels = {GridDirection.LONG: "▲ LONG", GridDirection.SHORT: "▼ SHORT", GridDirection.BOTH: "◆ BOTH"}
    d_color = dir_colors.get(direction, "white")
    d_label = dir_labels.get(direction, str(direction))
    extras = ""
    if config.TURBO_MODE:
        extras += "   [bold red]⚡TURBO[/bold red]"
    if session_mode:
        remaining = max(0.0, config.SESSION_DURATION_MINUTES - (time.time() - session_start) / 60)
        extras += f"   [cyan]Session: {remaining:.0f}m เหลือ[/cyan]"
    content = (
        f"Mode: {mode_str}   Market: {market_str}   "
        f"Grid: [bold {d_color}]{d_label}[/bold {d_color}]   "
        f"Symbol: [bold]{config.SYMBOL}[/bold]   "
        f"ราคา: [bold green]${current_price:,.4f}[/bold green]   "
        f"Cycle: {cycle}{extras}"
    )
    return Panel(content, title="[bold]Binance Trend Grid Bot[/bold]", border_style="blue")


def _stats_panel(engine: GridEngine, daily_target: float) -> Panel:
    s = engine.stats
    cap = config.total_margin_required
    open_count = len(engine.open_orders())

    # Daily P&L % (positive = profit, negative = loss)
    day_pct = (s.daily_profit_usdt / cap * 100) if cap else 0
    tot_pct = (s.realized_profit_usdt / cap * 100) if cap else 0

    # Profit bar (0 → target%)
    profit_filled = min(int(day_pct / config.DAILY_PROFIT_TARGET_PCT * 10), 10) if day_pct > 0 else 0
    profit_bar = f"{'█' * profit_filled}{'░' * (10 - profit_filled)}"

    # Loss bar (0 → max_loss%)
    loss_pct = abs(min(day_pct, 0))
    loss_filled = min(int(loss_pct / config.MAX_LOSS_PCT * 10), 10) if day_pct < 0 else 0
    loss_bar = f"{'█' * loss_filled}{'░' * (10 - loss_filled)}"

    day_color = "green" if day_pct >= 0 else "red"
    tot_color = "green" if tot_pct >= 0 else "red"
    sign = "+" if day_pct >= 0 else ""
    tsign = "+" if tot_pct >= 0 else ""

    lines = [
        f"Open orders  : {open_count}   Resets: {s.resets}",
        f"BUY / SELL   : {s.total_buys_filled} / {s.total_sells_filled}",
        f"วันนี้ P&L   : [bold {day_color}]{sign}{day_pct:.2f}%[/bold {day_color}]"
        f"  ([{day_color}]{sign}${s.daily_profit_usdt:.4f}[/{day_color}])",
        f"กำไร  [{day_color}]{profit_bar}[/{day_color}] {sign}{day_pct:.2f}% / +{config.DAILY_PROFIT_TARGET_PCT:.0f}%",
        f"ขาดทุน [red]{loss_bar}[/red] -{loss_pct:.2f}% / -{config.MAX_LOSS_PCT:.0f}%",
        f"Total P&L    : [bold {tot_color}]{tsign}{tot_pct:.2f}%[/bold {tot_color}]"
        f"  ([{tot_color}]{tsign}${s.realized_profit_usdt:.4f}[/{tot_color}])",
        f"Runtime      : {s.runtime_hours:.2f}h",
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


def _range_panel(grid_range: GridRange, current_price: float, alert_pct: float,
                  stop_pct: float, regrid_count: int = 0) -> Panel:
    status = check_boundary(current_price, grid_range, alert_pct, stop_pct)
    # clamp to 0–5 to prevent negative multiplication → bar overflow
    upper_bar = min(max(0, int((1 - status.upper_dist_pct / alert_pct) * 5)), 5) if status.upper_dist_pct >= 0 else 5
    lower_bar = min(max(0, int((1 - status.lower_dist_pct / alert_pct) * 5)), 5) if status.lower_dist_pct >= 0 else 5
    upper_color = "red" if status.breached_upper else ("yellow" if status.near_upper else "green")
    lower_color = "red" if status.breached_lower else ("yellow" if status.near_lower else "green")

    # Position bar: แสดงตำแหน่งราคาในกรอบ 0%=lower … 100%=upper
    width = grid_range.upper - grid_range.lower
    pos_pct = (current_price - grid_range.lower) / width * 100 if width > 0 else 50
    pos_pct = max(0.0, min(100.0, pos_pct))
    pos_filled = int(pos_pct / 10)
    pos_bar = "░" * pos_filled + "▓" + "░" * (9 - pos_filled)
    regrid_zone = int(config.REGRID_THRESHOLD * 10) if config.AUTO_REGRID else -1
    pos_color = "yellow" if config.AUTO_REGRID and (pos_filled >= (10 - regrid_zone) or pos_filled < regrid_zone) else "cyan"

    lines = [
        f"Strategy  : [cyan]{grid_range.strategy_used}[/cyan]"
        + (f"   ATR={grid_range.atr_value:.4f}" if grid_range.atr_value else "")
        + (f"   Re-grids: {regrid_count}" if regrid_count else ""),
        f"Upper     : [bold {upper_color}]${grid_range.upper:,.4f}[/bold {upper_color}]"
        f"  ห่าง {'█' * upper_bar}{'░' * (5 - upper_bar)} {status.upper_dist_pct:.2f}%",
        f"ตำแหน่ง  : [{pos_color}]{pos_bar}[/{pos_color}] {pos_pct:.0f}%"
        + (f"  [dim](re-grid zone <{regrid_zone*10}% / >{(10-regrid_zone)*10}%)[/dim]"
           if config.AUTO_REGRID else ""),
        f"Lower     : [bold {lower_color}]${grid_range.lower:,.4f}[/bold {lower_color}]"
        f"  ห่าง {'█' * lower_bar}{'░' * (5 - lower_bar)} {status.lower_dist_pct:.2f}%",
        f"แจ้งเตือน : <{alert_pct:.0f}% จากขอบ   หยุดบอท : >{stop_pct:.0f}% หลุดกรอบ",
    ]
    border = "red" if status.should_stop else ("yellow" if status.should_alert else "dim")
    title_suffix = " [bold red]⚠ หลุดกรอบ![/bold red]" if status.should_stop else (
        " [yellow]⚠ ใกล้ขอบ[/yellow]" if status.should_alert else ""
    )
    return Panel("\n".join(lines), title=f"[bold]Grid Range[/bold]{title_suffix}", border_style=border)


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


# ── Auto Re-grid helpers ──────────────────────────────────────────────────────

def _needs_regrid(current_price: float, grid_range: GridRange, threshold: float) -> tuple[bool, str]:
    """
    คืน (should_regrid, side) เมื่อราคาเข้าโซนขอบ top/bottom threshold%
    side = "upper" | "lower" | "none"
    """
    width = grid_range.upper - grid_range.lower
    if width <= 0:
        return False, "none"
    pos = (current_price - grid_range.lower) / width   # 0.0 = at lower, 1.0 = at upper
    if pos >= (1.0 - threshold):
        return True, "upper"
    if pos <= threshold:
        return True, "lower"
    return False, "none"


def _compute_new_range(current_price: float, old_range: GridRange) -> GridRange:
    """
    คำนวณกรอบใหม่ centered บนราคาปัจจุบัน
    - AUTO_RANGE=true  → ใช้ ATR/BB/Lookback คำนวณใหม่
    - AUTO_RANGE=false → เลื่อนกรอบเดิม (ความกว้างเท่าเดิม) มา center ที่ราคาใหม่
    """
    if config.AUTO_RANGE and config.RANGE_STRATEGY != "manual":
        try:
            candles = binance.get_klines(config.SYMBOL, config.TIMEFRAME, limit=200)
            return calculate_range(
                candles, current_price,
                strategy=config.RANGE_STRATEGY,
                atr_period=config.ATR_PERIOD,
                atr_multiplier=config.ATR_MULTIPLIER,
                bb_period=config.BB_PERIOD,
                bb_std=config.BB_STD,
                lookback_bars=config.LOOKBACK_BARS,
                buffer_pct=config.BUFFER_PCT,
            )
        except Exception as exc:
            logger.warning("Re-grid ATR calc failed: %s — shifting width instead", exc)

    # Fallback: shift same width to center on current price
    half = (old_range.upper - old_range.lower) / 2
    return GridRange(
        upper=round(current_price + half, 6),
        lower=round(current_price - half, 6),
        strategy_used=f"Shifted({old_range.strategy_used})",
        atr_value=old_range.atr_value,
        note="Same width, recentered",
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def run() -> None:
    session_start = time.time()
    session_mode = config.SESSION_DURATION_MINUTES > 0

    try:
        config.validate()
    except (ValueError, EnvironmentError) as exc:
        console.print(f"[bold red]Config error: {exc}[/bold red]")
        sys.exit(1)

    market_label = (
        f"FUTURES {config.LEVERAGE}x ({config.MARGIN_TYPE})"
        if config.is_futures else "SPOT"
    )

    if config.TURBO_MODE:
        console.print(Panel(
            f"[bold red]⚡ TURBO MODE[/bold red]\n\n"
            f"TF           : [bold red]{config.TIMEFRAME}[/bold red]  (สั้นที่สุด = ตัดสินใจถี่ขึ้น)\n"
            f"Signal       : CDC non-strict  (BULL=zones1-3 / BEAR=zones4-6 / ไม่มี NEUTRAL)\n"
            f"Confirm bars : 1  (react ทันทีทุก bar ไม่รอ confirm)\n"
            f"Min reset    : 60s  (reset ได้เร็วขึ้น)\n"
            f"Lookback     : {config.LOOKBACK_BARS} bars ≈ 24h\n\n"
            "[yellow]⚠  Turbo เพิ่มทั้งโอกาสกำไร และความเสี่ยง — resets จะถี่มากขึ้น "
            "ค่า fee สะสมได้เร็วกว่า[/yellow]",
            title="⚡ Turbo Mode", border_style="red",
        ))

    if session_mode:
        console.print(Panel(
            f"[bold cyan]Session Mode: {config.SESSION_DURATION_MINUTES} นาที[/bold cyan]\n\n"
            "บอทจะ [bold]เริ่มทันที ในโหมด BOTH[/bold] — เปิด grid สองทางเลย ไม่รอ trend signal\n"
            f"หยุดอัตโนมัติหลัง [cyan]{config.SESSION_DURATION_MINUTES}[/cyan] นาที พร้อมสรุปผล\n\n"
            "เหมาะสำหรับเทรดช่วงสั้น 20 นาที – 2 ชั่วโมง\n"
            "ใช้ --session N เพื่อกำหนดเวลา  หรือตั้ง SESSION_DURATION_MINUTES ใน .env",
            title="Session Mode", border_style="cyan",
        ))

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
        f"เป้ากำไร  : [green]+{config.DAILY_PROFIT_TARGET_PCT:.0f}%[/green]"
        f"  = +${config.daily_profit_target_usdt:.2f} USDT"
        f"   ยอมขาดทุน: [red]-{config.MAX_LOSS_PCT:.0f}%[/red]"
        f"  = -${config.daily_max_loss_usdt:.2f} USDT\n"
        f"Mode      : {'[bold red]LIVE[/bold red]' if not config.DRY_RUN else '[bold yellow]DRY RUN[/bold yellow]'}",
        title="Config", border_style="blue",
    ))

    for w in config.capital_warnings():
        console.print(f"[{'red' if '⚠' in w else 'dim'}]{w}[/{'red' if '⚠' in w else 'dim'}]")

    if not config.DRY_RUN:
        console.print("\n[bold red]⚠  LIVE MODE — จะส่ง order จริงใน 5 วินาที (Ctrl+C ยกเลิก)[/bold red]")
        time.sleep(5)

    # ── Telegram setup ────────────────────────────────────────────────────────
    notifier.configure(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)

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

    # ── Auto Range Calculation ────────────────────────────────────────────────
    grid_range: GridRange | None = None
    if config.AUTO_RANGE and config.RANGE_STRATEGY != "manual":
        console.print(
            f"[cyan]คำนวณกรอบ Grid อัตโนมัติ (strategy={config.RANGE_STRATEGY})...[/cyan]"
        )
        try:
            candles = binance.get_klines(config.SYMBOL, config.TIMEFRAME, limit=200)
            grid_range = calculate_range(
                candles,
                current_price,
                strategy=config.RANGE_STRATEGY,
                atr_period=config.ATR_PERIOD,
                atr_multiplier=config.ATR_MULTIPLIER,
                bb_period=config.BB_PERIOD,
                bb_std=config.BB_STD,
                lookback_bars=config.LOOKBACK_BARS,
                buffer_pct=config.BUFFER_PCT,
            )
            # Override config with calculated range
            config.UPPER_PRICE = grid_range.upper
            config.LOWER_PRICE = grid_range.lower
            range_width_pct = (grid_range.upper - grid_range.lower) / current_price * 100
            width_color = "green" if range_width_pct >= 8 else ("yellow" if range_width_pct >= 4 else "red")
            console.print(
                f"[{width_color}]กรอบใหม่: ${grid_range.lower:.4f} – ${grid_range.upper:.4f}  "
                f"(กว้าง {range_width_pct:.1f}%  {grid_range.summary()})[/{width_color}]"
            )
            if range_width_pct < 5.0:
                console.print(
                    f"[bold red]⚠  กรอบแคบเกินไป ({range_width_pct:.1f}%) — XRP เคลื่อน 3-8%/วัน "
                    f"จะหลุดกรอบใน < 1 ชั่วโมง[/bold red]\n"
                    f"[yellow]แก้ .env → RANGE_STRATEGY=lookback  หรือ  "
                    f"ATR_MULTIPLIER={max(10, int(10/range_width_pct*config.ATR_MULTIPLIER))} "
                    f"(สำหรับ TF {config.TIMEFRAME})[/yellow]"
                )
                logger.warning(
                    "Range width %.1f%% is too narrow for %s TF — recommend RANGE_STRATEGY=lookback",
                    range_width_pct, config.TIMEFRAME,
                )
            notifier.alert_range_calculated(
                config.SYMBOL, grid_range.strategy_used,
                grid_range.upper, grid_range.lower, grid_range.atr_value,
            )
        except Exception as exc:
            console.print(f"[yellow]⚠  Auto range ไม่สำเร็จ: {exc} — ใช้ค่าจาก .env แทน[/yellow]")
            logger.warning("Auto range calculation failed: %s", exc)

    if grid_range is None:
        # Use manual range from .env
        grid_range = GridRange(
            upper=config.UPPER_PRICE,
            lower=config.LOWER_PRICE,
            strategy_used="Manual",
        )

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

    # ── โหลด state เก่า (ถ้ามี) ──────────────────────────────────────────────
    saved = state_manager.load_state()
    if saved:
        from grid import GridDirection as _GD
        from range_manager import GridRange as _GR
        try:
            _saved_dir = _GD(saved["direction"])
            _saved_range = _GR(
                upper=saved["grid_range"]["upper"],
                lower=saved["grid_range"]["lower"],
                strategy_used=saved["grid_range"].get("strategy_used", "Restored"),
                atr_value=saved["grid_range"].get("atr_value", 0.0),
            )
            config.UPPER_PRICE = _saved_range.upper
            config.LOWER_PRICE = _saved_range.lower
            engine.levels = engine._compute_levels()
            state_manager.restore_engine(engine, saved)
            current_direction = _saved_dir
            grid_range = _saved_range
            regrid_count = saved.get("regrid_count", 0)
            console.print(f"[cyan]▶ ต่อจาก state เก่า: {saved['saved_at']}[/cyan]")
        except Exception as exc:
            logger.warning("State restore failed: %s — เริ่มใหม่", exc)
            saved = None

    if not saved:
        if session_mode:
            # Session mode: เริ่มทันทีในโหมด BOTH ไม่ต้องรอ trend confirm
            current_direction = GridDirection.BOTH
        else:
            current_direction = _signal_to_direction(sig)
        engine.initialize(current_price, current_direction)
        regrid_count = 0

    daily_target = config.daily_profit_target_usdt
    cycle = 0
    futures_info = None
    last_midnight = time.strftime("%Y-%m-%d")
    _save_interval = max(1, int(30 / config.POLL_INTERVAL_SECONDS))  # บันทึกทุก 30 วินาที
    _last_alert_upper = False
    _last_alert_lower = False
    _last_heartbeat_cycle = 0
    _heartbeat_interval = max(1, int(180 / config.POLL_INTERVAL_SECONDS))  # ทุก 3 นาที
    _last_cooldown_direction: str = ""

    with Live(console=console, refresh_per_second=0.5, screen=True) as live:
        while _running:
            cycle += 1
            current_price = binance.get_price()

            # ── Session timer ─────────────────────────────────────────────────
            if session_mode:
                elapsed_min = (time.time() - session_start) / 60
                if elapsed_min >= config.SESSION_DURATION_MINUTES:
                    logger.info("Session ครบ %.0f นาที — หยุดบอท", config.SESSION_DURATION_MINUTES)
                    console.print(
                        f"\n[bold cyan]Session ครบ {config.SESSION_DURATION_MINUTES} นาที "
                        f"(จริง {elapsed_min:.1f} นาที) — หยุดบอทอัตโนมัติ[/bold cyan]"
                    )
                    break

            # ── Reset daily P&L at midnight ───────────────────────────────────
            today = time.strftime("%Y-%m-%d")
            if today != last_midnight:
                engine.reset_daily_profit()
                last_midnight = today
                logger.info("วันใหม่ — รีเซ็ต daily P&L")

            # ── Daily profit target check ─────────────────────────────────────
            if engine.stats.daily_profit_usdt >= daily_target:
                day_pct = engine.stats.daily_profit_usdt / config.total_margin_required * 100
                console.print(
                    f"\n[bold green]🎯 ถึงเป้าวันนี้! +{day_pct:.2f}% "
                    f"(+${engine.stats.daily_profit_usdt:.4f} USDT)[/bold green]"
                )
                console.print("[yellow]หยุดบอทสำหรับวันนี้ — รันใหม่พรุ่งนี้[/yellow]")
                notifier.alert_profit_target(config.SYMBOL, engine.stats.daily_profit_usdt, day_pct)
                break

            # ── Daily loss limit check (symmetric with profit target) ─────────
            if engine.stats.daily_profit_usdt < -config.daily_max_loss_usdt:
                day_pct = engine.stats.daily_profit_usdt / config.total_margin_required * 100
                console.print(
                    f"[bold red]หยุดบอท: ขาดทุนวันนี้ {day_pct:.2f}% "
                    f"(${engine.stats.daily_profit_usdt:.4f}) เกินลิมิต -{config.MAX_LOSS_PCT:.0f}%[/bold red]"
                )
                notifier.alert_max_loss(
                    config.SYMBOL,
                    engine.stats.daily_profit_usdt,
                    config.daily_max_loss_usdt,
                )
                break

            # ── Boundary check (alert + auto-stop) ───────────────────────────
            boundary = check_boundary(
                current_price, grid_range,
                alert_pct=config.PRICE_ALERT_PCT,
                stop_pct=config.AUTO_STOP_PCT,
            )
            if boundary.should_stop:
                breach_side = "UPPER" if boundary.breached_upper else "LOWER"
                logger.warning(
                    "ราคาหลุดกรอบ %s: current=%.4f  upper=%.4f  lower=%.4f — หยุดบอท",
                    breach_side, current_price, grid_range.upper, grid_range.lower,
                )
                console.print(
                    f"[bold red]🚨 ราคาหลุดกรอบ {breach_side}! "
                    f"${current_price:.4f} — หยุดบอทอัตโนมัติ[/bold red]"
                )
                notifier.alert_boundary_breached(
                    config.SYMBOL, current_price, breach_side,
                    grid_range.upper, grid_range.lower,
                )
                break
            if boundary.near_upper and not _last_alert_upper:
                logger.warning(
                    "ราคาใกล้ขอบบน: %.4f  ห่าง %.2f%%", current_price, boundary.upper_dist_pct
                )
                notifier.alert_boundary_near(
                    config.SYMBOL, current_price, "UPPER",
                    boundary.upper_dist_pct, grid_range.upper, grid_range.lower,
                )
            if boundary.near_lower and not _last_alert_lower:
                logger.warning(
                    "ราคาใกล้ขอบล่าง: %.4f  ห่าง %.2f%%", current_price, boundary.lower_dist_pct
                )
                notifier.alert_boundary_near(
                    config.SYMBOL, current_price, "LOWER",
                    boundary.lower_dist_pct, grid_range.upper, grid_range.lower,
                )
            _last_alert_upper = boundary.near_upper
            _last_alert_lower = boundary.near_lower

            # ── Auto Re-grid (re-center เมื่อราคาเลื่อนออกจากศูนย์กลาง) ─────
            if config.AUTO_REGRID and engine._initialized:
                needs_rg, rg_side = _needs_regrid(
                    current_price, grid_range, config.REGRID_THRESHOLD
                )
                if needs_rg and strategy.can_reset_now():
                    new_range = _compute_new_range(current_price, grid_range)
                    logger.info(
                        "Auto Re-grid: ราคา %.4f อยู่โซน %s → กรอบใหม่ %.4f–%.4f",
                        current_price, rg_side, new_range.lower, new_range.upper,
                    )
                    engine.reset()
                    strategy.mark_reset()
                    config.UPPER_PRICE = new_range.upper
                    config.LOWER_PRICE = new_range.lower
                    engine.levels = engine._compute_levels()
                    grid_range = new_range
                    engine.initialize(current_price, current_direction)
                    regrid_count += 1
                    console.print(
                        f"[cyan]Re-grid #{regrid_count}: ${new_range.lower:.4f} – "
                        f"${new_range.upper:.4f}  ({new_range.strategy_used})[/cyan]"
                    )
                    notifier.alert_range_calculated(
                        config.SYMBOL,
                        f"Re-grid #{regrid_count} ({new_range.strategy_used})",
                        new_range.upper, new_range.lower, new_range.atr_value,
                    )

            # ── Re-analyze trend (with whipsaw protection) ───────────────────
            sig = strategy.analyze()
            new_direction = _signal_to_direction(sig)

            if engine._initialized and new_direction != current_direction:
                if strategy.can_reset_now():
                    soft = config.HOLD_POSITION_ON_RESET and config.is_futures
                    logger.info(
                        "🔄 Trend เปลี่ยน: %s → %s | ราคา $%.4f | reset=%s",
                        current_direction.value, new_direction.value, current_price,
                        "soft" if soft else "hard",
                    )
                    engine.reset(soft=soft)
                    strategy.mark_reset()
                    current_direction = new_direction
                    _last_cooldown_direction = ""
                    engine.initialize(current_price, current_direction)
                else:
                    if new_direction.value != _last_cooldown_direction:
                        logger.info(
                            "⏳ Signal %s แต่ยัง cooldown — รอก่อน (ราคา $%.4f)",
                            new_direction.value, current_price,
                        )
                        _last_cooldown_direction = new_direction.value

            # ── Poll order fills ──────────────────────────────────────────────
            newly_filled = engine.poll()
            # (detailed fill logs are emitted inside grid._on_fill)

            if config.is_futures:
                futures_info = binance.get_futures_info()

            # ── Heartbeat log (ทุก ~60 วินาที) ───────────────────────────────
            if cycle - _last_heartbeat_cycle >= _heartbeat_interval:
                _last_heartbeat_cycle = cycle
                open_orders = engine.open_orders()
                buy_waits = sorted(
                    [f"${o.price:.4f}" for o in open_orders if o.side == OrderSide.BUY],
                    reverse=True,
                )
                sell_waits = sorted(
                    [f"${o.price:.4f}" for o in open_orders if o.side == OrderSide.SELL]
                )
                day_pct = (
                    engine.stats.daily_profit_usdt / config.total_margin_required * 100
                    if config.total_margin_required else 0
                )
                d_sign = "+" if day_pct >= 0 else ""
                logger.info(
                    "[STATUS] ราคา=$%.4f | %s %.0f%% | Grid=%s"
                    " | รอ BUY: %s | รอ SELL: %s"
                    " | P&L วันนี้: %s%.2f%% ($%.4f) | %.1fh",
                    current_price,
                    sig.direction, sig.strength * 100,
                    current_direction.value,
                    ",".join(buy_waits) if buy_waits else "ไม่มี",
                    ",".join(sell_waits) if sell_waits else "ไม่มี",
                    d_sign, day_pct, engine.stats.daily_profit_usdt,
                    engine.stats.runtime_hours,
                )

            # ── Save state (ทุก 30 วินาที) ───────────────────────────────────
            if cycle % _save_interval == 0:
                state_manager.save_state(
                    engine, grid_range, current_direction, regrid_count, cycle
                )

            # ── Build dashboard ───────────────────────────────────────────────
            range_panel = _range_panel(
                grid_range, current_price,
                config.PRICE_ALERT_PCT, config.AUTO_STOP_PCT,
                regrid_count,
            )
            layout = Layout()
            if config.is_futures:
                layout.split_column(
                    Layout(_header_panel(current_price, cycle, current_direction, session_start, session_mode), size=3),
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
                    Layout(range_panel),
                )
            else:
                layout.split_column(
                    Layout(_header_panel(current_price, cycle, current_direction, session_start, session_mode), size=3),
                    Layout(name="row1", size=9),
                    Layout(name="row2", size=7),
                    Layout(_grid_table(engine, current_price), name="grid"),
                    Layout(_recent_fills_panel(engine), size=10),
                )
                layout["row1"].split_row(
                    Layout(_trend_panel(sig)),
                    Layout(_stats_panel(engine, daily_target)),
                )
                layout["row2"].split_row(
                    Layout(range_panel),
                    Layout(Panel(
                        "[yellow]PRICE_ALERT_PCT: ราคาใกล้ขอบ\n"
                        "AUTO_STOP_PCT: ราคาหลุดกรอบ\n"
                        "AUTO_RANGE=true: คำนวณกรอบอัตโนมัติ[/yellow]",
                        title="Range Settings", border_style="dim"
                    )),
                )

            live.update(layout)

            for _ in range(int(config.POLL_INTERVAL_SECONDS)):
                if not _running:
                    break
                time.sleep(1)

    # ── Shutdown ──────────────────────────────────────────────────────────────
    state_manager.save_state(engine, grid_range, current_direction, regrid_count, cycle)
    console.print("[yellow]ยกเลิก open orders ทั้งหมด...[/yellow]")
    binance.cancel_all_open_orders()

    s = engine.stats
    cap = config.total_margin_required
    day_pct = s.daily_profit_usdt / cap * 100 if cap else 0
    tot_pct = s.realized_profit_usdt / cap * 100 if cap else 0
    p_color = "green" if tot_pct >= 0 else "red"
    d_color = "green" if day_pct >= 0 else "red"
    dsign = "+" if day_pct >= 0 else ""
    tsign = "+" if tot_pct >= 0 else ""
    elapsed_min = (time.time() - session_start) / 60
    session_line = (
        f"Session   : [cyan]{elapsed_min:.1f} นาที[/cyan] / {config.SESSION_DURATION_MINUTES} นาที\n"
        if session_mode else ""
    )
    title_str = "สรุป Session" if session_mode else "จบการทำงาน"
    border_str = "cyan" if session_mode else "yellow"
    console.print(Panel(
        f"[bold]สรุปผล[/bold]\n\n"
        f"{session_line}"
        f"Market    : {market_label}\n"
        f"Timeframe : {config.TIMEFRAME}\n"
        f"Cycles    : {cycle}   Resets: {s.resets}   Re-grids: {regrid_count}\n"
        f"BUY/SELL  : {s.total_buys_filled} / {s.total_sells_filled}\n"
        f"วันนี้    : [{d_color}]{dsign}{day_pct:.2f}%  ({dsign}${s.daily_profit_usdt:.4f} USDT)[/{d_color}]\n"
        f"รวมทั้งหมด: [bold {p_color}]{tsign}{tot_pct:.2f}%  ({tsign}${s.realized_profit_usdt:.4f} USDT)[/bold {p_color}]\n"
        f"Runtime   : {s.runtime_hours:.2f}h",
        title=title_str, border_style=border_str,
    ))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Binance Trend-Following Grid Bot")
    parser.add_argument(
        "--session", type=int, default=0, metavar="MINUTES",
        help="Session mode: หยุดอัตโนมัติหลัง N นาที (0 = ไม่จำกัด)",
    )
    parser.add_argument(
        "--turbo", action="store_true",
        help=(
            "Turbo mode: TF สั้น + ตัดสินใจก้าวร้าว "
            "(CDC non-strict, confirm-bars=1, min-reset=60s)"
        ),
    )
    parser.add_argument(
        "--turbo-tf", default=None, metavar="TF",
        help="Timeframe ที่จะใช้ใน turbo mode (default: 3m)",
    )
    args = parser.parse_args()
    if args.session > 0:
        config.SESSION_DURATION_MINUTES = args.session
    if args.turbo or config.TURBO_MODE:
        config.TURBO_MODE = True
        tf = args.turbo_tf or config.TURBO_TIMEFRAME
        config.TIMEFRAME = tf
        config.TREND_CONFIRM_BARS = 1
        config.MIN_RESET_INTERVAL_SECONDS = 60.0
        config.CDC_STRICT = False     # BULL=zones1-3, BEAR=zones4-6 ไม่มี NEUTRAL
        config.SIGNAL_MODE = "cdc"    # CDC ตอบสนองเร็วกว่า EMA
        # Lookback ปรับตาม TF ใหม่ให้ครอบ 24h
        _tf_secs = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800}
        config.LOOKBACK_BARS = max(48, int(86400 / _tf_secs.get(tf, 180)))
    run()
