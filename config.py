import os
from dotenv import load_dotenv

load_dotenv()

VALID_TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m")


class Config:
    # Binance API
    API_KEY: str = os.getenv("BINANCE_API_KEY", "")
    API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")

    # Market type: "spot" or "futures"
    MARKET: str = os.getenv("MARKET", "futures").lower()

    # Grid parameters
    SYMBOL: str = os.getenv("SYMBOL", "XRPUSDT")
    UPPER_PRICE: float = float(os.getenv("UPPER_PRICE", "2.40"))
    LOWER_PRICE: float = float(os.getenv("LOWER_PRICE", "1.60"))
    GRID_COUNT: int = int(os.getenv("GRID_COUNT", "5"))
    USDT_PER_GRID: float = float(os.getenv("USDT_PER_GRID", "4.0"))

    # Futures-only settings
    LEVERAGE: int = int(os.getenv("LEVERAGE", "5"))
    MARGIN_TYPE: str = os.getenv("MARGIN_TYPE", "ISOLATED")

    # ── Trend-following settings ───────────────────────────────────────────────
    # Timeframe สำหรับอ่าน candle: 1m, 3m, 5m, 15m, 30m
    TIMEFRAME: str = os.getenv("TIMEFRAME", "5m")

    # EMA periods
    EMA_SHORT: int = int(os.getenv("EMA_SHORT", "9"))
    EMA_LONG: int = int(os.getenv("EMA_LONG", "21"))

    # RSI period
    RSI_PERIOD: int = int(os.getenv("RSI_PERIOD", "14"))

    # Grid direction mode:
    #   "trend"  = ตาม bull/bear signal (แนะนำ)
    #   "both"   = grid สองทาง ไม่สนใจ trend (โหมดเดิม)
    GRID_MODE: str = os.getenv("GRID_MODE", "trend").lower()

    # ── Whipsaw Protection ────────────────────────────────────────────────────
    # จำนวน candle ที่ต้องเห็น signal เดิมติดต่อกัน ก่อนจะยอม reset grid
    # ยิ่งสูง = นิ่งขึ้น (reset น้อยลง) แต่ตอบสนองช้าลง
    # แนะนำ: TF 5m → 2-3 bars,  TF 15m → 2 bars
    TREND_CONFIRM_BARS: int = int(os.getenv("TREND_CONFIRM_BARS", "2"))

    # รอขั้นต่ำกี่วินาทีก่อน reset grid อีกครั้ง (ป้องกัน reset ถี่เกินไป)
    # แนะนำ: ≥ 1 เท่าของ TF (เช่น 5m → MIN_RESET_INTERVAL=300)
    MIN_RESET_INTERVAL_SECONDS: float = float(os.getenv("MIN_RESET_INTERVAL_SECONDS", "300"))

    # ── Profit targets & safety ────────────────────────────────────────────────
    # เป้ากำไรต่อวัน (% ของทุนทั้งหมด) — หยุดบอทเมื่อถึง
    DAILY_PROFIT_TARGET_PCT: float = float(os.getenv("DAILY_PROFIT_TARGET_PCT", "33"))

    # หยุดถ้าขาดทุนเกินนี้ (USDT)
    MAX_LOSS_USDT: float = float(os.getenv("MAX_LOSS_USDT", "20.0"))

    # DRY_RUN=true = จำลองเท่านั้น ไม่ส่ง order จริง
    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() != "false"

    # ตรวจสอบ order / re-analyze trend ทุกกี่วินาที
    POLL_INTERVAL_SECONDS: float = float(os.getenv("POLL_INTERVAL_SECONDS", "5"))

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
            f"เป้ากำไร/วัน: {self.DAILY_PROFIT_TARGET_PCT:.0f}%  "
            f"= ${self.daily_profit_target_usdt:.2f} USDT"
        )
        return warnings


config = Config()
