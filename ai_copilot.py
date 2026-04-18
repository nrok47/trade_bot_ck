"""
AI Co-pilot for Gambler Bot
===========================
Calls Claude Haiku to evaluate trade setups and return a confidence score.
Confidence blends with technical score to scale position size.

Usage:
  from ai_copilot import evaluate_trade
  result = evaluate_trade(side, score, tf_details, price, atr_5m)
  # result = {"confidence": 72, "quality": "INFERRED", "reasoning": "..."}
"""
from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger("gambler.copilot")

_CACHE_TTL = 60  # seconds — same signal within 1 min reuses the last answer
_cache: dict[str, dict] = {}

_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 256

_FALLBACK = {"confidence": 60, "quality": "UNKNOWN", "reasoning": "fallback — API unavailable"}


def _client():
    try:
        import anthropic
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            return None
        return anthropic.Anthropic(api_key=api_key)
    except ImportError:
        return None


def _build_prompt(side: str, score: float, tf_details: dict, price: float, atr: float) -> str:
    score_3m  = tf_details.get("3m",  {}).get("score", 0)
    score_5m  = tf_details.get("5m",  {}).get("score", 0)
    score_15m = tf_details.get("15m", {}).get("score", 0)
    atr_pct   = atr / price * 100 if price > 0 else 0

    tf_lines = []
    for tf in ("3m", "5m", "15m"):
        d = tf_details.get(tf, {})
        tf_lines.append(
            f"  {tf}: score={d.get('score', 0):+.2f}  EMA={d.get('ema', '?')}  "
            f"RSI={d.get('rsi', '?')}  BB={d.get('bb', '?')}"
        )
    tf_block = "\n".join(tf_lines)

    # Determine confluence level for the prompt
    signs = [score_3m >= 0, score_5m >= 0, score_15m >= 0] if side == "LONG" else \
            [score_3m < 0, score_5m < 0, score_15m < 0]
    agree = sum(signs)
    confluence = "KNOWN" if agree == 3 else ("INFERRED" if agree == 2 else "UNKNOWN")

    return f"""You are a trading co-pilot assessing a futures trade setup. Reply with ONLY valid JSON.

Setup:
- Proposed direction: {side}
- Technical score: {score:+.2f} (threshold ±3.5, max ±11)
- Price: {price:.6f}  ATR(5m): {atr:.6f} ({atr_pct:.2f}% of price)
- TF breakdown:
{tf_block}
- TF confluence: {agree}/3 timeframes agree → {confluence}

Rate your confidence in this trade (0=avoid, 50=neutral, 100=high conviction).
Consider: TF alignment, signal strength vs threshold, ATR (high ATR = volatile = lower confidence).

Return ONLY this JSON (no markdown, no explanation):
{{"confidence": <0-100>, "quality": "<KNOWN|INFERRED|UNKNOWN>", "reasoning": "<1 sentence max 80 chars>"}}"""


def evaluate_trade(
    side: str,
    score: float,
    tf_details: dict,
    price: float,
    atr: float,
) -> dict:
    """
    Evaluate trade via Claude Haiku. Returns confidence dict.
    Falls back to {"confidence": 60, ...} if API is unavailable.
    """
    cache_key = f"{side}_{round(score, 1)}"
    now = time.time()

    cached = _cache.get(cache_key)
    if cached and now - cached["ts"] < _CACHE_TTL:
        logger.debug("CO-PILOT cache hit: %s", cache_key)
        return cached["result"]

    cli = _client()
    if cli is None:
        logger.warning("CO-PILOT skipped — anthropic SDK not installed or no API key")
        return _FALLBACK.copy()

    try:
        prompt = _build_prompt(side, score, tf_details, price, atr)
        msg = cli.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        result = json.loads(raw)
        result["confidence"] = max(0, min(100, int(result.get("confidence", 60))))
        result.setdefault("quality", "INFERRED")
        result.setdefault("reasoning", "")
        logger.info("CO-PILOT: confidence=%d%% quality=%s | %s",
                    result["confidence"], result["quality"], result["reasoning"])
    except json.JSONDecodeError as exc:
        logger.warning("CO-PILOT bad JSON (%s) — using fallback", exc)
        result = _FALLBACK.copy()
    except Exception as exc:
        logger.warning("CO-PILOT error: %s — using fallback", exc)
        result = _FALLBACK.copy()

    _cache[cache_key] = {"ts": now, "result": result}
    return result
