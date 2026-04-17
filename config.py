import os
from dotenv import load_dotenv

load_dotenv()

VALID_TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m")


def _float(key: str, default: str) -> float:
    """อ่าน env var แล้ว convert เป็น float — ถ้าว่างหรือ invalid ใช้ default"""
    return float(os.getenv(key, default) or default)


def _int(key: str, default: str) -> int:
    """อ่าน env var แล้ว convert เป็น int — ถ้าว่างหรือ invalid ใช้ default"""
    return int(os.getenv(key, default) or default)


class Config:
    # Binance API
    API_KEY: str = os.getenv("BINANCE_API_KEY", "")
    API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")

    # Market type: "spot" or "futures"
    MARKET: str = (os.getenv("MARKET", "futures") or "futures").lower()

    # Grid parameters
    SYMBOL: str = os.getenv("SYMBOL", "XRPUSDT") or "XRPUSDT"
    UPPER_PRICE: float = _float("UPPER_PRICE", "2.40")
    LOWER_PRICE: float = _float("LOWER_PRICE", "1.60")
    GRID_COUNT: int = _int("GRID_COUNT", "5")
    USDT_PER_GRID: float = _float("USDT_PER_GRID", "4.0")

    # Futures-only settings
    LEVERAGE: int = _int("LEVERAGE", "5")
    MARGIN_TYPE: str = os.getenv("MARGIN_TYPE", "ISOLATED") or "ISOLATED"

    # ── Trend-following settings ───────────────────────────────────────────────
    TIMEFRAME: str = os.getenv("TIMEFRAME", "5m") or "5m"

    # EMA periods
    EMA_SHORT: int = _int("EMA_SHORT", "9")
    EMA_LONG: int = _int("EMA_LONG", "21")

    # RSI period
    RSI_PERIOD: int = _int("RSI_PERIOD", "14")

    # % ขั้นต่ำที่ EMA short ต้องห่างจาก EMA long ถึงจะนับเป็น condition 1
    EMA_MIN_GAP_PCT: float = _float("EMA_MIN_GAP_PCT", "0.1")

    # Grid direction mode
    GRID_MODE: str = (os.getenv("GRID_MODE", "trend") or "trend").lower()

    # ── Stop Loss per grid ────────────────────────────────────────────────────
    STOP_LOSS_PCT: float = _float("STOP_LOSS_PCT", "0")

    # ── Whipsaw Protection ────────────────────────────────────────────────────
    TREND_CONFIRM_BARS: int = _int("TREND_CONFIRM_BARS", "2")
    MIN_RESET_INTERVAL_SECONDS: float = _float("MIN_RESET_INTERVAL_SECONDS", "300")

    # ── Auto Re-grid ──────────────────────────────────────────────────────────
    AUTO_REGRID: bool = (os.getenv("AUTO_REGRID", "false") or "false").lower() == "true"
    HOLD_POSITION_ON_RESET: bool = (os.getenv("HOLD_POSITION_ON_RESET", "false") or "false").lower() == "true"
    REGRID_THRESHOLD: float = _float("REGRID_THRESHOLD", "0.25")

    # ── Dynamic Range ─────────────────────────────────────────────────────────
    AUTO_RANGE: bool = (os.getenv("AUTO_RANGE", "false") or "false").lower() == "true"
    RANGE_STRATEGY: str = (os.getenv("RANGE_STRATEGY", "lookback") or "lookback").lower()

    ATR_PERIOD: int = _int("ATR_PERIOD", "14")
    ATR_MULTIPLIER: float = _float("ATR_MULTIPLIER", "2.0")

    BB_PERIOD: int = _int("BB_PERIOD", "20")
    BB_STD: float = _float("BB_STD", "2.0")

    LOOKBACK_BARS: int = _int("LOOKBACK_BARS", "288")
    BUFFER_PCT: float = _float("BUFFER_PCT", "0.05")

    # ── Boundary Alert & Auto-Stop ────────────────────────────────────────────
    PRICE_ALERT_PCT: float = _float("PRICE_ALERT_PCT", "3.0")
    AUTO_STOP_PCT: float = _float("AUTO_STOP_PCT", "5.0")

    # ── Proxy ─────────────────────────────────────────────────────────────────
    PROXY_URL: str = os.getenv("PROXY_URL", "")

    # ── Telegram ──────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Profit targets & safety ───────────────────────────────────────────────
    DAILY_PROFIT_TARGET_PCT: float = _float("DAILY_PROFIT_TARGET_PCT", "33")
    MAX_LOSS_PCT: float = _float("MAX_LOSS_PCT", "33")
    DRY_RUN: bool = (os.getenv("DRY_RUN", "true") or "true").lower() != "false"
    POLL_INTERVAL_SECONDS: float = _float("POLL_INTERVAL_SECONDS", "5")

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def is_futures(self) -> bool:
        return self.MARKET == "futures"

    @property
    def grid_spacing(self) -> float:
        return (self.UPPER_PRICE - self.LOWER_PRICE) / self.GRID_COUNT

    @property
    def notional_per_grid(self) -> float:
        return self.USDT_PER_GRID * (self.LEVERAGE if self.is_futures else 1)

    @property
    def total_margin_required(self) -> float:
        return self.USDT_PER_GRID * self.GRID_COUNT

    @property
    def daily_profit_target_usdt(self) -> float:
        """USDT กำไรที่ต้องการต่อวัน (คำนวณจาก % ของทุน)."""
        return self.total_margin_required * self.DAILY_PROFIT_TARGET_PCT / 100

    @property
    def daily_max_loss_usdt(self) -> float:
        """USDT ขาดทุนสูงสุดต่อวัน (สมมาตรกับ daily_profit_target_usdt)."""
        return self.total_margin_required * self.MAX_LOSS_PCT / 100

    # ── Validation ────────────────────────────────────────────────────────────

    def validate(self) -> None:
        if self.MARKET not in ("spot", "futures"):
            raise ValueError("MARKET must be 'spot' or 'futures'")
        if self.UPPER_PRICE <= self.LOWER_PRICE:
            raise ValueError("UPPER_PRICE must be greater than LOWER_PRICE")
        if self.GRID_COUNT < 2:
            raise ValueError("GRID_COUNT must be at least 2")
        if self.USDT_PER_GRID <= 0:
            raise ValueError("USDT_PER_GRID must be positive")
        if self.TIMEFRAME not in VALID_TIMEFRAMES:
            raise ValueError(f"TIMEFRAME must be one of {VALID_TIMEFRAMES}")
        if self.EMA_SHORT >= self.EMA_LONG:
            raise ValueError("EMA_SHORT must be less than EMA_LONG")
        if self.GRID_MODE not in ("trend", "both"):
            raise ValueError("GRID_MODE must be 'trend' or 'both'")
        if self.RANGE_STRATEGY not in ("atr", "bollinger", "lookback", "manual"):
            raise ValueError("RANGE_STRATEGY must be 'atr', 'bollinger', 'lookback', or 'manual'")
        if self.PRICE_ALERT_PCT <= 0:
            raise ValueError("PRICE_ALERT_PCT must be positive")
        if self.AUTO_STOP_PCT <= 0:
            raise ValueError("AUTO_STOP_PCT must be positive")
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

    def capital_warnings(self) -> list[str]:
        warnings = []
        warnings.append(
            f"ทุนรวม: ${self.total_margin_required:.2f} USDT  "
            f"({self.GRID_COUNT} grids × ${self.USDT_PER_GRID:.2f})"
        )
        if self.is_futures:
            warnings.append(
                f"Notional/grid: ${self.notional_per_grid:.2f} USDT  "
                f"(${self.USDT_PER_GRID:.2f} × {self.LEVERAGE}x)"
            )
            if self.notional_per_grid < 5:
                warnings.append(
                    f"⚠  Notional ${self.notional_per_grid:.2f} ต่ำเกินไป — "
                    "Binance min $5/order (ลด GRID_COUNT หรือเพิ่ม USDT_PER_GRID)"
                )
            if self.LEVERAGE > 10:
                warnings.append(
                    f"⚠  Leverage {self.LEVERAGE}x สูง — "
                    "ราคาเคลื่อนที่ 10% อาจโดน Liquidate"
                )
        warnings.append(
            f"เป้ากำไร/วัน : +{self.DAILY_PROFIT_TARGET_PCT:.0f}%"
            f"  = +${self.daily_profit_target_usdt:.2f} USDT"
        )
        warnings.append(
            f"ยอมขาดทุน/วัน: -{self.MAX_LOSS_PCT:.0f}%"
            f"  = -${self.daily_max_loss_usdt:.2f} USDT"
        )
        return warnings


config = Config()
