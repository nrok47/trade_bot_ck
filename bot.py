"""
Polymarket Binary Arbitrage Bot
================================
Strategy: Binary Arbitrage
  - Scans Polymarket prediction markets every N seconds
  - A binary market has two outcomes: YES and NO
  - Each resolved contract pays exactly $1.00 to the winning side
  - If  YES_ask + NO_ask < $1.00  →  guaranteed profit buying both sides
  - Bot buys both legs simultaneously and waits for market resolution

Usage:
  1. Copy .env.example → .env and fill in your credentials
  2. Set DRY_RUN=true to scan without placing orders (recommended first)
  3. python bot.py

Safety defaults:
  - DRY_RUN=true by default — will NOT place real orders until you change it
  - MAX_TRADE_SIZE_USDC limits exposure per trade
  - MAX_OPEN_POSITIONS_USDC limits total exposure
"""
from __future__ import annotations

import logging
import signal
import sys
import time

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich import box

from config import config
from executor import executor
from scanner import scanner, ArbOpportunity

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot")
console = Console()

# ── Graceful shutdown ─────────────────────────────────────────────────────────
_running = True


def _handle_signal(sig, frame):
    global _running
    console.print("\n[yellow]Shutting down…[/yellow]")
    _running = False


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Dashboard ─────────────────────────────────────────────────────────────────

def _build_status_panel(cycle: int, opportunities: list[ArbOpportunity]) -> Panel:
    mode = "[bold red]LIVE[/bold red]" if not config.DRY_RUN else "[bold yellow]DRY RUN[/bold yellow]"
    lines = [
        f"Mode: {mode}   Cycle: {cycle}   Open: ${executor.open_usdc:.2f} / ${config.MAX_OPEN_POSITIONS_USDC:.2f}",
        f"Trades executed: {len(executor.trade_history)}   "
        f"Expected profit: ${executor.total_profit_usdc:.4f} USDC",
        f"Opportunities found this scan: {len(opportunities)}",
    ]
    return Panel("\n".join(lines), title="Polymarket Arb Bot", border_style="green")


def _build_opp_table(opportunities: list[ArbOpportunity]) -> Table:
    table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
    table.add_column("Market", style="white", no_wrap=False, max_width=55)
    table.add_column("YES ask", justify="right", style="green")
    table.add_column("NO ask", justify="right", style="green")
    table.add_column("Cost", justify="right", style="yellow")
    table.add_column("Gross%", justify="right", style="magenta")
    table.add_column("Net USDC", justify="right", style="bold green")

    for opp in opportunities[:20]:  # show top 20
        table.add_row(
            opp.market.question[:55],
            f"{opp.market.yes_book.best_ask:.4f}",
            f"{opp.market.no_book.best_ask:.4f}",
            f"{opp.market.arb_cost:.4f}",
            f"{opp.gross_profit_pct:.2f}%",
            f"${opp.net_profit_usdc:.4f}",
        )

    if not opportunities:
        table.add_row("[dim]No arbitrage opportunities found[/dim]", "", "", "", "", "")

    return table


# ── Main loop ─────────────────────────────────────────────────────────────────

def run() -> None:
    console.print(
        Panel.fit(
            "[bold green]Polymarket Binary Arbitrage Bot[/bold green]\n"
            f"Strategy : YES ask + NO ask < $1.00 → buy both legs\n"
            f"Mode     : {'[bold red]LIVE TRADING[/bold red]' if not config.DRY_RUN else '[bold yellow]DRY RUN (no real orders)[/bold yellow]'}\n"
            f"Min profit: ${config.MIN_PROFIT_USDC:.4f} USDC per scan\n"
            f"Max trade : ${config.MAX_TRADE_SIZE_USDC:.2f} USDC\n"
            f"Scan every: {config.SCAN_INTERVAL_SECONDS}s",
            title="Config",
            border_style="blue",
        )
    )

    if not config.DRY_RUN:
        config.validate_for_live_trading()
        console.print("[bold red]⚠  LIVE MODE — real orders will be placed![/bold red]")
        time.sleep(3)  # brief pause so user can Ctrl+C

    cycle = 0
    while _running:
        cycle += 1
        logger.info("=== Scan cycle %d ===", cycle)

        try:
            opportunities = scanner.scan()
        except Exception as exc:
            logger.error("Scanner error: %s", exc)
            opportunities = []

        # Print dashboard
        console.print(_build_status_panel(cycle, opportunities))
        console.print(_build_opp_table(opportunities))

        # Execute best opportunity (if any)
        if opportunities:
            best = opportunities[0]
            logger.info("Executing best opportunity: %s", best.market.question[:60])
            result = executor.execute(best)
            if result.success:
                console.print(f"[bold green]{result.summary()}[/bold green]")
            else:
                console.print(f"[bold red]{result.summary()}[/bold red]")

        # Sleep until next cycle
        for _ in range(int(config.SCAN_INTERVAL_SECONDS)):
            if not _running:
                break
            time.sleep(1)

    console.print(
        Panel(
            f"[bold]Session summary[/bold]\n"
            f"Cycles run       : {cycle}\n"
            f"Trades executed  : {len(executor.trade_history)}\n"
            f"Expected profit  : ${executor.total_profit_usdc:.4f} USDC",
            title="Done",
            border_style="yellow",
        )
    )


if __name__ == "__main__":
    run()
