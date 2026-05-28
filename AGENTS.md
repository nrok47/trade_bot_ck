# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Running the bots

```powershell
# Gambler Bot (primary active bot)
python bot_Gambler.py                  # dry run follows DRY_RUN in .env
python bot_Gambler.py --dry-run        # force dry run
python bot_Gambler.py --symbol DOGEUSDT

# Gambler dashboard (separate terminal)
python gambler_dashboard.py            # http://localhost:8080

# Gambler backtest
python backtest_gambler.py             # XRPUSDT, 14 days
python backtest_gambler.py --days 30 --symbol DOGEUSDT --threshold 4.0 --tp 40 --sl 25

# Grid Bot (legacy, less active)
python bot.py

# Grid Bot web UI
python web_ui.py                       # http://localhost:8080
```

## Setup

```powershell
pip install -r requirements.txt
cp .env.example .env   # then fill in API keys
```

`.env` must exist for `config.py` to load. `DRY_RUN=true` by default — no real orders.

## Architecture

### Two separate bots sharing infrastructure

**Gambler Bot** (`bot_Gambler.py`) — primary bot, directional swing trader:
- Fetches 3m/5m/15m candles, scores each TF (EMA, CDC, RSI, Bollinger, VWAP, Volume) weighted by TF importance
- Aggregates score across TFs + Fear & Greed index → enters LONG or SHORT when score exceeds `effective_threshold = score_threshold × regime_multiplier`
- Regime detection (ADX + Hurst exponent from 15m) adjusts threshold: RANGING markets require a higher score to enter
- Exit rules: TP hit, Hard SL, Soft SL (trend re-check), Trailing stop (activates at 10% ROE peak), Trend-flip early exit
- Places an exchange-side `STOP_MARKET` order at hard SL so position closes even if bot is offline

**Grid Bot** (`bot.py`) — trend-following grid, places limit buy/sell ladders around current price.

### Shared modules

| File | Role |
|------|------|
| `config.py` | Singleton `config` — reads all `.env` values once at import; restart required for `.env` changes |
| `binance_client.py` | Singleton `binance` — wraps python-binance for both Spot and Futures; all methods branch on `config.is_futures` |
| `indicators.py` | Pure functions: `ema`, `rsi`, `atr`, `bollinger_bands`, `adx`, `vwap`, `cdc_action_zone`, `hurst_exponent` |
| `ai_copilot.py` | Optional: calls Codex Haiku to score trade quality → scales position size 0.5×–1.5× |

### Gambler Bot config: two layers

1. **`.env`** — symbol, API keys, `DRY_RUN`. Read once at startup.
2. **`gambler_settings.json`** — `capital_pct`, `leverage`, `tp_roe_pct`, `sl_hard_roe_pct`, `score_threshold`. Re-read **every poll loop** so the dashboard can hot-update these without restarting the bot.

The module-level constants in `bot_Gambler.py` (`CAPITAL_PCT`, `SL_HARD_ROE_PCT`, etc.) are **fallback defaults** only — at runtime the bot always uses `_read_settings_bot()` which merges defaults with the JSON file. The startup log now reflects the actual settings.

### State files written by Gambler Bot

| File | Contents |
|------|----------|
| `gambler_state.json` | Current open position (restored on restart) |
| `gambler_live.json` | Latest price/score/direction snapshot (refreshed every 30s) |
| `gambler_trades.json` | Append-only closed trade log |
| `gambler_history.json` | Rolling 200-poll history for dashboard chart |
| `gambler_backtest.json` | Output of `backtest_gambler.py` — read by Kelly Criterion |

### Kelly Criterion

When `gambler_kelly.json` has `{"enabled": true}`, `_apply_kelly()` reads `gambler_backtest.json` metrics and overrides `capital_pct` with ¼ Kelly fraction (capped at 60%). Requires ≥10 backtest trades with valid win/loss stats.

### AI Co-pilot

When `gambler_copilot.json` has `{"enabled": true}` and `ANTHROPIC_API_KEY` is set, each entry calls `ai_copilot.evaluate_trade()` which asks Codex Haiku for a confidence score (0–100). Position size is scaled by `0.5 + confidence/100`. Results cached 60s per signal.

## Key implementation notes

### ADX formula
`indicators.adx()` uses Wilder smoothing for TR/+DM/-DM (formula: `prev*(n-1)/n + v`) but uses standard EMA (`(prev*(n-1) + v) / n`) for the DX→ADX final step. This is intentional — applying the Wilder formula to DX (which is already bounded 0–100) would cause ADX to diverge to ~14× the true value.

### DRY_RUN behaviour
`binance.get_balance()` returns 9999.0 in dry run. All order methods log `[DRY]` and return a fake order dict/ID without hitting Binance. The exchange-side SL stop order is simulated with a fake string ID (`DRY_SL_SELL_1.xxx`).

### Gambler Bot guardrails (non-configurable)
- `MAX_SESSION_LOSS_USDT = 100` — stops new entries if session P&L drops below −$100
- `MAX_TRADES_PER_HOUR = 3` — rate-limits entries within a rolling 1-hour window
- 5-minute cooldown after every close before next entry

### Score threshold effective value
`effective_threshold = score_threshold × regime_mult`
Regime multipliers: TRENDING=1.0, WEAK_TREND=1.2, RANGING=1.4, RANGING_STRONG=1.6.
With `score_threshold=3.0` and regime=RANGING, the bot needs score ≥ 4.2 to enter.
