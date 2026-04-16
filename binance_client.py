"""
Binance API wrapper — handles price feed, order placement, and symbol info.
Supports DRY_RUN mode where all order calls are logged but not sent.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException

from config import config

logger = logging.getLogger(__name__)


@dataclass
class SymbolInfo:
    """Precision rules returned by Binance exchange info."""
    symbol: str
    price_precision: int      # decimal places for price
    qty_precision: int        # decimal places for quantity
    min_qty: float
    min_notional: float       # minimum order value in USDT


class BinanceClient:
    def __init__(self) -> None:
        if config.API_KEY and config.API_SECRET:
            self._client = Client(config.API_KEY, config.API_SECRET)
        else:
            # Public endpoints only (price, symbol info) — no auth needed
            self._client = Client()
        self._symbol_info: Optional[SymbolInfo] = None

    # ── Symbol metadata ───────────────────────────────────────────────────────

    def get_symbol_info(self) -> SymbolInfo:
        if self._symbol_info:
            return self._symbol_info

        info = self._client.get_symbol_info(config.SYMBOL)
        if not info:
            raise ValueError(f"Symbol {config.SYMBOL} not found on Binance")

        filters = {f["filterType"]: f for f in info["filters"]}

        lot = filters.get("LOT_SIZE", {})
        notional = filters.get("MIN_NOTIONAL", {})

        step_size = float(lot.get("stepSize", "0.00001"))
        qty_precision = int(round(-math.log10(step_size))) if step_size > 0 else 5

        tick_size = float(info.get("quotePrecision", 2))

        self._symbol_info = SymbolInfo(
            symbol=config.SYMBOL,
            price_precision=int(info.get("quotePrecision", 2)),
            qty_precision=qty_precision,
            min_qty=float(lot.get("minQty", "0.00001")),
            min_notional=float(notional.get("minNotional", "10")),
        )
        return self._symbol_info

    # ── Price ─────────────────────────────────────────────────────────────────

    def get_price(self) -> float:
        ticker = self._client.get_symbol_ticker(symbol=config.SYMBOL)
        return float(ticker["price"])

    def get_balance(self, asset: str = "USDT") -> float:
        """Return free balance for an asset."""
        if config.DRY_RUN:
            return 9999.0  # simulated balance
        try:
            bal = self._client.get_asset_balance(asset=asset)
            return float(bal["free"]) if bal else 0.0
        except BinanceAPIException as exc:
            logger.error("get_balance error: %s", exc)
            return 0.0

    # ── Orders ────────────────────────────────────────────────────────────────

    def _round_qty(self, qty: float) -> float:
        info = self.get_symbol_info()
        factor = 10 ** info.qty_precision
        return math.floor(qty * factor) / factor

    def _round_price(self, price: float) -> float:
        info = self.get_symbol_info()
        factor = 10 ** info.price_precision
        return round(price * factor) / factor

    def place_limit_buy(self, price: float, usdt_amount: float) -> Optional[dict]:
        """Place a limit BUY order. Returns order dict or None."""
        rounded_price = self._round_price(price)
        qty = self._round_qty(usdt_amount / rounded_price)

        if config.DRY_RUN:
            order = {
                "orderId": f"DRY_{int(price)}",
                "status": "NEW",
                "side": "BUY",
                "price": str(rounded_price),
                "origQty": str(qty),
                "dry_run": True,
            }
            logger.info("[DRY RUN] LIMIT BUY  %.6f %s @ %.2f USDT",
                        qty, config.SYMBOL, rounded_price)
            return order

        try:
            order = self._client.create_order(
                symbol=config.SYMBOL,
                side=Client.SIDE_BUY,
                type=Client.ORDER_TYPE_LIMIT,
                timeInForce=Client.TIME_IN_FORCE_GTC,
                quantity=qty,
                price=rounded_price,
            )
            logger.info("LIMIT BUY  %.6f %s @ %.2f  orderId=%s",
                        qty, config.SYMBOL, rounded_price, order["orderId"])
            return order
        except BinanceAPIException as exc:
            logger.error("place_limit_buy error: %s", exc)
            return None

    def place_limit_sell(self, price: float, qty: float) -> Optional[dict]:
        """Place a limit SELL order for qty units."""
        rounded_price = self._round_price(price)
        rounded_qty = self._round_qty(qty)

        if config.DRY_RUN:
            order = {
                "orderId": f"DRY_SELL_{int(price)}",
                "status": "NEW",
                "side": "SELL",
                "price": str(rounded_price),
                "origQty": str(rounded_qty),
                "dry_run": True,
            }
            logger.info("[DRY RUN] LIMIT SELL %.6f %s @ %.2f USDT",
                        rounded_qty, config.SYMBOL, rounded_price)
            return order

        try:
            order = self._client.create_order(
                symbol=config.SYMBOL,
                side=Client.SIDE_SELL,
                type=Client.ORDER_TYPE_LIMIT,
                timeInForce=Client.TIME_IN_FORCE_GTC,
                quantity=rounded_qty,
                price=rounded_price,
            )
            logger.info("LIMIT SELL %.6f %s @ %.2f  orderId=%s",
                        rounded_qty, config.SYMBOL, rounded_price, order["orderId"])
            return order
        except BinanceAPIException as exc:
            logger.error("place_limit_sell error: %s", exc)
            return None

    def cancel_order(self, order_id: str) -> bool:
        if config.DRY_RUN:
            return True
        try:
            self._client.cancel_order(symbol=config.SYMBOL, orderId=order_id)
            return True
        except BinanceAPIException as exc:
            logger.error("cancel_order(%s) error: %s", order_id, exc)
            return False

    def get_order_status(self, order_id: str) -> Optional[str]:
        """Return order status string: NEW, PARTIALLY_FILLED, FILLED, CANCELED, etc."""
        if config.DRY_RUN:
            return "NEW"
        try:
            order = self._client.get_order(symbol=config.SYMBOL, orderId=order_id)
            return order.get("status")
        except BinanceAPIException as exc:
            logger.error("get_order_status(%s) error: %s", order_id, exc)
            return None

    def cancel_all_open_orders(self) -> None:
        if config.DRY_RUN:
            logger.info("[DRY RUN] Would cancel all open orders")
            return
        try:
            self._client.cancel_open_orders(symbol=config.SYMBOL)
            logger.info("Cancelled all open orders for %s", config.SYMBOL)
        except BinanceAPIException as exc:
            logger.error("cancel_all_open_orders error: %s", exc)


binance = BinanceClient()
