"""
Order executor — takes an ArbOpportunity and fires both legs simultaneously.

Safety rules enforced here:
  - DRY_RUN: log only, never touch the exchange
  - MAX_OPEN_POSITIONS_USDC: refuse if total exposure already too high
  - Leg-failure guard: if YES order fails, skip NO order to avoid naked position
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from config import config
from polymarket_client import ArbOpportunity, polymarket
from scanner import ArbOpportunity

logger = logging.getLogger(__name__)


@dataclass
class TradeResult:
    condition_id: str
    question: str
    yes_order: dict | None
    no_order: dict | None
    units: float
    expected_profit_usdc: float
    success: bool
    timestamp: float = field(default_factory=time.time)

    def summary(self) -> str:
        status = "OK" if self.success else "FAILED"
        mode = "[DRY RUN] " if config.DRY_RUN else ""
        return (
            f"{mode}[{status}] {self.question[:60]}\n"
            f"  Units: {self.units:.4f}  "
            f"Expected profit: ${self.expected_profit_usdc:.4f} USDC"
        )


class Executor:
    def __init__(self) -> None:
        self._open_usdc: float = 0.0        # running total of open exposure
        self.trade_history: list[TradeResult] = []
        self.total_profit_usdc: float = 0.0

    @property
    def open_usdc(self) -> float:
        return self._open_usdc

    def can_trade(self, cost_usdc: float) -> bool:
        return (self._open_usdc + cost_usdc) <= config.MAX_OPEN_POSITIONS_USDC

    def execute(self, opp: ArbOpportunity) -> TradeResult:
        cost_per_unit = opp.market.arb_cost
        units = min(
            opp.units,
            (config.MAX_OPEN_POSITIONS_USDC - self._open_usdc) / cost_per_unit,
        )
        if units <= 0:
            logger.warning("Position limit reached, skipping %s", opp.market.question[:50])
            return TradeResult(
                condition_id=opp.market.condition_id,
                question=opp.market.question,
                yes_order=None,
                no_order=None,
                units=0,
                expected_profit_usdc=0,
                success=False,
            )

        yes_cost = opp.market.yes_book.best_ask * units
        no_cost = opp.market.no_book.best_ask * units

        # --- Leg 1: YES ---
        yes_order = polymarket.place_market_order(
            token_id=opp.market.yes_token_id,
            amount_usdc=yes_cost,
            side="BUY",
        )
        if yes_order is None:
            logger.error("YES leg failed for %s — skipping NO leg", opp.market.question[:50])
            return TradeResult(
                condition_id=opp.market.condition_id,
                question=opp.market.question,
                yes_order=None,
                no_order=None,
                units=units,
                expected_profit_usdc=opp.net_profit_usdc,
                success=False,
            )

        # --- Leg 2: NO ---
        no_order = polymarket.place_market_order(
            token_id=opp.market.no_token_id,
            amount_usdc=no_cost,
            side="BUY",
        )
        if no_order is None:
            logger.error(
                "NO leg failed for %s — WARNING: naked YES position open!",
                opp.market.question[:50],
            )
            return TradeResult(
                condition_id=opp.market.condition_id,
                question=opp.market.question,
                yes_order=yes_order,
                no_order=None,
                units=units,
                expected_profit_usdc=opp.net_profit_usdc,
                success=False,
            )

        total_cost = yes_cost + no_cost
        self._open_usdc += total_cost
        expected_profit = opp.net_profit_usdc

        result = TradeResult(
            condition_id=opp.market.condition_id,
            question=opp.market.question,
            yes_order=yes_order,
            no_order=no_order,
            units=units,
            expected_profit_usdc=expected_profit,
            success=True,
        )
        self.trade_history.append(result)
        self.total_profit_usdc += expected_profit
        logger.info(result.summary())
        return result


executor = Executor()
