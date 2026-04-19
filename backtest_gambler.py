"""
Gambler Bot Backtester
======================
Simulates multi-TF confluence scoring → directional entry → TP/SL/trailing exit
using historical OHLCV data from Binance Futures (no API key needed).

Usage:
  python backtest_gambler.py                          # XRPUSDT, 14 days
  python backtest_gambler.py --days 30
  python backtest_gambler.py --symbol DOGEUSDT --days 7
  python backtest_gambler.py --threshold 4.0
  python backtest_gambler.py --tp 40 --sl 25

Output:
  gambler_backtest.json — trade log + metrics + equity curve
  (also printed as summary in terminal)
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
from datetime import datetime, timezone
from typing import Optional

from binance.client import Client

from indicators import CandleData, ema as _ema, rsi as _rsi, bollinger_bands

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Strategy defaults (same as bot_Gambler.py) ────────────────────────────────
LEVERAGE           = 8
SCORE_THRESHOLD    = 3.5
TP_ROE_PCT         = 30.0
SL_SOFT_ROE_PCT    = 10.0
SL_HARD_ROE_PCT    = 30.0
TAKER_FEE          = 0.0004
EMA_GAP_PCT        = 0.03
RSI_OVERSOLD       = 40
RSI_OVERBOUGHT     = 60
VOL_SURGE_MULT     = 2.0
TRAIL_ACTIVATE_ROE = 10.0
TRAIL_GIVEBACK_PCT = 0.40
MIN_ROE_FOR_FLIP   = 5.0
COOLDOWN_BARS      = 1      # 1 × 5m bar ≈ 5-min cooldown

TF_WEIGHTS = {"3m": 1.0, "5m": 1.5, "15m": 2.0}
TF_LIMITS  = {"3m": 240, "5m": 144, "15m":  48}
WARMUP_5M  = 150   # bars before simulation starts (enough for 15m × 48 + buffer)

OUTPUT_FILE = "gambler_backtest.json"


# ── Data fetch ────────────────────────────────────────────────────────────────

def _fetch(symbol: str, interval: str, days: int) -> tuple[CandleData, list[int]]:
    """Fetch historical OHLCV from Binance Futures (public, no key required)."""
    mins = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30}.get(interval, 5)
    total_needed = days * 24 * 60 // mins + 600  # extra for warmup

    client = Client()
    all_raw: list = []
    end_time: Optional[int] = None
    remaining = total_needed

    while remaining > 0:
        limit = min(remaining, 1500)
        kw: dict = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_time is not None:
            kw["endTime"] = end_time
        batch = client.futures_klines(**kw)
        if not batch:
            break
        all_raw = batch + all_raw
        remaining -= len(batch)
        end_time = int(batch[0][0]) - 1
        if len(batch) < limit:
            break

    candles = CandleData(
        opens=[float(k[1]) for k in all_raw],
        highs=[float(k[2]) for k in all_raw],
        lows=[float(k[3]) for k in all_raw],
        closes=[float(k[4]) for k in all_raw],
        volumes=[float(k[5]) for k in all_raw],
    )
    timestamps = [int(k[0]) for k in all_raw]
    return candles, timestamps


# ── Scoring (mirrors bot_Gambler._score_tf) ───────────────────────────────────

def _score_tf(candles: CandleData, weight: float) -> float:
    closes  = candles.closes
    highs   = candles.highs
    lows    = candles.lows
    volumes = candles.volumes
    if len(closes) < 22:
        return 0.0

    score = 0.0

    # EMA 9/21
    es_vals = _ema(closes, 9)
    el_vals = _ema(closes, 21)
    if es_vals and el_vals:
        es, el = es_vals[-1], el_vals[-1]
        gap = (es - el) / el * 100 if el else 0
        if gap > EMA_GAP_PCT:
            score += 1.0 * weight
        elif gap < -EMA_GAP_PCT:
            score -= 1.0 * weight

    # RSI 14
    rv = _rsi(closes, 14)
    if rv < RSI_OVERSOLD:
        score += 0.8 * weight
    elif rv > RSI_OVERBOUGHT:
        score -= 0.8 * weight

    # Bollinger Bands 20, 2σ
    bb = bollinger_bands(closes, 20, 2.0)
    p = closes[-1]
    if p < bb.lower:
        score += 0.7 * weight
    elif p > bb.upper:
        score -= 0.7 * weight

    # Volume surge
    if len(volumes) >= 22:
        avg_v = sum(volumes[-21:-1]) / 20
        if avg_v > 0 and volumes[-1] / avg_v >= VOL_SURGE_MULT:
            score *= 1.3

    return score


def _signal(sl3: CandleData, sl5: CandleData, sl15: CandleData,
            threshold: float) -> tuple[str, float]:
    total = (_score_tf(sl3, 1.0) + _score_tf(sl5, 1.5) + _score_tf(sl15, 2.0))
    if total >= threshold:
        return "LONG", total
    if total <= -threshold:
        return "SHORT", total
    return "SKIP", total


# ── TP / SL levels ────────────────────────────────────────────────────────────

def _levels(entry: float, side: str, tp_roe: float,
            sl_hard_roe: float, lev: int) -> tuple[float, float, float]:
    tp_move      = tp_roe     / 100 / lev + TAKER_FEE * 2
    sl_soft_move = SL_SOFT_ROE_PCT / 100 / lev
    sl_hard_move = sl_hard_roe / 100 / lev
    if side == "LONG":
        return (entry * (1 + tp_move),
                entry * (1 - sl_soft_move),
                entry * (1 - sl_hard_move))
    return (entry * (1 - tp_move),
            entry * (1 + sl_soft_move),
            entry * (1 + sl_hard_move))


def _roe(entry: float, price: float, side: str, lev: int) -> float:
    pnl = (price - entry) if side == "LONG" else (entry - price)
    return pnl / entry * lev * 100


# ── Metrics ───────────────────────────────────────────────────────────────────

_WIN_REASONS = ("TP", "TRAIL", "FLIP_TP")


def _metrics(trades: list[dict], equity_curve: list[float]) -> dict:
    if not trades:
        return {k: 0 for k in (
            "total wins losses win_rate long_win_rate short_win_rate "
            "total_roe avg_roe profit_factor expectancy "
            "max_drawdown max_dd_duration sharpe sortino"
        ).split()}

    def _is_win(t: dict) -> bool:
        return any(t["reason"].startswith(r) for r in _WIN_REASONS)

    wins   = [t for t in trades if _is_win(t)]
    losses = [t for t in trades if not _is_win(t)]
    longs  = [t for t in trades if t["side"] == "LONG"]
    shorts = [t for t in trades if t["side"] == "SHORT"]
    lw = [t for t in longs  if _is_win(t)]
    sw = [t for t in shorts if _is_win(t)]

    win_roes  = [t["roe"] for t in wins]
    loss_roes = [abs(t["roe"]) for t in losses]
    pf = (sum(win_roes) / sum(loss_roes)) if sum(loss_roes) > 0 else 0
    avg_win  = sum(win_roes)  / len(win_roes)  if win_roes  else 0
    avg_loss = sum(loss_roes) / len(loss_roes) if loss_roes else 0
    wr = len(wins) / len(trades)
    expectancy = wr * avg_win - (1 - wr) * avg_loss

    # Trade-level Sharpe / Sortino from equity curve returns
    returns = [(equity_curve[i+1] / equity_curve[i] - 1)
               for i in range(len(equity_curve) - 1) if equity_curve[i] > 0]
    n = len(returns)
    if n > 1:
        mu = sum(returns) / n
        std = math.sqrt(sum((r - mu) ** 2 for r in returns) / n)
        sharpe = mu / std if std > 0 else 0
        neg = [r for r in returns if r < 0]
        std_neg = math.sqrt(sum(r ** 2 for r in neg) / len(neg)) if neg else std
        sortino = mu / std_neg if std_neg > 0 else 0
    else:
        sharpe = sortino = 0.0

    # Max drawdown (over equity curve)
    peak = equity_curve[0]
    max_dd = 0.0
    max_dd_dur = 0
    cur_start = 0
    for i, eq in enumerate(equity_curve):
        if eq > peak:
            peak = eq
            cur_start = i
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            max_dd_dur = i - cur_start

    all_roes = [t["roe"] for t in trades]
    return {
        "total":           len(trades),
        "wins":            len(wins),
        "losses":          len(losses),
        "long_total":      len(longs),
        "short_total":     len(shorts),
        "long_wins":       len(lw),
        "short_wins":      len(sw),
        "long_losses":     len(longs)  - len(lw),
        "short_losses":    len(shorts) - len(sw),
        "win_rate":        round(wr * 100, 1),
        "long_win_rate":   round(len(lw) / len(longs)  * 100, 1) if longs  else 0,
        "short_win_rate":  round(len(sw) / len(shorts) * 100, 1) if shorts else 0,
        "total_roe":       round(sum(all_roes), 2),
        "avg_roe":         round(sum(all_roes) / len(trades), 2),
        "profit_factor":   round(pf, 3),
        "expectancy":      round(expectancy, 2),
        "max_drawdown":    round(max_dd, 2),
        "max_dd_duration": max_dd_dur,
        "sharpe":          round(sharpe, 4),
        "sortino":         round(sortino, 4),
    }


# ── Main simulation ───────────────────────────────────────────────────────────

def _ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _close_pos(trades: list, equity_curve: list, equity_ts: list,
               pos_side: str, pos_entry_bar: int, ts_5m: list,
               entry_price: float, exit_price: float, reason: str,
               hold_bars: int, ts_cur: int, lev: int) -> float:
    roe = _roe(entry_price, exit_price, pos_side, lev)
    trades.append({
        "entry_ts":    _ts(ts_5m[pos_entry_bar]),
        "exit_ts":     _ts(ts_cur),
        "side":        pos_side,
        "entry_price": round(entry_price, 6),
        "exit_price":  round(exit_price, 6),
        "roe":         round(roe, 2),
        "reason":      reason,
        "hold_bars":   hold_bars,
    })
    equity = equity_curve[-1] * (1 + roe / 100)
    equity_curve.append(round(equity, 4))
    equity_ts.append(_ts(ts_cur))
    return roe


def run_backtest(symbol: str, days: int, threshold: float,
                 tp_roe: float, sl_roe: float, lev: int) -> dict:
    print(f"Fetching {symbol} — {days} days × 3 TFs …")
    c3,  t3  = _fetch(symbol, "3m",  days)
    c5,  t5  = _fetch(symbol, "5m",  days)
    c15, t15 = _fetch(symbol, "15m", days)
    n = len(t5)
    print(f"  3m={len(t3)} bars  5m={n} bars  15m={len(t15)} bars")

    if n < WARMUP_5M + 10:
        raise ValueError(f"Not enough 5m data ({n} bars)")

    # Position state
    pos_side:  Optional[str] = None
    pos_entry: float = 0.0
    pos_tp:    float = 0.0
    pos_sl_s:  float = 0.0
    pos_sl_h:  float = 0.0
    pos_peak:  float = 0.0
    pos_bar:   int   = 0
    last_close_bar: int = -999

    trades: list[dict] = []
    equity_curve: list[float] = [100.0]
    equity_ts: list[str] = []

    print(f"Simulating {n - WARMUP_5M} bars …", flush=True)

    for i in range(WARMUP_5M, n):
        ts_cur = t5[i]
        o5, h5, l5, c5_price = (
            c5.opens[i], c5.highs[i], c5.lows[i], c5.closes[i]
        )

        # Build TF slices aligned to current bar's timestamp
        j3  = bisect.bisect_right(t3,  ts_cur) - 1
        j15 = bisect.bisect_right(t15, ts_cur) - 1
        j3  = max(j3,  0)
        j15 = max(j15, 0)

        def _slice(candles: CandleData, start: int, end: int) -> CandleData:
            s = max(0, start)
            return CandleData(
                candles.opens[s:end+1],  candles.highs[s:end+1],
                candles.lows[s:end+1],   candles.closes[s:end+1],
                candles.volumes[s:end+1],
            )

        sl3  = _slice(c3,  j3  - TF_LIMITS["3m"]  + 1, j3)
        sl5  = _slice(c5,  i   - TF_LIMITS["5m"]  + 1, i)
        sl15 = _slice(c15, j15 - TF_LIMITS["15m"] + 1, j15)

        direction, score = _signal(sl3, sl5, sl15, threshold)

        closed_this_bar = False

        # ── Manage open position ──────────────────────────────────────────
        if pos_side is not None:
            if pos_side == "LONG":
                sl_hit   = l5 <= pos_sl_h
                tp_hit   = h5 >= pos_tp
                soft_hit = l5 <= pos_sl_s
            else:
                sl_hit   = h5 >= pos_sl_h
                tp_hit   = l5 <= pos_tp
                soft_hit = h5 >= pos_sl_s

            if sl_hit and tp_hit:   # pessimistic: SL wins
                tp_hit = False

            hold = i - pos_bar

            if sl_hit:
                _close_pos(trades, equity_curve, equity_ts, pos_side, pos_bar,
                           t5, pos_entry, pos_sl_h, "HARD_SL", hold, ts_cur, lev)
                pos_side = None;  last_close_bar = i;  closed_this_bar = True

            elif tp_hit:
                _close_pos(trades, equity_curve, equity_ts, pos_side, pos_bar,
                           t5, pos_entry, pos_tp, "TP", hold, ts_cur, lev)
                pos_side = None;  last_close_bar = i;  closed_this_bar = True

            else:
                cur_roe = _roe(pos_entry, c5_price, pos_side, lev)
                if cur_roe > pos_peak:
                    pos_peak = cur_roe

                # Trailing stop
                if (pos_peak >= TRAIL_ACTIVATE_ROE and
                        cur_roe <= pos_peak * (1 - TRAIL_GIVEBACK_PCT)):
                    _close_pos(trades, equity_curve, equity_ts, pos_side, pos_bar,
                               t5, pos_entry, c5_price, "TRAIL", hold, ts_cur, lev)
                    pos_side = None;  last_close_bar = i;  closed_this_bar = True

                # Flip TP (trend reversed while profitable)
                elif (cur_roe >= MIN_ROE_FOR_FLIP and
                      direction not in ("SKIP", pos_side)):
                    _close_pos(trades, equity_curve, equity_ts, pos_side, pos_bar,
                               t5, pos_entry, c5_price, "FLIP_TP", hold, ts_cur, lev)
                    pos_side = None;  last_close_bar = i;  closed_this_bar = True

                # Soft SL (trend gone)
                elif soft_hit and direction not in ("SKIP", pos_side):
                    _close_pos(trades, equity_curve, equity_ts, pos_side, pos_bar,
                               t5, pos_entry, c5_price, "SOFT_SL", hold, ts_cur, lev)
                    pos_side = None;  last_close_bar = i;  closed_this_bar = True

                # Early exit (strong opposite signal)
                elif (direction not in ("SKIP", pos_side) and
                      abs(score) >= threshold * 1.2):
                    _close_pos(trades, equity_curve, equity_ts, pos_side, pos_bar,
                               t5, pos_entry, c5_price, "EARLY_EXIT", hold, ts_cur, lev)
                    pos_side = None;  last_close_bar = i;  closed_this_bar = True

        # ── Enter new position at next bar's open ─────────────────────────
        if (pos_side is None and not closed_this_bar and
                direction != "SKIP" and
                (i - last_close_bar) >= COOLDOWN_BARS and
                i + 1 < n):
            ep = c5.opens[i + 1]  # fill at next bar open (avoids look-ahead)
            tp, sl_s, sl_h = _levels(ep, direction, tp_roe, sl_roe, lev)
            pos_side  = direction
            pos_entry = ep
            pos_tp    = tp
            pos_sl_s  = sl_s
            pos_sl_h  = sl_h
            pos_peak  = 0.0
            pos_bar   = i + 1

        # Progress
        done = i - WARMUP_5M
        total_bars = n - WARMUP_5M
        if done % 500 == 0 and done > 0:
            print(f"  {done/total_bars*100:.0f}%  bar={done}/{total_bars}  "
                  f"trades={len(trades)}", flush=True)

    # Force-close at end of data
    if pos_side is not None:
        lp = c5.closes[-1]
        _close_pos(trades, equity_curve, equity_ts, pos_side, pos_bar,
                   t5, pos_entry, lp, "END_OF_DATA",
                   n - 1 - pos_bar, t5[-1], lev)

    m = _metrics(trades, equity_curve)

    # Equity curve for chart (max 500 points)
    step = max(1, len(equity_ts) // 500)
    eq_chart = [{"ts": _ts(t5[WARMUP_5M]), "equity": 100.0}]
    for k in range(0, len(equity_ts), step):
        eq_chart.append({"ts": equity_ts[k], "equity": equity_curve[k + 1]})

    return {
        "meta": {
            "symbol":    symbol,
            "days":      days,
            "threshold": threshold,
            "tp_roe":    tp_roe,
            "sl_roe":    sl_roe,
            "leverage":  lev,
            "bars_5m":   n,
            "ran_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "metrics":      m,
        "trades":       trades,
        "equity_curve": eq_chart,
    }


# ── Output ────────────────────────────────────────────────────────────────────

def _print_summary(r: dict) -> None:
    m, meta = r["metrics"], r["meta"]
    print(f"\n{'='*62}")
    print(f"  Gambler Backtest — {meta['symbol']}  {meta['days']}d  @{meta['ran_at']}")
    print(f"  threshold={meta['threshold']}  TP={meta['tp_roe']}%  SL={meta['sl_roe']}%  {meta['leverage']}x")
    print(f"{'='*62}")
    print(f"  Trades:        {m['total']}  ({m['wins']} wins / {m['losses']} losses)")
    print(f"  Win Rate:      {m['win_rate']}%  (L:{m['long_win_rate']}% / S:{m['short_win_rate']}%)")
    print(f"  Total ROE:     {m['total_roe']:+.1f}%")
    print(f"  Avg ROE/trade: {m['avg_roe']:+.2f}%")
    print(f"  Profit Factor: {m['profit_factor']:.2f}")
    print(f"  Expectancy:    {m['expectancy']:+.2f}% per trade")
    print(f"  Max Drawdown:  -{m['max_drawdown']:.1f}%  ({m['max_dd_duration']} trades)")
    print(f"  Sharpe:        {m['sharpe']:.3f}  |  Sortino: {m['sortino']:.3f}")
    print(f"{'='*62}")
    trades = r["trades"]
    if trades:
        print(f"\n  Recent trades (last 10):")
        for t in trades[-10:]:
            roe_c = "+" if t["roe"] >= 0 else ""
            print(f"  {t['exit_ts']}  {t['side']:<5}  {t['reason']:<12}  "
                  f"{roe_c}{t['roe']:.1f}%  hold={t['hold_bars']}bars")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Gambler Bot Backtester")
    ap.add_argument("--symbol",    default="XRPUSDT",       help="Trading pair")
    ap.add_argument("--days",      type=int,   default=14,  help="Lookback days")
    ap.add_argument("--threshold", type=float, default=SCORE_THRESHOLD)
    ap.add_argument("--tp",        type=float, default=TP_ROE_PCT,    dest="tp",  help="TP ROE%%")
    ap.add_argument("--sl",        type=float, default=SL_HARD_ROE_PCT, dest="sl", help="Hard SL ROE%%")
    ap.add_argument("--leverage",  type=int,   default=LEVERAGE)
    args = ap.parse_args()

    result = run_backtest(
        symbol=args.symbol.upper(),
        days=args.days,
        threshold=args.threshold,
        tp_roe=args.tp,
        sl_roe=args.sl,
        lev=args.leverage,
    )
    _print_summary(result)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\n  Saved → {OUTPUT_FILE}")
