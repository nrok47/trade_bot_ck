import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"Missing required env var: {key}")
    return val


class Config:
    # Wallet
    PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")

    # Polymarket API credentials
    API_KEY: str = os.getenv("POLYMARKET_API_KEY", "")
    API_SECRET: str = os.getenv("POLYMARKET_API_SECRET", "")
    API_PASSPHRASE: str = os.getenv("POLYMARKET_API_PASSPHRASE", "")

    # Polymarket CLOB endpoint (Polygon mainnet)
    CLOB_HOST: str = "https://clob.polymarket.com"
    CHAIN_ID: int = 137  # Polygon mainnet

    # Bot behaviour
    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() != "false"
    MIN_PROFIT_USDC: float = float(os.getenv("MIN_PROFIT_USDC", "0.05"))
    MAX_TRADE_SIZE_USDC: float = float(os.getenv("MAX_TRADE_SIZE_USDC", "10.0"))
    MAX_OPEN_POSITIONS_USDC: float = float(os.getenv("MAX_OPEN_POSITIONS_USDC", "50.0"))
    SCAN_INTERVAL_SECONDS: float = float(os.getenv("SCAN_INTERVAL_SECONDS", "5"))
    TAKER_FEE_RATE: float = float(os.getenv("TAKER_FEE_RATE", "0.0"))

    def validate_for_live_trading(self) -> None:
        """Raise if any required credential is missing (called only when DRY_RUN=false)."""
        missing = []
        for attr in ("PRIVATE_KEY", "API_KEY", "API_SECRET", "API_PASSPHRASE"):
            if not getattr(self, attr):
                missing.append(attr)
        if missing:
            raise EnvironmentError(
                f"Live trading requires these env vars: {', '.join(missing)}"
            )


config = Config()
