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
    # แนะนำ: TF 5m → 2 bars (10 min),  TF 15m → 2 bars,  TF 3m → 3 bars
    TREND_CONFIRM_BARS: int = int(os.getenv("TREND_CONFIRM_BARS", "2"))

    # รอขั้นต่ำกี่วินาทีก่อน reset grid อีกครั้ง (ป้องกัน reset ถี่เกินไป)
    # แนะนำ: ≥ 1 candle  → 5m=300, 15m=900, 3m=180
    MIN_RESET_INTERVAL_SECONDS: float = float(os.getenv("MIN_RESET_INTERVAL_SECONDS", "300"))

    # ── Auto Re-grid (re-center เมื่อราคาเลื่อนออกจากศูนย์กลาง) ──────────────
    # AUTO_REGRID=true → re-center grid เมื่อราคาเคลื่อนเข้าโซนขอบ (top/bottom threshold%)
    #   ปิดโดย default — เปิดเฉพาะเมื่อเข้าใจความเสี่ยงแล้ว
    AUTO_REGRID: bool = os.getenv("AUTO_REGRID", "false").lower() == "true"

    # เปอร์เซ็นต์โซนขอบที่ trigger re-grid (0.0–0.5)
    # 0.25 = trigger เมื่อราคาอยู่ใน top/bottom 25% ของกรอบ
    # ต่ำกว่า = re-grid บ่อยกว่า  สูงกว่า = รอนานกว่า (แนะนำ 0.20–0.30)
    REGRID_THRESHOLD: float = float(os.getenv("REGRID_THRESHOLD", "0.25"))

    # ── Dynamic Range (ATR-based auto initialization) ─────────────────────────
    # AUTO_RANGE=true → คำนวณ Upper/Lower จาก ATR อัตโนมัติตอนเริ่มบอท
    #                    (ไม่ต้องตั้ง UPPER_PRICE / LOWER_PRICE เอง)
    AUTO_RANGE: bool = os.getenv("AUTO_RANGE", "false").lower() == "true"

    # กลยุทธ์คำนวณกรอบ: "atr" | "bollinger" | "lookback" | "manual"
    # แนะนำ: "lookback" สำหรับ TF สั้น (3m/5m/15m) — ใช้ High/Low จริงของ 24h
    #         "atr" เหมาะกับ TF ยาว (1h+) เท่านั้น เพราะ ATR_MULTIPLIER ต้องปรับตาม TF
    RANGE_STRATEGY: str = os.getenv("RANGE_STRATEGY", "lookback").lower()

    # ATR settings (ใช้เมื่อ RANGE_STRATEGY=atr)
    ATR_PERIOD: int = int(os.getenv("ATR_PERIOD", "14"))
    ATR_MULTIPLIER: float = float(os.getenv("ATR_MULTIPLIER", "2.0"))

    # Bollinger Bands settings (ใช้เมื่อ RANGE_STRATEGY=bollinger)
    BB_PERIOD: int = int(os.getenv("BB_PERIOD", "20"))
    BB_STD: float = float(os.getenv("BB_STD", "2.0"))

    # Lookback settings (ใช้เมื่อ RANGE_STRATEGY=lookback)
    LOOKBACK_BARS: int = int(os.getenv("LOOKBACK_BARS", "288"))  # 288×5m = 24h  (96×15m = 24h, 480×3m = 24h)
    BUFFER_PCT: float = float(os.getenv("BUFFER_PCT", "0.05"))  # 5% buffer

    # ── Boundary Alert & Auto-Stop ────────────────────────────────────────────
    # แจ้งเตือนเมื่อราคาใกล้ขอบกรอบภายใน % นี้ (ค่าเริ่มต้น 3%)
    PRICE_ALERT_PCT: float = float(os.getenv("PRICE_ALERT_PCT", "3.0"))

    # หยุดบอทเมื่อราคาหลุดกรอบออกไป % นี้ (ค่าเริ่มต้น 5%)
    AUTO_STOP_PCT: float = float(os.getenv("AUTO_STOP_PCT", "5.0"))

    # ── Proxy (ใช้เมื่อ Binance บล็อก IP ของ server) ─────────────────────────
    # ตัวอย่าง: http://user:pass@host:port  หรือ  socks5://host:port
    PROXY_URL: str = os.getenv("PROXY_URL", "")

    # ── Telegram Notifications ────────────────────────────────────────────────
    # ดู README หรือ .env.example สำหรับวิธีหา token และ chat_id
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Profit targets & safety ────────────────────────────────────────────────
    # เป้ากำไรต่อวัน (% ของทุน) — หยุดบอทเมื่อถึง
    DAILY_PROFIT_TARGET_PCT: float = float(os.getenv("DAILY_PROFIT_TARGET_PCT", "33"))

    # ยอมขาดทุนต่อวันได้กี่ % ของทุน — ควรตั้งเท่ากับ DAILY_PROFIT_TARGET_PCT
    # เช่น ทุน $20, ทั้งคู่ 33% → กำไรเป้า $6.60  ขาดทุนสูงสุด $6.60
    MAX_LOSS_PCT: float = float(os.getenv("MAX_LOSS_PCT", "33"))

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
