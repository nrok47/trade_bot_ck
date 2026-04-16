import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Binance API
    API_KEY: str = os.getenv("BINANCE_API_KEY", "")
    API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")

    # Market type: "spot" or "futures"
    MARKET: str = os.getenv("MARKET", "spot").lower()

    # Grid parameters
    SYMBOL: str = os.getenv("SYMBOL", "BTCUSDT")
    UPPER_PRICE: float = float(os.getenv("UPPER_PRICE", "100000"))
    LOWER_PRICE: float = float(os.getenv("LOWER_PRICE", "80000"))
    GRID_COUNT: int = int(os.getenv("GRID_COUNT", "10"))
    USDT_PER_GRID: float = float(os.getenv("USDT_PER_GRID", "10.0"))

    # Futures-only settings
    LEVERAGE: int = int(os.getenv("LEVERAGE", "1"))
    MARGIN_TYPE: str = os.getenv("MARGIN_TYPE", "ISOLATED")   # ISOLATED | CROSSED

    # Safety
    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() != "false"
    MAX_LOSS_USDT: float = float(os.getenv("MAX_LOSS_USDT", "50.0"))
    POLL_INTERVAL_SECONDS: float = float(os.getenv("POLL_INTERVAL_SECONDS", "5"))

    @property
    def is_futures(self) -> bool:
        return self.MARKET == "futures"

    def validate(self) -> None:
        if self.MARKET not in ("spot", "futures"):
            raise ValueError("MARKET must be 'spot' or 'futures'")
        if self.UPPER_PRICE <= self.LOWER_PRICE:
            raise ValueError("UPPER_PRICE must be greater than LOWER_PRICE")
        if self.GRID_COUNT < 2:
            raise ValueError("GRID_COUNT must be at least 2")
        if self.USDT_PER_GRID <= 0:
            raise ValueError("USDT_PER_GRID must be positive")
        if self.is_futures:
            if not 1 <= self.LEVERAGE <= 125:
                raise ValueError("LEVERAGE must be between 1 and 125")
            if self.MARGIN_TYPE not in ("ISOLATED", "CROSSED"):
                raise ValueError("MARGIN_TYPE must be ISOLATED or CROSSED")
        if not self.DRY_RUN:
            if not self.API_KEY or not self.API_SECRET:
                raise EnvironmentError(
                    "Live trading requires BINANCE_API_KEY and BINANCE_API_SECRET"
                )

    @property
    def grid_spacing(self) -> float:
        return (self.UPPER_PRICE - self.LOWER_PRICE) / self.GRID_COUNT

    @property
    def notional_per_grid(self) -> float:
        """Effective order value (USDT) per grid after leverage."""
        return self.USDT_PER_GRID * (self.LEVERAGE if self.is_futures else 1)

    @property
    def total_margin_required(self) -> float:
        """Actual USDT needed in wallet (margin, not notional)."""
        return self.USDT_PER_GRID * self.GRID_COUNT

    @property
    def total_usdt_required(self) -> float:
        return self.total_margin_required

    def capital_warnings(self) -> list[str]:
        """Return human-readable warnings about capital requirements."""
        warnings = []
        if self.total_margin_required > 0:
            warnings.append(
                f"ทุนที่ต้องใช้ทั้งหมด: ${self.total_margin_required:.2f} USDT "
                f"(= {self.GRID_COUNT} grids × ${self.USDT_PER_GRID:.2f})"
            )
        if self.is_futures:
            warnings.append(
                f"Notional ต่อ grid: ${self.notional_per_grid:.2f} USDT "
                f"(margin ${self.USDT_PER_GRID:.2f} × {self.LEVERAGE}x)"
            )
            if self.notional_per_grid < 5:
                warnings.append(
                    f"⚠  Notional ${self.notional_per_grid:.2f} ต่ำเกินไป — "
                    "Binance กำหนด min $5 ต่อ order (ลด GRID_COUNT หรือเพิ่ม USDT_PER_GRID)"
                )
            if self.LEVERAGE > 10:
                warnings.append(
                    f"⚠  Leverage {self.LEVERAGE}x สูงมาก — "
                    "ราคาเคลื่อนที่ 10% ก็อาจโดน Liquidate"
                )
        return warnings


config = Config()
