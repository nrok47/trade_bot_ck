"""
Binance API wrapper — supports both Spot and Futures (USDT-M).
ทุก method จะเรียก Spot หรือ Futures API โดยอัตโนมัติตาม config.MARKET
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException

from config import config
from indicators import CandleData

logger = logging.getLogger(__name__)


@dataclass
class SymbolInfo:
    symbol: str
    price_precision: int
    qty_precision: int
    min_qty: float
    min_notional: float


@dataclass
class FuturesInfo:
    """Extra info only available in Futures mode."""
    leverage: int
    margin_type: str
    liquidation_price: float    # 0 if no position open
    unrealized_pnl: float
    position_size: float        # current open position qty
    funding_rate: float         # current funding rate (per 8h)


class BinanceClient:
    def __init__(self) -> None:
        requests_params = {}
        if config.PROXY_URL:
            requests_params["proxies"] = {
                "http": config.PROXY_URL,
                "https": config.PROXY_URL,
            }
            logger.info("Using proxy: %s", config.PROXY_URL)
        if config.API_KEY and config.API_SECRET:
            self._client = Client(config.API_KEY, config.API_SECRET,
                                  requests_params=requests_params)
        else:
            self._client = Client(requests_params=requests_params)
        self._symbol_info: Optional[SymbolInfo] = None
        self._last_price: float = 0.0

    # ── Symbol metadata ───────────────────────────────────────────────────────

    def get_symbol_info(self) -> SymbolInfo:
        if self._symbol_info:
            return self._symbol_info

        if config.is_futures:
            # futures_exchange_info() คืน dict ที่มี list "symbols"
            exchange_info = self._client.futures_exchange_info()
            info = next(
                (s for s in exchange_info["symbols"] if s["symbol"] == config.SYMBOL),
                None,
            )
        else:
            info = self._client.get_symbol_info(config.SYMBOL)

        if not info:
            raise ValueError(f"Symbol {config.SYMBOL} not found")

        filters = {f["filterType"]: f for f in info["filters"]}
        lot = filters.get("LOT_SIZE", {})

        step_size = float(lot.get("stepSize", "0.001"))
        qty_precision = int(round(-math.log10(step_size))) if step_size > 0 else 3

        if config.is_futures:
            min_notional = float(
                filters.get("MIN_NOTIONAL", {}).get("notional", "5")
            )
            price_precision = int(info.get("pricePrecision", 4))
        else:
            min_notional = float(
                filters.get("MIN_NOTIONAL", {}).get("minNotional", "10")
            )
            price_precision = int(info.get("quotePrecision", 2))

        self._symbol_info = SymbolInfo(
            symbol=config.SYMBOL,
            price_precision=price_precision,
            qty_precision=qty_precision,
            min_qty=float(lot.get("minQty", "0.001")),
            min_notional=min_notional,
        )
        return self._symbol_info

    # ── Price ─────────────────────────────────────────────────────────────────

    def get_price(self) -> float:
        for attempt in range(3):
            try:
                if config.is_futures:
                    ticker = self._client.futures_symbol_ticker(symbol=config.SYMBOL)
                else:
                    ticker = self._client.get_symbol_ticker(symbol=config.SYMBOL)
                self._last_price = float(ticker["price"])
                return self._last_price
            except BinanceAPIException as exc:
                if "restricted location" in str(exc):
                    logger.error(
                        "Binance บล็อก IP นี้ (restricted location) — "
                        "ต้องตั้งค่า PROXY_URL ใน .env  attempt=%d/3", attempt + 1
                    )
                    if attempt < 2:
                        time.sleep(5)
                    else:
                        if self._last_price:
                            logger.warning("ใช้ราคาล่าสุด $%.4f แทน", self._last_price)
                            return self._last_price
                        raise
                else:
                    raise
        return self._last_price

    # ── Balance ───────────────────────────────────────────────────────────────

    def get_balance(self, asset: str = "USDT") -> float:
        if config.DRY_RUN:
            return 9999.0
        try:
            if config.is_futures:
                balances = self._client.futures_account_balance()
                for b in balances:
                    if b["asset"] == asset:
                        return float(b["availableBalance"])
                return 0.0
            else:
                bal = self._client.get_asset_balance(asset=asset)
                return float(bal["free"]) if bal else 0.0
        except BinanceAPIException as exc:
            logger.error("get_balance error: %s", exc)
            return 0.0

    # ── Futures-specific setup & info ─────────────────────────────────────────

    def setup_futures(self) -> None:
        """Set leverage and margin type for futures trading."""
        if not config.is_futures or config.DRY_RUN:
            return
        try:
            self._client.futures_change_leverage(
                symbol=config.SYMBOL,
                leverage=config.LEVERAGE,
            )
            logger.info("Leverage set to %dx", config.LEVERAGE)
        except BinanceAPIException as exc:
            logger.warning("futures_change_leverage: %s", exc)

        try:
            self._client.futures_change_margin_type(
                symbol=config.SYMBOL,
                marginType=config.MARGIN_TYPE,
            )
            logger.info("Margin type set to %s", config.MARGIN_TYPE)
        except BinanceAPIException as exc:
            # Error code -4046 = margin type already set — safe to ignore
            if "4046" not in str(exc):
                logger.warning("futures_change_margin_type: %s", exc)

    def get_futures_info(self) -> FuturesInfo:
        """Return live futures position info for the dashboard."""
        if config.DRY_RUN:
            return FuturesInfo(
                leverage=config.LEVERAGE,
                margin_type=config.MARGIN_TYPE,
                liquidation_price=0.0,
                unrealized_pnl=0.0,
                position_size=0.0,
                funding_rate=0.0,
            )
        try:
            positions = self._client.futures_position_information(symbol=config.SYMBOL)
            pos = positions[0] if positions else {}

            funding = self._client.futures_mark_price(symbol=config.SYMBOL)
            funding_rate = float(funding.get("lastFundingRate", 0))

            return FuturesInfo(
                leverage=int(pos.get("leverage", config.LEVERAGE)),
                margin_type=pos.get("marginType", config.MARGIN_TYPE).upper(),
                liquidation_price=float(pos.get("liquidationPrice", 0)),
                unrealized_pnl=float(pos.get("unRealizedProfit", 0)),
                position_size=float(pos.get("positionAmt", 0)),
                funding_rate=funding_rate,
            )
        except BinanceAPIException as exc:
            logger.error("get_futures_info error: %s", exc)
            return FuturesInfo(config.LEVERAGE, config.MARGIN_TYPE, 0, 0, 0, 0)

    # ── Rounding helpers ──────────────────────────────────────────────────────

    def _round_qty(self, qty: float) -> float:
        info = self.get_symbol_info()
        factor = 10 ** info.qty_precision
        return math.floor(qty * factor) / factor

    def _round_price(self, price: float) -> float:
        info = self.get_symbol_info()
        factor = 10 ** info.price_precision
        return round(price * factor) / factor

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_limit_buy(self, price: float, usdt_amount: float) -> Optional[dict]:
        rounded_price = self._round_price(price)
        # Futures: qty = (usdt * leverage) / price
        effective_usdt = usdt_amount * config.LEVERAGE if config.is_futures else usdt_amount
        qty = self._round_qty(effective_usdt / rounded_price)

        if config.DRY_RUN:
            logger.info("[DRY RUN] LIMIT BUY  %.6f %s @ %.2f  (%s)",
                        qty, config.SYMBOL, rounded_price, config.MARKET.upper())
            return {"orderId": f"DRY_BUY_{rounded_price:.6f}", "status": "NEW",
                    "side": "BUY", "price": str(rounded_price), "origQty": str(qty)}

        try:
            if config.is_futures:
                order = self._client.futures_create_order(
                    symbol=config.SYMBOL,
                    side=Client.SIDE_BUY,
                    type=Client.FUTURE_ORDER_TYPE_LIMIT,
                    timeInForce=Client.TIME_IN_FORCE_GTC,
                    quantity=qty,
                    price=rounded_price,
                )
            else:
                order = self._client.create_order(
                    symbol=config.SYMBOL,
                    side=Client.SIDE_BUY,
                    type=Client.ORDER_TYPE_LIMIT,
                    timeInForce=Client.TIME_IN_FORCE_GTC,
                    quantity=qty,
                    price=rounded_price,
                )
            logger.info("LIMIT BUY  %.6f %s @ %.2f  id=%s",
                        qty, config.SYMBOL, rounded_price, order["orderId"])
            return order
        except BinanceAPIException as exc:
            logger.error("place_limit_buy error: %s", exc)
            return None

    def place_limit_sell(self, price: float, qty: float) -> Optional[dict]:
        rounded_price = self._round_price(price)
        rounded_qty = self._round_qty(qty)

        if config.DRY_RUN:
            logger.info("[DRY RUN] LIMIT SELL %.6f %s @ %.2f  (%s)",
                        rounded_qty, config.SYMBOL, rounded_price, config.MARKET.upper())
            return {"orderId": f"DRY_SELL_{rounded_price:.6f}", "status": "NEW",
                    "side": "SELL", "price": str(rounded_price), "origQty": str(rounded_qty)}

        try:
            if config.is_futures:
                order = self._client.futures_create_order(
                    symbol=config.SYMBOL,
                    side=Client.SIDE_SELL,
                    type=Client.FUTURE_ORDER_TYPE_LIMIT,
                    timeInForce=Client.TIME_IN_FORCE_GTC,
                    quantity=rounded_qty,
                    price=rounded_price,
                    reduceOnly=False,
                )
            else:
                order = self._client.create_order(
                    symbol=config.SYMBOL,
                    side=Client.SIDE_SELL,
                    type=Client.ORDER_TYPE_LIMIT,
                    timeInForce=Client.TIME_IN_FORCE_GTC,
                    quantity=rounded_qty,
                    price=rounded_price,
                )
            logger.info("LIMIT SELL %.6f %s @ %.2f  id=%s",
                        rounded_qty, config.SYMBOL, rounded_price, order["orderId"])
            return order
        except BinanceAPIException as exc:
            logger.error("place_limit_sell error: %s", exc)
            return None

    def get_order_status(self, order_id: str) -> Optional[str]:
        if config.DRY_RUN:
            return "NEW"
        try:
            if config.is_futures:
                order = self._client.futures_get_order(
                    symbol=config.SYMBOL, orderId=order_id
                )
            else:
                order = self._client.get_order(
                    symbol=config.SYMBOL, orderId=order_id
                )
            return order.get("status")
        except BinanceAPIException as exc:
            logger.error("get_order_status(%s) error: %s", order_id, exc)
            return None

    def cancel_order(self, order_id: str) -> bool:
        if config.DRY_RUN:
            return True
        try:
            if config.is_futures:
                self._client.futures_cancel_order(
                    symbol=config.SYMBOL, orderId=order_id
                )
            else:
                self._client.cancel_order(
                    symbol=config.SYMBOL, orderId=order_id
                )
            return True
        except BinanceAPIException as exc:
            logger.error("cancel_order(%s) error: %s", order_id, exc)
            return False

    # ── Klines (candles) ──────────────────────────────────────────────────────

    def get_klines(
        self, symbol: str, interval: str, limit: int = 50
    ) -> Optional[CandleData]:
        """
        ดึง candle data จาก Binance (ใช้ได้ทั้ง Spot และ Futures)
        interval: "1m", "3m", "5m", "15m", "30m" ฯลฯ
        """
        try:
            if config.is_futures:
                raw = self._client.futures_klines(
                    symbol=symbol, interval=interval, limit=limit
                )
            else:
                raw = self._client.get_klines(
                    symbol=symbol, interval=interval, limit=limit
                )
            # raw[i] = [open_time, open, high, low, close, volume, ...]
            opens   = [float(k[1]) for k in raw]
            highs   = [float(k[2]) for k in raw]
            lows    = [float(k[3]) for k in raw]
            closes  = [float(k[4]) for k in raw]
            volumes = [float(k[5]) for k in raw]
            return CandleData(opens, highs, lows, closes, volumes)
        except BinanceAPIException as exc:
            logger.error("get_klines error: %s", exc)
            return None

    def place_stop_market(self, side: str, qty: float, stop_price: float, dry_run: bool) -> Optional[str]:
        """
        Place STOP_MARKET reduceOnly futures order (hard SL that survives bot offline).
        side: "SELL" for LONG position, "BUY" for SHORT position.
        Returns orderId string or None on failure.
        """
        rounded_price = self._round_price(stop_price)
        rounded_qty   = self._round_qty(qty)
        if dry_run:
            fake_id = f"DRY_SL_{side}_{rounded_price}"
            logger.info("[DRY] STOP_MARKET %s  qty=%.4f  stop=%.4f  id=%s",
                        side, rounded_qty, rounded_price, fake_id)
            return fake_id
        try:
            order_side = Client.SIDE_SELL if side == "SELL" else Client.SIDE_BUY
            resp = self._client.futures_create_order(
                symbol=config.SYMBOL,
                side=order_side,
                type="STOP_MARKET",
                stopPrice=rounded_price,
                quantity=rounded_qty,
                reduceOnly=True,
                workingType="MARK_PRICE",
            )
            order_id = str(resp["orderId"])
            logger.info("STOP_MARKET %s placed  qty=%.4f  stop=%.4f  id=%s",
                        side, rounded_qty, rounded_price, order_id)
            return order_id
        except BinanceAPIException as exc:
            logger.error("place_stop_market error: %s", exc)
            return None

    def cancel_all_open_orders(self) -> None:
        if config.DRY_RUN:
            logger.info("[DRY RUN] Would cancel all open orders")
            return
        try:
            if config.is_futures:
                self._client.futures_cancel_all_open_orders(symbol=config.SYMBOL)
            else:
                self._client.cancel_open_orders(symbol=config.SYMBOL)
            logger.info("Cancelled all open orders for %s", config.SYMBOL)
        except BinanceAPIException as exc:
            logger.error("cancel_all_open_orders error: %s", exc)

    def close_position_qty(self, qty: float, is_long: bool) -> None:
        """ปิด position เฉพาะ qty ที่ระบุ (reduceOnly market order) — ใช้ตอน Stop Loss."""
        if not config.is_futures:
            return
        if config.DRY_RUN:
            logger.info("[DRY RUN] Would close %.6f %s position", qty, "LONG" if is_long else "SHORT")
            return
        try:
            close_side = Client.SIDE_SELL if is_long else Client.SIDE_BUY
            close_qty = self._round_qty(qty)
            self._client.futures_create_order(
                symbol=config.SYMBOL,
                side=close_side,
                type=Client.FUTURE_ORDER_TYPE_MARKET,
                quantity=close_qty,
                reduceOnly=True,
            )
            logger.info("SL close: %s %.6f %s (market)", close_side, close_qty, config.SYMBOL)
        except BinanceAPIException as exc:
            logger.error("close_position_qty error: %s", exc)

    def close_all_positions(self) -> None:
        """
        ปิด open position ทั้งหมดด้วย market order (Futures เท่านั้น)
        เรียกตอน trend เปลี่ยน เพื่อป้องกัน position ค้าง
        """
        if not config.is_futures:
            return
        if config.DRY_RUN:
            logger.info("[DRY RUN] Would close all open positions")
            return
        try:
            positions = self._client.futures_position_information(symbol=config.SYMBOL)
            for pos in positions:
                amt = float(pos.get("positionAmt", 0))
                if amt == 0:
                    continue
                # amt > 0 = Long → ต้องขาย, amt < 0 = Short → ต้องซื้อ
                close_side = Client.SIDE_SELL if amt > 0 else Client.SIDE_BUY
                close_qty = self._round_qty(abs(amt))
                self._client.futures_create_order(
                    symbol=config.SYMBOL,
                    side=close_side,
                    type=Client.FUTURE_ORDER_TYPE_MARKET,
                    quantity=close_qty,
                    reduceOnly=True,
                )
                logger.info(
                    "Closed position: %s %.6f %s (market order)",
                    close_side, close_qty, config.SYMBOL,
                )
        except BinanceAPIException as exc:
            logger.error("close_all_positions error: %s", exc)

    def get_available_margin(self) -> float:
        """คืน margin ที่ใช้ได้ตอนนี้ (Futures: availableBalance, Spot: free USDT)."""
        return self.get_balance("USDT")


binance = BinanceClient()
