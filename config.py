import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Binance API
    API_KEY: str = os.getenv("BINANCE_API_KEY", "")
    API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")

    # Grid parameters
    SYMBOL: str = os.getenv("SYMBOL", "BTCUSDT")
    UPPER_PRICE: float = float(os.getenv("UPPER_PRICE", "100000"))
    LOWER_PRICE: float = float(os.getenv("LOWER_PRICE", "80000"))
    GRID_COUNT: int = int(os.getenv("GRID_COUNT", "10"))
    USDT_PER_GRID: float = float(os.getenv("USDT_PER_GRID", "10.0"))

    # Safety
    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() != "false"
    MAX_LOSS_USDT: float = float(os.getenv("MAX_LOSS_USDT", "50.0"))
    POLL_INTERVAL_SECONDS: float = float(os.getenv("POLL_INTERVAL_SECONDS", "5"))

    def validate(self) -> None:
        if self.UPPER_PRICE <= self.LOWER_PRICE:
            raise ValueError("UPPER_PRICE must be greater than LOWER_PRICE")
        if self.GRID_COUNT < 2:
            raise ValueError("GRID_COUNT must be at least 2")
        if self.USDT_PER_GRID <= 0:
            raise ValueError("USDT_PER_GRID must be positive")
        if not self.DRY_RUN:
            if not self.API_KEY or not self.API_SECRET:
                raise EnvironmentError(
                    "Live trading requires BINANCE_API_KEY and BINANCE_API_SECRET"
                )

    @property
    def grid_spacing(self) -> float:
        return (self.UPPER_PRICE - self.LOWER_PRICE) / self.GRID_COUNT

    @property
    def total_usdt_required(self) -> float:
        """Rough estimate: buy orders on all levels below current price."""
        return self.USDT_PER_GRID * self.GRID_COUNT


config = Config()
