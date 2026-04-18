"""
Gambler Bot Dashboard
=====================
Web UI for monitoring bot_Gambler.py — position, live score, trade history, log.

  python gambler_dashboard.py            # http://localhost:8080
  python gambler_dashboard.py --port 9090

Reads:
  gambler_state.json  — current open position
  gambler_live.json   — latest price/score/direction (written by bot each poll)
  gambler_bot.log     — trade history + live log tail
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Optional

try:
    from flask import Flask, render_template_string, jsonify
except ImportError:
    print("ติดตั้ง Flask ก่อน:  pip install flask")
    raise

BASE_DIR    = Path(__file__).parent
STATE_FILE  = BASE_DIR / "gambler_state.json"
LIVE_FILE   = BASE_DIR / "gambler_live.json"
LOG_FILE    = BASE_DIR / "gambler_bot.log"

app = Flask(__name__)

# ── Styles (reuse dark theme from web_ui.py) ──────────────────────────────────

_STYLE = """
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gambler Bot</title>
<style>
  *{box-sizing:border-box}
  body{font-family:monospace;background:#0d1117;color:#c9d1d9;margin:0;padding:0}
  nav{background:#161b22;padding:12px 20px;display:flex;gap:20px;border-bottom:1px solid #30363d;align-items:center}
  nav .brand{color:#f0f6fc;font-weight:bold;margin-right:8px}
  nav a{color:#58a6ff;text-decoration:none;font-size:14px}
  nav a:hover{color:#79c0ff}
  .container{padding:20px;max-width:1100px;margin:auto}
  h2{color:#f0f6fc;border-bottom:1px solid #30363d;padding-bottom:8px;margin-top:0}
  .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:16px}
  .card h3{color:#79c0ff;margin-top:0;font-size:12px;text-transform:uppercase;letter-spacing:1px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
  .grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
  .grid6{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}
  @media(max-width:700px){.grid2,.grid3,.grid4,.grid6{grid-template-columns:1fr 1fr}}
  .stat{text-align:center;padding:10px}
  .stat .val{font-size:22px;font-weight:bold;line-height:1.2}
  .stat .lbl{font-size:11px;color:#8b949e;margin-top:4px}
  .green{color:#3fb950} .red{color:#f85149} .yellow{color:#e3b341}
  .blue{color:#58a6ff} .gray{color:#8b949e} .orange{color:#d29922}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{background:#21262d;color:#8b949e;padding:7px 10px;text-align:left;font-weight:normal}
  td{padding:7px 10px;border-top:1px solid #21262d}
  .badge{padding:2px 8px;border-radius:12px;font-size:11px;display:inline-block}
  .b-green{background:#0f3d20;color:#3fb950}
  .b-red{background:#3d0f0f;color:#f85149}
  .b-yellow{background:#3d2f0f;color:#e3b341}
  .b-blue{background:#0d2d5e;color:#58a6ff}
  .b-gray{background:#21262d;color:#8b949e}
  pre{background:#0d1117;border:1px solid #30363d;padding:12px;border-radius:6px;
    overflow-x:auto;font-size:11px;line-height:1.5;max-height:420px;overflow-y:auto;white-space:pre-wrap}
  .score-bar-wrap{display:flex;align-items:center;gap:10px;margin:8px 0}
  .score-bar-bg{flex:1;height:12px;background:#21262d;border-radius:6px;overflow:hidden;position:relative}
  .score-bar-center{position:absolute;left:50%;top:0;width:1px;height:100%;background:#444}
  .score-bar-fill{position:absolute;height:100%;border-radius:6px;transition:width .3s}
  .tf-row{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0}
  .tf-chip{background:#21262d;border-radius:6px;padding:4px 10px;font-size:12px}
  .stale{opacity:.45}
  .refresh-info{color:#8b949e;font-size:11px;margin-top:4px}
  .no-pos{color:#8b949e;font-style:italic}
</style>
"""

_NAV = """
<nav>
  <span class="brand">Gambler 8x</span>
  <a href="/">Dashboard</a>
  <a href="/log">Log</a>
  <a href="/api/status">JSON</a>
</nav>
"""

# ── Main dashboard template ────────────────────────────────────────────────────

DASH_TMPL = """<!DOCTYPE html><html>
<head>""" + _STYLE + """</head>
<body>""" + _NAV + """
<div class="container">

  <!-- Header: price + signal -->
  <div class="card">
    <h3>Live Signal
      {% if live %}<span class="refresh-info">&nbsp;·&nbsp;last update {{ live.ts }}</span>{% endif %}
    </h3>
    {% if live %}
    <div class="grid4">
      <div class="stat">
        <div class="val blue">${{ '%.5f'|format(live.price) }}</div>
        <div class="lbl">{{ live.symbol }} Price</div>
      </div>
      <div class="stat">
        <div class="val {{ 'green' if live.score > 0 else ('red' if live.score < 0 else 'gray') }}">
          {{ '%+.2f'|format(live.score) }}
        </div>
        <div class="lbl">Total Score</div>
      </div>
      <div class="stat">
        {% if live.direction == 'LONG' %}
          <div class="val green">▲ LONG</div>
        {% elif live.direction == 'SHORT' %}
          <div class="val red">▼ SHORT</div>
        {% else %}
          <div class="val gray">◆ SKIP</div>
        {% endif %}
        <div class="lbl">Signal</div>
      </div>
      <div class="stat">
        {% if live.cooldown_left > 0 %}
          <div class="val yellow">{{ live.cooldown_left|int }}s</div>
          <div class="lbl">Cooldown</div>
        {% else %}
          <div class="val green">Ready</div>
          <div class="lbl">Status</div>
        {% endif %}
      </div>
    </div>

    <!-- Score bar -->
    <div class="score-bar-wrap">
      <span class="gray" style="font-size:11px">SHORT</span>
      <div class="score-bar-bg">
        <div class="score-bar-center"></div>
        {% set pct = [[(live.score / 11 * 50)|abs, 50]|min, 0]|max %}
        {% if live.score >= 0 %}
        <div class="score-bar-fill"
             style="left:50%;width:{{ pct }}%;background:#3fb950"></div>
        {% else %}
        <div class="score-bar-fill"
             style="right:50%;width:{{ pct }}%;background:#f85149"></div>
        {% endif %}
      </div>
      <span class="gray" style="font-size:11px">LONG</span>
    </div>

    <!-- TF breakdown chips -->
    {% if live.details %}
    <div class="tf-row">
      {% for tf, d in live.details.items() %}
      <div class="tf-chip">
        <b>{{ tf }}</b>
        <span class="{{ 'green' if d.score > 0 else ('red' if d.score < 0 else 'gray') }}">
          {{ '%+.2f'|format(d.score) }}
        </span>
        &nbsp;EMA:{{ d.ema|truncate(16,true,'') if d.ema else '?' }}
        &nbsp;RSI:{{ d.rsi if d.rsi else '?' }}
      </div>
      {% endfor %}
    </div>
    {% endif %}

    {% if live.copilot %}
    <div style="margin-top:8px;font-size:12px">
      <span class="badge b-blue">AI co-pilot</span>
      confidence=<b>{{ live.copilot.confidence }}%</b>
      quality=<span class="{{ 'green' if live.copilot.quality=='KNOWN' else ('yellow' if live.copilot.quality=='INFERRED' else 'gray') }}">
        {{ live.copilot.quality }}</span>
      &nbsp;{{ live.copilot.reasoning }}
    </div>
    {% endif %}

    {% else %}
    <p class="no-pos">Bot not running or gambler_live.json not found.</p>
    {% endif %}
  </div>

  <!-- Open Position -->
  <div class="card">
    <h3>Open Position</h3>
    {% if pos %}
    <div class="grid4" style="margin-bottom:12px">
      <div class="stat">
        <div class="val {{ 'green' if pos.side == 'LONG' else 'red' }}">
          {{ pos.side }}
        </div>
        <div class="lbl">Direction</div>
      </div>
      <div class="stat">
        <div class="val">${{ '%.5f'|format(pos.entry_price) }}</div>
        <div class="lbl">Entry Price</div>
      </div>
      <div class="stat">
        <div class="val blue">${{ '%.2f'|format(pos.margin) }}</div>
        <div class="lbl">Margin</div>
      </div>
      <div class="stat">
        <div class="val {{ 'green' if pos.peak_roe > 0 else 'gray' }}">
          {{ '%+.2f'|format(pos.peak_roe) }}%
        </div>
        <div class="lbl">Peak ROE</div>
      </div>
    </div>
    <table>
      <tr>
        <th>TP</th><th>SL Soft</th><th>SL Hard</th><th>Qty</th>
      </tr>
      <tr>
        <td class="green">${{ '%.5f'|format(pos.tp_price) }}</td>
        <td class="yellow">${{ '%.5f'|format(pos.sl_soft_price) }}</td>
        <td class="red">${{ '%.5f'|format(pos.sl_hard_price) }}</td>
        <td>{{ '%.4f'|format(pos.qty) }}</td>
      </tr>
    </table>
    {% else %}
    <p class="no-pos">No open position</p>
    {% endif %}
  </div>

  <!-- Trade Summary -->
  <div class="card">
    <h3>Trade Summary (from log)</h3>
    <div class="grid6">
      <div class="stat">
        <div class="val blue">{{ summary.total }}</div>
        <div class="lbl">Total Trades</div>
      </div>
      <div class="stat">
        <div class="val green">{{ summary.wins }}</div>
        <div class="lbl">Wins</div>
      </div>
      <div class="stat">
        <div class="val red">{{ summary.losses }}</div>
        <div class="lbl">Losses</div>
      </div>
      <div class="stat">
        <div class="val {{ 'green' if summary.win_rate >= 50 else 'red' }}">
          {{ '%.0f'|format(summary.win_rate) }}%
        </div>
        <div class="lbl">Win Rate</div>
      </div>
      <div class="stat">
        <div class="val {{ 'green' if summary.avg_roe >= 0 else 'red' }}">
          {{ '%+.1f'|format(summary.avg_roe) }}%
        </div>
        <div class="lbl">Avg ROE</div>
      </div>
      <div class="stat">
        <div class="val {{ 'green' if summary.total_roe >= 0 else 'red' }}">
          {{ '%+.1f'|format(summary.total_roe) }}%
        </div>
        <div class="lbl">Total ROE</div>
      </div>
    </div>

    {% if summary.trades %}
    <table style="margin-top:12px">
      <tr>
        <th>#</th><th>Time</th><th>Side</th><th>Reason</th><th>ROE</th>
      </tr>
      {% for t in summary.trades[-20:]|reverse %}
      <tr>
        <td class="gray">{{ loop.index }}</td>
        <td>{{ t.time }}</td>
        <td><span class="badge {{ 'b-green' if t.side == 'LONG' else 'b-red' }}">{{ t.side }}</span></td>
        <td><span class="badge b-gray">{{ t.reason }}</span></td>
        <td class="{{ 'green' if t.roe >= 0 else 'red' }}">{{ '%+.1f'|format(t.roe) }}%</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <p class="no-pos">No closed trades found in log yet.</p>
    {% endif %}
  </div>

</div>
<script>
  // Auto-refresh every 30s
  setTimeout(()=>location.reload(), 30000);
</script>
</body></html>
"""

LOG_TMPL = """<!DOCTYPE html><html>
<head>""" + _STYLE + """</head>
<body>""" + _NAV + """
<div class="container">
  <h2>Log — last {{ count }} lines</h2>
  <div class="card">
    <pre id="logbox">{{ content }}</pre>
  </div>
</div>
<script>
  var el = document.getElementById('logbox');
  el.scrollTop = el.scrollHeight;
  setTimeout(()=>location.reload(), 30000);
</script>
</body></html>
"""

# ── Data helpers ──────────────────────────────────────────────────────────────

def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


_RE_CLOSE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?CLOSED (LONG|SHORT).*?reason=(\S+)"
)
_RE_CLOSE_DRY = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?\[DRY\] CLOSE (LONG|SHORT).*?reason=(\S+)"
)
_RE_ROE = re.compile(r"\(([+-]?\d+\.?\d*)%\)")

_WIN_PREFIXES = ("TP(", "TRAIL(", "FLIP_TP(")
_LOSS_PREFIXES = ("HARD_SL(", "SOFT_SL", "EARLY_EXIT(")


def _parse_summary() -> dict:
    if not LOG_FILE.exists():
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "avg_roe": 0.0, "total_roe": 0.0, "trades": []}

    text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    trades = []

    for pat in (_RE_CLOSE, _RE_CLOSE_DRY):
        for m in pat.finditer(text):
            ts_str, side, reason = m.group(1), m.group(2), m.group(3)
            roe_m = _RE_ROE.search(reason)
            roe = float(roe_m.group(1)) if roe_m else 0.0
            trades.append({"time": ts_str[11:19], "side": side, "reason": reason, "roe": roe})

    # Deduplicate by (time, side) — both patterns may match
    seen: set[tuple] = set()
    unique = []
    for t in trades:
        key = (t["time"], t["side"], t["reason"])
        if key not in seen:
            seen.add(key)
            unique.append(t)

    unique.sort(key=lambda x: x["time"])

    wins = sum(1 for t in unique if any(t["reason"].startswith(p) for p in _WIN_PREFIXES))
    losses = sum(1 for t in unique if any(t["reason"].startswith(p) for p in _LOSS_PREFIXES))
    total_roe = sum(t["roe"] for t in unique)
    avg_roe = total_roe / len(unique) if unique else 0.0
    win_rate = wins / len(unique) * 100 if unique else 0.0

    return {
        "total": len(unique),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "avg_roe": avg_roe,
        "total_roe": total_roe,
        "trades": unique,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    live = _read_json(LIVE_FILE)
    pos_raw = _read_json(STATE_FILE)

    # Wrap pos so template can access attributes
    pos = None
    if pos_raw:
        class _P:
            pass
        p = _P()
        for k, v in pos_raw.items():
            setattr(p, k, v)
        pos = p

    summary = _parse_summary()

    # Wrap live details for template
    if live and live.get("details"):
        details_wrapped = {}
        for tf, d in live["details"].items():
            class _D:
                pass
            obj = _D()
            for k, v in d.items():
                setattr(obj, k, v)
            obj.score = d.get("score", 0)
            obj.ema   = d.get("ema", "")
            obj.rsi   = d.get("rsi", "")
            details_wrapped[tf] = obj
        live["details"] = details_wrapped

    if live and live.get("copilot"):
        class _C:
            pass
        c = _C()
        for k, v in live["copilot"].items():
            setattr(c, k, v)
        live["copilot"] = c

    return render_template_string(DASH_TMPL, live=live, pos=pos, summary=summary)


@app.route("/log")
def log_page():
    lines: list[str] = []
    if LOG_FILE.exists():
        all_lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        lines = all_lines[-200:]
    return render_template_string(LOG_TMPL, content="\n".join(lines), count=len(lines))


@app.route("/api/status")
def api_status():
    live = _read_json(LIVE_FILE) or {}
    pos  = _read_json(STATE_FILE) or {}
    summary = _parse_summary()
    return jsonify({
        "running": bool(live),
        "ts": live.get("ts"),
        "price": live.get("price"),
        "direction": live.get("direction"),
        "score": live.get("score"),
        "position": pos if pos else None,
        "summary": {k: v for k, v in summary.items() if k != "trades"},
    })


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gambler Bot Dashboard")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()
    print(f"Dashboard: http://localhost:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
