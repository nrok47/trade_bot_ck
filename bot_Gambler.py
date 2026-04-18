"""
Gambler Bot — Directional swing trader (LONG / SHORT)
=====================================================
Strategy: Multi-TF confluence scoring → binary entry → hold until TP or SL

Signals used (3m + 5m + 15m, last 12h):
  • EMA 9/21  — trend direction per TF
  • RSI 14    — oversold / overbought
  • Bollinger — band touch / breakout
  • Volume    — surge amplifier (×1.3 when current vol > 2× avg)
  • ATR 14    — sets minimum TP distance

Money Management:
  Capital   = 30% of USDT balance
  Leverage  = 8×
  Entry     = Market Order (Taker)
  TP        = 30% ROE  (price move = 30%/8 + round-trip fee = ~3.83%)
  SL soft   = 10% ROE  → re-check trend → close if invalidated
  SL hard   = 30% ROE  → force close always

Cooldown: 5 min after any close before re-entering.

Usage:
  python bot_Gambler.py               # DRY_RUN follows .env
  python bot_Gambler.py --dry-run     # force dry run
  python bot_Gambler.py --symbol DOGEUSDT
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from binance.client import Client
from binance.exceptions import BinanceAPIException

from binance_client import binance
from config import config
from indicators import (
    CandleData, ema, rsi as calc_rsi, atr as calc_atr, bollinger_bands,
)

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE = "gambler_bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("gambler")

# ── Constants ─────────────────────────────────────────────────────────────────
LEVERAGE         = 8
CAPITAL_PCT      = 0.30      # 30% of free balance
TAKER_FEE        = 0.0004    # 0.04% per leg (USDT-M futures taker)
TP_ROE_PCT       = 30.0      # target ROE%
SL_SOFT_ROE_PCT  = 10.0      # soft stop ROE% → trend re-check
SL_HARD_ROE_PCT  = 30.0      # hard stop ROE% → force close
POLL_INTERVAL    = 30        # seconds
SCORE_THRESHOLD  = 3.5       # minimum |score| to enter (out of ~11 max)
COOLDOWN_SECS    = 300       # 5 min between trades after any close
STATE_FILE       = "gambler_state.json"
LIVE_FILE        = "gambler_live.json"
COPILOT_ENABLED  = os.getenv("COPILOT_ENABLED", "true").lower() != "false"

# ── Dynamic profit-taking (when trend changes, don't wait for full TP) ───────
# Trailing stop: once ROE peaked at >= TRAIL_ACTIVATE, exit when we give back
# TRAIL_GIVEBACK_PCT of the peak. Locks in gains without closing too early.
TRAIL_ACTIVATE_ROE   = 10.0   # activate trailing after peak hits +10% ROE
TRAIL_GIVEBACK_PCT   = 0.40   # close when current drops 40% below peak
# Early TP on trend flip: if in profit and signal flips to opposite side,
# take the money — don't sit through the reversal waiting for full 30% TP.
MIN_ROE_FOR_FLIP_EXIT = 5.0   # require at least +5% ROE before honouring flip

# ── Signal thresholds (independent from grid bot config) ──────────────────────
# EMA gap: lower than grid bot (0.1%) — grid needs stricter filter to avoid resets;
# gambler only needs to detect trend, 0.03% is enough to avoid flat-line noise.
EMA_GAP_PCT      = 0.03   # % gap EMA_SHORT vs EMA_LONG to count as trend signal
RSI_OVERSOLD     = 40     # RSI below this → bullish pressure   (grid uses 35, too strict)
RSI_OVERBOUGHT   = 60     # RSI above this → bearish pressure   (grid uses 65, too strict)
VOL_SURGE_MULT   = 2.0    # volume must be N× 20-bar avg to count as surge

# TF config: limit = 12h of candles  |  weight: 15m counts most
TF_CONFIG: dict[str, dict] = {
    "3m":  {"limit": 240, "weight": 1.0},
    "5m":  {"limit": 144, "weight": 1.5},
    "15m": {"limit":  48, "weight": 2.0},
}


# ── Position dataclass ────────────────────────────────────────────────────────

@dataclass
class Position:
    side: str            # "LONG" | "SHORT"
    entry_price: float
    qty: float
    margin: float        # USDT margin committed
    tp_price: float
    sl_soft_price: float
    sl_hard_price: float
    entry_time: float    # unix timestamp
    peak_roe: float = 0.0   # highest ROE reached during this position's lifetime

    def roe(self, price: float) -> float:
        """Unrealised ROE % (excludes fees)."""
        pnl = (price - self.entry_price) if self.side == "LONG" else (self.entry_price - price)
        return pnl * self.qty / self.margin * 100 if self.margin else 0.0

    def save(self) -> None:
        with open(STATE_FILE, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls) -> Optional["Position"]:
        if not os.path.exists(STATE_FILE):
            return None
        try:
            with open(STATE_FILE) as f:
                d = json.load(f)
            # tolerate older state files missing newer fields
            d.setdefault("peak_roe", 0.0)
            p = cls(**d)
            logger.info("Restored position: %s @ %.4f  qty=%.4f  peak_roe=%.2f%%",
                        p.side, p.entry_price, p.qty, p.peak_roe)
            return p
        except Exception as exc:
            logger.warning("Cannot restore state: %s", exc)
            return None

    @classmethod
    def clear(cls) -> None:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)


# ── Scoring per timeframe ─────────────────────────────────────────────────────

def _score_tf(candles: CandleData, weight: float) -> tuple[float, dict]:
    """
    Score one TF against 4 signals.
    Returns (weighted_score, info_dict).
    Max raw score ≈ (1.0 + 0.8 + 0.7) × weight × 1.3 (surge)
    """
    closes  = candles.closes
    highs   = candles.highs
    lows    = candles.lows
    volumes = candles.volumes
    price   = closes[-1]
    score   = 0.0
    info: dict = {}

    # 1. EMA 9/21 trend
    ema_s_vals = ema(closes, config.EMA_SHORT)
    ema_l_vals = ema(closes, config.EMA_LONG)
    if ema_s_vals and ema_l_vals:
        es, el = ema_s_vals[-1], ema_l_vals[-1]
        gap = (es - el) / el * 100 if el else 0
        if gap > EMA_GAP_PCT:
            score += 1.0 * weight
            info["ema"] = f"BULL gap={gap:.3f}%"
        elif gap < -EMA_GAP_PCT:
            score -= 1.0 * weight
            info["ema"] = f"BEAR gap={gap:.3f}%"
        else:
            info["ema"] = f"NEUTRAL gap={gap:.3f}%"

    # 2. RSI
    rsi_val = calc_rsi(closes, config.RSI_PERIOD)
    if rsi_val < RSI_OVERSOLD:
        score += 0.8 * weight
        info["rsi"] = f"OVERSOLD {rsi_val:.1f}"
    elif rsi_val > RSI_OVERBOUGHT:
        score -= 0.8 * weight
        info["rsi"] = f"OVERBOUGHT {rsi_val:.1f}"
    else:
        info["rsi"] = f"neutral {rsi_val:.1f}"

    # 3. Bollinger Bands (20, 2σ)
    bb = bollinger_bands(closes, 20, 2.0)
    if price < bb.lower:
        score += 0.7 * weight
        info["bb"] = f"BELOW_LOWER bw={bb.bandwidth:.4f}"
    elif price > bb.upper:
        score -= 0.7 * weight
        info["bb"] = f"ABOVE_UPPER bw={bb.bandwidth:.4f}"
    else:
        pos_pct = (price - bb.lower) / (bb.upper - bb.lower) * 100 if bb.upper != bb.lower else 50
        info["bb"] = f"inside {pos_pct:.0f}% bw={bb.bandwidth:.4f}"

    # 4. Volume Surge — amplify score if current bar's volume > 2× avg of prev 20
    vol_ratio = 1.0
    if len(volumes) >= 22:
        avg_vol = sum(volumes[-21:-1]) / 20
        if avg_vol > 0:
            vol_ratio = volumes[-1] / avg_vol
            if vol_ratio >= VOL_SURGE_MULT:
                score *= 1.3
                info["vol"] = f"SURGE x{vol_ratio:.1f}"
            else:
                info["vol"] = f"normal x{vol_ratio:.1f}"

    # ATR (info only — used for min TP distance)
    atr_val = calc_atr(highs, lows, closes, 14)
    info["atr"] = round(atr_val, 6)

    return score, info


def compute_signal(symbol: str) -> tuple[str, float, dict, float]:
    """
    Fetch all TFs, aggregate scores, return:
      (direction, total_score, details_per_tf, atr_5m)
    direction: "LONG" | "SHORT" | "SKIP"
    """
    total = 0.0
    details: dict = {}
    atr_5m = 0.0

    for tf, cfg in TF_CONFIG.items():
        candles = binance.get_klines(symbol, tf, cfg["limit"])
        if not candles or len(candles.closes) < 30:
            logger.warning("Insufficient candles: %s %s (%d)",
                           symbol, tf, len(candles.closes) if candles else 0)
            continue
        sc, info = _score_tf(candles, cfg["weight"])
        total += sc
        details[tf] = {"score": round(sc, 3), **info}
        if tf == "5m":
            atr_5m = info.get("atr", 0.0)

    if total >= SCORE_THRESHOLD:
        direction = "LONG"
    elif total <= -SCORE_THRESHOLD:
        direction = "SHORT"
    else:
        direction = "SKIP"

    return direction, total, details, atr_5m


# ── TP / SL levels ────────────────────────────────────────────────────────────

def calc_levels(entry: float, side: str, atr_val: float = 0.0) -> tuple[float, float, float]:
    """
    Compute TP, SL_soft, SL_hard as absolute prices.

    TP price move = TP_ROE% / leverage + round-trip fee%
      e.g. 30%/8 + 0.08% = 3.83%
    ATR override: if 1.5×ATR > computed TP move, use ATR (avoid stop hunts).
    """
    fee_pct      = TAKER_FEE * 2          # two legs
    tp_move      = TP_ROE_PCT      / 100 / LEVERAGE + fee_pct
    sl_soft_move = SL_SOFT_ROE_PCT / 100 / LEVERAGE
    sl_hard_move = SL_HARD_ROE_PCT / 100 / LEVERAGE

    if atr_val > 0:
        atr_move = atr_val / entry * 1.5
        tp_move = max(tp_move, atr_move)   # at least 1.5× ATR for TP

    if side == "LONG":
        return (
            entry * (1 + tp_move),
            entry * (1 - sl_soft_move),
            entry * (1 - sl_hard_move),
        )
    else:
        return (
            entry * (1 - tp_move),
            entry * (1 + sl_soft_move),
            entry * (1 + sl_hard_move),
        )


# ── Order helpers ─────────────────────────────────────────────────────────────

def _round_qty(qty: float) -> float:
    info = binance.get_symbol_info()
    return math.floor(qty * 10 ** info.qty_precision) / 10 ** info.qty_precision


def enter_market(side: str, margin: float, ref_price: float, dry_run: bool) -> Optional[tuple[float, float]]:
    """
    Place MARKET order.  Returns (fill_price, filled_qty) or None on failure.
    """
    notional = margin * LEVERAGE
    qty = _round_qty(notional / ref_price)
    if qty <= 0:
        logger.error("Qty rounds to 0 — min notional issue (margin=$%.2f notional=$%.2f)", margin, notional)
        return None

    if dry_run:
        logger.info("[DRY] MARKET %s  %.4f %s  margin=$%.2f  notional=$%.2f",
                    side, qty, config.SYMBOL, margin, notional)
        return ref_price, qty

    try:
        order_side = Client.SIDE_BUY if side == "LONG" else Client.SIDE_SELL
        resp = binance._client.futures_create_order(
            symbol=config.SYMBOL,
            side=order_side,
            type=Client.FUTURE_ORDER_TYPE_MARKET,
            quantity=qty,
        )
        fill = float(resp.get("avgPrice") or ref_price)
        filled_qty = float(resp.get("executedQty") or qty)
        logger.info("MARKET %s filled  qty=%.4f @ %.4f", side, filled_qty, fill)
        return fill, filled_qty
    except BinanceAPIException as exc:
        logger.error("enter_market error: %s", exc)
        return None


def close_market(pos: Position, reason: str, dry_run: bool) -> None:
    """Close open position with reduceOnly market order."""
    if dry_run:
        logger.info("[DRY] CLOSE %s  qty=%.4f  reason=%s", pos.side, pos.qty, reason)
        return
    try:
        close_side = Client.SIDE_SELL if pos.side == "LONG" else Client.SIDE_BUY
        binance._client.futures_create_order(
            symbol=config.SYMBOL,
            side=close_side,
            type=Client.FUTURE_ORDER_TYPE_MARKET,
            quantity=_round_qty(pos.qty),
            reduceOnly=True,
        )
        logger.info("CLOSED %s  qty=%.4f  reason=%s", pos.side, pos.qty, reason)
    except BinanceAPIException as exc:
        logger.error("close_market error: %s", exc)


def setup_leverage(dry_run: bool) -> None:
    if dry_run or not config.is_futures:
        return
    try:
        binance._client.futures_change_leverage(symbol=config.SYMBOL, leverage=LEVERAGE)
        logger.info("Leverage set to %dx", LEVERAGE)
    except BinanceAPIException as exc:
        logger.warning("set leverage: %s", exc)
    try:
        binance._client.futures_change_margin_type(symbol=config.SYMBOL, marginType="ISOLATED")
        logger.info("Margin type: ISOLATED")
    except BinanceAPIException as exc:
        if "4046" not in str(exc):
            logger.warning("set margin type: %s", exc)


# ── Dashboard ─────────────────────────────────────────────────────────────────

def _bar(score: float, width: int = 20) -> str:
    """Simple ASCII score bar centered at 0."""
    half = width // 2
    filled = int(abs(score) / SCORE_THRESHOLD * half)
    filled = min(filled, half)
    if score >= 0:
        return " " * half + "█" * filled + " " * (half - filled)
    else:
        return " " * (half - filled) + "█" * filled + " " * half


def _save_live(price: float, direction: str, score: float, details: dict,
               cooldown_left: float, copilot: Optional[dict] = None) -> None:
    """Write lightweight state snapshot for the web dashboard."""
    try:
        data: dict = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": config.SYMBOL,
            "price": price,
            "direction": direction,
            "score": round(score, 3),
            "cooldown_left": round(cooldown_left, 0),
            "details": {tf: {k: v for k, v in d.items()} for tf, d in details.items()},
        }
        if copilot:
            data["copilot"] = copilot
        with open(LIVE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as exc:
        logger.debug("_save_live error: %s", exc)


def print_dashboard(pos: Optional[Position], price: float, score: float,
                    direction: str, details: dict, cooldown_left: float) -> None:
    ts = time.strftime("%H:%M:%S")
    arrow = {"LONG": "▲", "SHORT": "▼", "SKIP": "◆"}.get(direction, "?")
    bar = _bar(score)

    print(f"\n{'='*62}")
    print(f"  {ts}  {config.SYMBOL}  ${price:.4f}  [GAMBLER 8x  cap={CAPITAL_PCT*100:.0f}%]")
    print(f"  Score [{bar}] {score:+.2f}  →  {arrow} {direction}")
    if cooldown_left > 0:
        print(f"  Cooldown: {cooldown_left:.0f}s remaining")

    if pos:
        roe = pos.roe(price)
        roe_bar = "+" * max(0, int(roe / 5)) if roe >= 0 else "-" * max(0, int(-roe / 5))
        print(f"  OPEN {pos.side} @ {pos.entry_price:.4f}  qty={pos.qty:.4f}"
              f"  ROE={roe:+.2f}% [{roe_bar}]  peak={pos.peak_roe:+.2f}%")
        print(f"  TP={pos.tp_price:.4f}  SL_soft={pos.sl_soft_price:.4f}  SL_hard={pos.sl_hard_price:.4f}")
        # show trailing stop trigger level once armed
        if pos.peak_roe >= TRAIL_ACTIVATE_ROE:
            trail_roe = pos.peak_roe * (1 - TRAIL_GIVEBACK_PCT)
            print(f"  Trailing armed: exit if ROE drops below {trail_roe:+.2f}%")
    else:
        print(f"  No open position")

    print(f"  ── TF breakdown ──────────────────────────────────────────")
    for tf, d in details.items():
        wt = TF_CONFIG[tf]["weight"]
        print(f"  {tf} (w={wt:.1f}) score={d.get('score',0):+.3f}"
              f"  EMA:{d.get('ema','?')[:16]}  RSI:{d.get('rsi','?')[:18]}")
        print(f"          BB:{d.get('bb','?')[:22]}  vol:{d.get('vol','?')[:14]}  ATR={d.get('atr',0):.5f}")
    print(f"{'='*62}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(symbol: str, dry_run: bool) -> None:
    logger.info("=== Gambler Bot  symbol=%s  8x  capital=%.0f%%  dry=%s ===",
                symbol, CAPITAL_PCT * 100, dry_run)
    logger.info("TP=%.0f%% ROE  SL_soft=%.0f%%  SL_hard=%.0f%%  fee=%.2f%%/leg  threshold=%.1f",
                TP_ROE_PCT, SL_SOFT_ROE_PCT, SL_HARD_ROE_PCT, TAKER_FEE * 100, SCORE_THRESHOLD)

    setup_leverage(dry_run)
    pos: Optional[Position] = Position.load()
    last_close_time: float = 0.0
    skip_streak = 0

    while True:
        try:
            price   = binance.get_price()
            direction, score, details, atr_5m = compute_signal(symbol)
            cooldown_left = max(0.0, COOLDOWN_SECS - (time.time() - last_close_time))

            # ── Manage existing position ──────────────────────────────────
            if pos is not None:
                roe = pos.roe(price)
                # track running peak so trailing / flip rules have reference
                if roe > pos.peak_roe:
                    pos.peak_roe = roe
                    pos.save()

                # Hard SL — always close
                hard_hit = (pos.side == "LONG"  and price <= pos.sl_hard_price) or \
                           (pos.side == "SHORT" and price >= pos.sl_hard_price)
                if hard_hit:
                    logger.warning("HARD SL  ROE=%.2f%%  price=%.4f", roe, price)
                    close_market(pos, f"HARD_SL({roe:.1f}%)", dry_run)
                    pos = None; Position.clear(); last_close_time = time.time()

                # TP hit
                elif (pos.side == "LONG"  and price >= pos.tp_price) or \
                     (pos.side == "SHORT" and price <= pos.tp_price):
                    logger.info("TP hit!  ROE=%.2f%%  price=%.4f", roe, price)
                    close_market(pos, f"TP({roe:.1f}%)", dry_run)
                    pos = None; Position.clear(); last_close_time = time.time()

                # Trailing stop — once peak hit activation, lock-in gains on pullback
                elif pos.peak_roe >= TRAIL_ACTIVATE_ROE and \
                     roe <= pos.peak_roe * (1 - TRAIL_GIVEBACK_PCT):
                    giveback = pos.peak_roe - roe
                    logger.info(
                        "TRAIL_STOP  peak=%.2f%%  roe=%.2f%%  giveback=%.2f%%",
                        pos.peak_roe, roe, giveback,
                    )
                    close_market(pos, f"TRAIL(peak={pos.peak_roe:.1f}%→{roe:.1f}%)", dry_run)
                    pos = None; Position.clear(); last_close_time = time.time()

                # Early TP — trend flipped while in profit, take the money
                elif roe >= MIN_ROE_FOR_FLIP_EXIT and \
                     direction not in ("SKIP", pos.side):
                    logger.info(
                        "TREND_FLIP_TP  dir=%s vs pos=%s  score=%+.2f  ROE=%.2f%%",
                        direction, pos.side, score, roe,
                    )
                    close_market(pos, f"FLIP_TP({roe:.1f}%)", dry_run)
                    pos = None; Position.clear(); last_close_time = time.time()

                # Soft SL — re-check trend
                elif (pos.side == "LONG"  and price <= pos.sl_soft_price) or \
                     (pos.side == "SHORT" and price >= pos.sl_soft_price):
                    logger.warning("Soft SL  ROE=%.2f%%  re-checking trend...", roe)
                    trend_ok = (pos.side == direction)  # signal still agrees?
                    if trend_ok:
                        logger.info("Trend holds — staying in position  ROE=%.2f%%", roe)
                    else:
                        logger.warning("Trend gone — closing  ROE=%.2f%%", roe)
                        close_market(pos, f"SOFT_SL+FLIP({roe:.1f}%)", dry_run)
                        pos = None; Position.clear(); last_close_time = time.time()

                # Strong opposite signal — early exit (catches losses too)
                elif direction not in ("SKIP", pos.side) and abs(score) >= SCORE_THRESHOLD * 1.2:
                    logger.warning("Opposite signal (%s score=%.2f) — early exit  ROE=%.2f%%",
                                   direction, score, roe)
                    close_market(pos, f"EARLY_EXIT({roe:.1f}%)", dry_run)
                    pos = None; Position.clear(); last_close_time = time.time()

            # ── Enter new position ────────────────────────────────────────
            copilot_result: Optional[dict] = None
            if pos is None and direction != "SKIP" and cooldown_left == 0:
                balance = binance.get_balance("USDT")
                margin  = balance * CAPITAL_PCT

                # Co-pilot: scale margin by AI confidence (0.5× – 1.5×)
                if COPILOT_ENABLED:
                    try:
                        from ai_copilot import evaluate_trade as _cp_eval
                        copilot_result = _cp_eval(direction, score, details, price, atr_5m)
                        confidence = copilot_result.get("confidence", 60)
                        multiplier = 0.5 + (confidence / 100.0) * 1.0
                        margin = margin * multiplier
                        logger.info(
                            "CO-PILOT: confidence=%d%% quality=%s → margin=$%.2f (×%.2f) | %s",
                            confidence, copilot_result.get("quality", "?"),
                            margin, multiplier, copilot_result.get("reasoning", ""),
                        )
                    except Exception as cp_exc:
                        logger.warning("CO-PILOT failed: %s — using base margin", cp_exc)

                if margin * LEVERAGE < 5:
                    logger.warning("Notional too small ($%.2f) — need balance > $%.2f",
                                   margin * LEVERAGE, 5 / LEVERAGE / CAPITAL_PCT)
                else:
                    result = enter_market(direction, margin, price, dry_run)
                    if result:
                        fill_price, filled_qty = result
                        tp, sl_soft, sl_hard = calc_levels(fill_price, direction, atr_5m)
                        pos = Position(
                            side=direction,
                            entry_price=fill_price,
                            qty=filled_qty,
                            margin=margin,
                            tp_price=tp,
                            sl_soft_price=sl_soft,
                            sl_hard_price=sl_hard,
                            entry_time=time.time(),
                        )
                        pos.save()
                        tp_pct  = abs(tp - fill_price) / fill_price * 100
                        slh_pct = abs(sl_hard - fill_price) / fill_price * 100
                        logger.info(
                            "ENTERED %s @ %.4f  TP=%.4f (+%.2f%% price / +%.0f%% ROE)"
                            "  SL_hard=%.4f (-%.2f%% price / -%.0f%% ROE)",
                            direction, fill_price,
                            tp,  tp_pct,  tp_pct  * LEVERAGE,
                            sl_hard, slh_pct, slh_pct * LEVERAGE,
                        )
                        skip_streak = 0
            elif direction == "SKIP" or cooldown_left > 0:
                skip_streak += 1
                if skip_streak % 6 == 1:
                    reason = f"cooldown {cooldown_left:.0f}s" if cooldown_left > 0 else f"score={score:+.2f}"
                    logger.info("Waiting: %s  (streak=%d)", reason, skip_streak)

            _save_live(price, direction, score, details, cooldown_left, copilot_result)
            print_dashboard(pos, price, score, direction, details, cooldown_left)

        except KeyboardInterrupt:
            logger.info("Interrupted")
            if pos and not dry_run:
                logger.warning("! Open position exists — check Binance manually: %s @ %.4f qty=%.4f",
                               pos.side, pos.entry_price, pos.qty)
            break
        except Exception as exc:
            logger.error("Unexpected error: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gambler Bot — directional swing trader")
    parser.add_argument("--symbol",  default=config.SYMBOL, help="Symbol (default from .env)")
    parser.add_argument("--dry-run", action="store_true",   help="No real orders")
    args = parser.parse_args()

    run(args.symbol, args.dry_run or config.DRY_RUN)
