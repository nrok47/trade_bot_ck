"""
Thin wrapper around py-clob-client that handles auth, pagination,
and returns plain Python dicts so the rest of the bot stays clean.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    BookParams,
    OrderArgs,
    OrderType,
    MarketOrderArgs,
)
from py_clob_client.constants import POLYGON

from config import config

logger = logging.getLogger(__name__)


@dataclass
class OrderBook:
    token_id: str
    best_bid: float   # highest price someone will BUY at
    best_ask: float   # lowest price someone will SELL at (you pay this to go long)
    mid: float


@dataclass
class ArbMarket:
    """Snapshot of a single binary market with YES/NO order books."""
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_book: OrderBook
    no_book: OrderBook

    @property
    def arb_cost(self) -> float:
        """Total USDC needed to buy 1 contract of YES + 1 contract of NO."""
        return self.yes_book.best_ask + self.no_book.best_ask

    @property
    def gross_profit_per_unit(self) -> float:
        """Guaranteed payout is $1.00 per winning contract.
        Both sides together pay out exactly $1.00.
        Profit = 1.00 - cost_of_both_sides."""
        return 1.0 - self.arb_cost

    def net_profit_per_unit(self, fee_rate: float = 0.0) -> float:
        fees = self.arb_cost * fee_rate * 2  # fee on each leg
        return self.gross_profit_per_unit - fees


class PolymarketClient:
    def __init__(self) -> None:
        self._client: Optional[ClobClient] = None

    def _get_client(self) -> ClobClient:
        if self._client is None:
            if config.PRIVATE_KEY and config.API_KEY:
                self._client = ClobClient(
                    host=config.CLOB_HOST,
                    chain_id=POLYGON,
                    key=config.PRIVATE_KEY,
                    creds={
                        "apiKey": config.API_KEY,
                        "secret": config.API_SECRET,
                        "passphrase": config.API_PASSPHRASE,
                    },
                )
            else:
                # Read-only client (no auth needed for market scanning)
                self._client = ClobClient(
                    host=config.CLOB_HOST,
                    chain_id=POLYGON,
                )
        return self._client

    def get_markets(self, limit: int = 100) -> list[dict]:
        """Return active binary (YES/NO) markets."""
        client = self._get_client()
        markets = []
        next_cursor = ""
        while True:
            try:
                resp = client.get_markets(next_cursor=next_cursor)
            except Exception as exc:
                logger.error("get_markets error: %s", exc)
                break

            batch = resp.get("data", [])
            for m in batch:
                # Only keep active binary markets with exactly 2 tokens
                if (
                    m.get("active")
                    and not m.get("closed")
                    and len(m.get("tokens", [])) == 2
                ):
                    markets.append(m)

            next_cursor = resp.get("next_cursor", "")
            if not next_cursor or next_cursor == "LTE=" or len(markets) >= limit:
                break

        return markets[:limit]

    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """Fetch best bid/ask for a single token."""
        client = self._get_client()
        try:
            book = client.get_order_book(token_id)
        except Exception as exc:
            logger.warning("get_order_book(%s) error: %s", token_id, exc)
            return None

        bids = sorted(book.bids, key=lambda x: float(x.price), reverse=True)
        asks = sorted(book.asks, key=lambda x: float(x.price))

        best_bid = float(bids[0].price) if bids else 0.0
        best_ask = float(asks[0].price) if asks else 1.0
        mid = (best_bid + best_ask) / 2 if bids and asks else best_ask

        return OrderBook(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
        )

    def get_arb_market(self, market: dict) -> Optional[ArbMarket]:
        """Build an ArbMarket from a raw market dict, or None if books are unavailable."""
        tokens = market.get("tokens", [])
        if len(tokens) != 2:
            return None

        # Find YES and NO tokens
        yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
        no_token = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), None)
        if not yes_token or not no_token:
            return None

        yes_book = self.get_order_book(yes_token["token_id"])
        no_book = self.get_order_book(no_token["token_id"])

        if not yes_book or not no_book:
            return None
        if yes_book.best_ask <= 0 or no_book.best_ask <= 0:
            return None

        return ArbMarket(
            condition_id=market.get("condition_id", ""),
            question=market.get("question", ""),
            yes_token_id=yes_token["token_id"],
            no_token_id=no_token["token_id"],
            yes_book=yes_book,
            no_book=no_book,
        )

    def place_market_order(
        self, token_id: str, amount_usdc: float, side: str = "BUY"
    ) -> Optional[dict]:
        """
        Place a market order.
        side: "BUY" to go long on a token.
        Returns order response dict or None on failure.
        """
        if config.DRY_RUN:
            logger.info("[DRY RUN] Would place %s %.4f USDC on token %s", side, amount_usdc, token_id)
            return {"status": "dry_run", "token_id": token_id, "amount": amount_usdc}

        client = self._get_client()
        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount_usdc,
            )
            signed = client.create_market_order(order_args)
            resp = client.post_order(signed, OrderType.FOK)
            return resp
        except Exception as exc:
            logger.error("place_market_order error: %s", exc)
            return None


polymarket = PolymarketClient()
