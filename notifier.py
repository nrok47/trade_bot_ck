"""
Telegram Notifier
=================
ส่งแจ้งเตือนผ่าน Telegram Bot API

การตั้งค่า (ใน .env):
  TELEGRAM_BOT_TOKEN  — token จาก @BotFather
  TELEGRAM_CHAT_ID    — chat/user ID (ใช้ @userinfobot เพื่อหา)

ถ้าไม่ได้ตั้งค่า → ข้าม (ไม่ error) ระบบยังทำงานได้ปกติ
"""
from __future__ import annotations

import logging
import urllib.request
import urllib.parse
import urllib.error
import json

logger = logging.getLogger(__name__)

_ENABLED = False
_BOT_TOKEN: str = ""
_CHAT_ID: str = ""


def configure(bot_token: str, chat_id: str) -> None:
    """เรียกครั้งเดียวตอนเริ่มบอท เพื่อตั้งค่า Telegram credentials."""
    global _ENABLED, _BOT_TOKEN, _CHAT_ID
    if bot_token and chat_id:
        _BOT_TOKEN = bot_token
        _CHAT_ID = chat_id
        _ENABLED = True
        logger.info("Telegram notifications enabled (chat_id=%s)", chat_id)
    else:
        _ENABLED = False
        logger.info("Telegram not configured — notifications disabled")


def send_alert(message: str, silent: bool = False) -> bool:
    """
    ส่งข้อความแจ้งเตือนไป Telegram

    Args:
        message: ข้อความ (รองรับ Markdown)
        silent:  True = ไม่มีเสียง notification

    Returns:
        True ถ้าส่งสำเร็จ, False ถ้า error หรือไม่ได้ตั้งค่า
    """
    if not _ENABLED:
        return False

    url = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": _CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_notification": silent,
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                return True
            logger.warning("Telegram API returned status %d", resp.status)
            return False
    except urllib.error.URLError as exc:
        logger.warning("Telegram send failed (network): %s", exc)
        return False
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False


def alert_boundary_near(symbol: str, current_price: float, boundary: str,
                         dist_pct: float, upper: float, lower: float) -> None:
    """แจ้งเตือนเมื่อราคาใกล้ขอบกรอบ."""
    icon = "⚠️" if boundary == "UPPER" else "⚠️"
    arrow = "🔴 สูงขึ้น" if boundary == "UPPER" else "🟢 ลงต่ำ"
    msg = (
        f"{icon} *{symbol} ราคาใกล้ขอบ{boundary}*\n\n"
        f"ราคาปัจจุบัน: `${current_price:,.4f}`\n"
        f"ทิศทาง: {arrow}\n"
        f"ห่างจากขอบ: `{dist_pct:.2f}%`\n"
        f"กรอบ Grid: `${lower:,.4f}` – `${upper:,.4f}`\n\n"
        f"_บอทยังทำงานอยู่ แต่ควรติดตาม_"
    )
    send_alert(msg)


def alert_boundary_breached(symbol: str, current_price: float, boundary: str,
                              upper: float, lower: float) -> None:
    """แจ้งเตือนเมื่อราคาหลุดกรอบ — บอทหยุดแล้ว."""
    icon = "🚨"
    direction = "เหนือ UPPER" if boundary == "UPPER" else "ใต้ LOWER"
    msg = (
        f"{icon} *{symbol} ราคาหลุดกรอบ {direction}* — บอทหยุดแล้ว\n\n"
        f"ราคาปัจจุบัน: `${current_price:,.4f}`\n"
        f"กรอบ Grid: `${lower:,.4f}` – `${upper:,.4f}`\n\n"
        f"_กรุณาตรวจสอบ position และตั้งกรอบใหม่_"
    )
    send_alert(msg)


def alert_profit_target(symbol: str, profit_usdt: float, pct: float) -> None:
    """แจ้งเตือนเมื่อถึงเป้ากำไร."""
    msg = (
        f"🎯 *{symbol} ถึงเป้ากำไรวันนี้!*\n\n"
        f"กำไร: `${profit_usdt:.4f} USDT` (`{pct:.1f}%`)\n\n"
        f"_บอทหยุดทำงานสำหรับวันนี้_"
    )
    send_alert(msg)


def alert_max_loss(symbol: str, loss_usdt: float, max_loss: float) -> None:
    """แจ้งเตือนเมื่อขาดทุนเกินลิมิต."""
    msg = (
        f"🛑 *{symbol} ขาดทุนเกินลิมิต!*\n\n"
        f"ขาดทุน: `${abs(loss_usdt):.4f} USDT`\n"
        f"ลิมิต: `${max_loss:.2f} USDT`\n\n"
        f"_บอทหยุดอัตโนมัติ_"
    )
    send_alert(msg)


def alert_range_calculated(symbol: str, strategy: str, upper: float,
                             lower: float, atr_value: float = 0) -> None:
    """แจ้งเตือนเมื่อคำนวณกรอบใหม่ตอนเริ่มบอท."""
    atr_str = f"\nATR: `{atr_value:.4f}`" if atr_value else ""
    msg = (
        f"📐 *{symbol} คำนวณกรอบ Grid อัตโนมัติ*\n\n"
        f"Strategy: `{strategy}`{atr_str}\n"
        f"Upper: `${upper:,.4f}`\n"
        f"Lower: `${lower:,.4f}`\n"
        f"กว้าง: `${upper - lower:,.4f}`\n\n"
        f"_บอทเริ่มทำงานแล้ว_"
    )
    send_alert(msg)
