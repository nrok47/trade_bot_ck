"""
Arbitrage scanner — finds binary markets where:

    YES_best_ask + NO_best_ask < 1.00

In a resolved binary market exactly one side pays $1.00 and the other $0.00.
If you can buy both YES and NO for less than $1.00 combined, you lock in
a risk-free profit regardless of the outcome.

Example:
    YES ask = $0.44   (implied probability 44%)
    NO  ask = $0.44   (implied probability 44%)
    Total   = $0.88   → guaranteed profit of $0.12 per $1 face value (13.6%)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from config import config
from polymarket_client import ArbMarket, polymarket

logger = logging.getLogger(__name__)


@dataclass
class ArbOpportunity:
    market: ArbMarket
    gross_profit_pct: float       # % profit before fees, per unit
    net_profit_usdc: float        # USDC profit at MAX_TRADE_SIZE
    units: float                  # contracts to buy (limited by MAX_TRADE_SIZE)

    def summary(self) -> str:
        return (
            f"[ARB] {self.market.question[:70]}\n"
            f"      YES ask={self.market.yes_book.best_ask:.4f}  "
            f"NO ask={self.market.no_book.best_ask:.4f}  "
            f"Cost={self.market.arb_cost:.4f}  "
            f"Gross={self.gross_profit_pct:.2f}%  "
            f"Net≈${self.net_profit_usdc:.4f} USDC"
        )


class ArbScanner:
    def __init__(self) -> None:
        self._market_cache: list[dict] = []
        self._cache_cycles: int = 0
        self._cache_refresh_every: int = 60  # refresh full market list every 60 scans

    def _refresh_markets(self, force: bool = False) -> None:
        if force or self._cache_cycles % self._cache_refresh_every == 0:
            logger.info("Fetching active markets from Polymarket…")
            self._market_cache = polymarket.get_markets(limit=500)
            logger.info("Loaded %d markets", len(self._market_cache))
        self._cache_cycles += 1

    def scan(self) -> list[ArbOpportunity]:
        """Scan all cached markets and return opportunities sorted by net profit."""
        self._refresh_markets()
        opportunities: list[ArbOpportunity] = []

        for raw_market in self._market_cache:
            arb_market = polymarket.get_arb_market(raw_market)
            if arb_market is None:
                continue

            net_per_unit = arb_market.net_profit_per_unit(config.TAKER_FEE_RATE)
            if net_per_unit <= 0:
                continue  # no arb after fees

            # How many units can we buy with MAX_TRADE_SIZE?
            max_units = config.MAX_TRADE_SIZE_USDC / arb_market.arb_cost
            net_profit_usdc = net_per_unit * max_units

            if net_profit_usdc < config.MIN_PROFIT_USDC:
                continue  # profit too small to bother

            gross_pct = (arb_market.gross_profit_per_unit / arb_market.arb_cost) * 100

            opp = ArbOpportunity(
                market=arb_market,
                gross_profit_pct=gross_pct,
                net_profit_usdc=net_profit_usdc,
                units=max_units,
            )
            opportunities.append(opp)
            logger.debug(opp.summary())

        # Best opportunities first
        opportunities.sort(key=lambda o: o.net_profit_usdc, reverse=True)
        return opportunities


scanner = ArbScanner()
