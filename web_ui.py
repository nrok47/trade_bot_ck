"""
Web UI — ปรับแต่งค่าและดูสถานะบอทผ่านเว็บเบราเซอร์
======================================================
รันแยกจากบอท (terminal คนละอัน)

  python web_ui.py            # http://localhost:8080
  python web_ui.py --port 9090

ฟีเจอร์:
  / (Dashboard)   — ดูสถานะบอท, P&L, grid range, recent fills
  /config         — แก้ไขค่าใน .env ผ่านฟอร์ม
  /log            — ดู log ล่าสุด 100 บรรทัด
  /api/status     — JSON สำหรับ monitoring script
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from flask import Flask, render_template_string, request, redirect, url_for, jsonify
except ImportError:
    print("ติดตั้ง Flask ก่อน:  pip install flask")
    raise

BASE_DIR = Path(__file__).parent
ENV_FILE  = BASE_DIR / ".env"
STATE_FILE = BASE_DIR / "bot_state.json"
LOG_FILE   = BASE_DIR / "grid_bot.log"

app = Flask(__name__)

# ── HTML Templates ────────────────────────────────────────────────────────────

_BASE_STYLE = """
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Grid Bot</title>
<style>
  body{font-family:monospace;background:#0d1117;color:#c9d1d9;margin:0;padding:0}
  nav{background:#161b22;padding:12px 20px;display:flex;gap:20px;border-bottom:1px solid #30363d}
  nav a{color:#58a6ff;text-decoration:none;font-size:14px}
  nav a:hover{color:#79c0ff}
  .container{padding:20px;max-width:1100px;margin:auto}
  h2{color:#f0f6fc;border-bottom:1px solid #30363d;padding-bottom:8px}
  .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:16px}
  .card h3{color:#79c0ff;margin-top:0;font-size:14px;text-transform:uppercase;letter-spacing:1px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
  .stat{text-align:center;padding:12px}
  .stat .val{font-size:24px;font-weight:bold}
  .stat .lbl{font-size:11px;color:#8b949e;margin-top:4px}
  .green{color:#3fb950} .red{color:#f85149} .yellow{color:#e3b341} .blue{color:#58a6ff}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{background:#21262d;color:#8b949e;padding:8px;text-align:left;font-weight:normal}
  td{padding:8px;border-top:1px solid #21262d}
  .badge{padding:2px 8px;border-radius:12px;font-size:11px}
  .badge-green{background:#0f3d20;color:#3fb950}
  .badge-red{background:#3d0f0f;color:#f85149}
  .badge-yellow{background:#3d2f0f;color:#e3b341}
  form label{display:block;color:#8b949e;font-size:12px;margin:12px 0 4px}
  form input,form select{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;
    padding:8px;border-radius:6px;width:100%;box-sizing:border-box;font-family:monospace}
  form input:focus{border-color:#58a6ff;outline:none}
  .section-title{color:#8b949e;font-size:11px;text-transform:uppercase;
    letter-spacing:1px;margin:20px 0 8px;border-top:1px solid #21262d;padding-top:12px}
  .btn{background:#238636;color:#fff;border:none;padding:10px 20px;border-radius:6px;
    cursor:pointer;font-size:14px;font-family:monospace}
  .btn:hover{background:#2ea043}
  .btn-secondary{background:#21262d;border:1px solid #30363d}
  .btn-secondary:hover{background:#30363d}
  pre{background:#0d1117;border:1px solid #30363d;padding:12px;border-radius:6px;
    overflow-x:auto;font-size:12px;line-height:1.5;max-height:500px;overflow-y:auto}
  .alert{padding:10px 16px;border-radius:6px;margin-bottom:12px;font-size:13px}
  .alert-success{background:#0f3d20;border:1px solid #3fb950;color:#3fb950}
  .alert-error{background:#3d0f0f;border:1px solid #f85149;color:#f85149}
</style>
"""

_NAV = """
<nav>
  <span style="color:#f0f6fc;font-weight:bold">⚡ Grid Bot</span>
  <a href="/">Dashboard</a>
  <a href="/config">Config</a>
  <a href="/log">Log</a>
</nav>
"""

DASHBOARD_TMPL = """
<!DOCTYPE html><html>
<head>""" + _BASE_STYLE + """</head>
<body>""" + _NAV + """
<div class="container">
  <h2>Dashboard</h2>

  {% if state %}
  <div class="card">
    <h3>สถานะบอท</h3>
    <div class="grid">
      <div class="stat">
        <div class="val {{ 'green' if state.stats.daily_profit_usdt >= 0 else 'red' }}">
          {{ '%+.4f'|format(state.stats.daily_profit_usdt) }} USDT
        </div>
        <div class="lbl">P&L วันนี้</div>
      </div>
      <div class="stat">
        <div class="val {{ 'green' if state.stats.realized_profit_usdt >= 0 else 'red' }}">
          {{ '%+.4f'|format(state.stats.realized_profit_usdt) }} USDT
        </div>
        <div class="lbl">P&L รวม</div>
      </div>
      <div class="stat">
        <div class="val blue">{{ state.stats.total_buys_filled }}</div>
        <div class="lbl">BUY fills</div>
      </div>
      <div class="stat">
        <div class="val blue">{{ state.stats.total_sells_filled }}</div>
        <div class="lbl">SELL fills</div>
      </div>
      <div class="stat">
        <div class="val yellow">{{ state.stats.resets }}</div>
        <div class="lbl">Resets</div>
      </div>
      <div class="stat">
        <div class="val">{{ state.direction }}</div>
        <div class="lbl">Direction</div>
      </div>
    </div>
  </div>

  <div class="card">
    <h3>Grid Range</h3>
    <table>
      <tr><th>Upper</th><td>${{ '%.4f'|format(state.grid_range.upper) }}</td>
          <th>Lower</th><td>${{ '%.4f'|format(state.grid_range.lower) }}</td>
          <th>Strategy</th><td>{{ state.grid_range.strategy_used }}</td></tr>
    </table>
  </div>

  <div class="card">
    <h3>Open Orders ({{ open_orders|length }})</h3>
    <table>
      <tr><th>Level</th><th>Side</th><th>Price</th><th>Qty</th><th>Status</th><th>Stop Loss</th></tr>
      {% for o in open_orders %}
      <tr>
        <td>{{ o.level_index }}</td>
        <td><span class="badge {{ 'badge-green' if o.side == 'BUY' else 'badge-red' }}">{{ o.side }}</span></td>
        <td>${{ '%.4f'|format(o.price) }}</td>
        <td>{{ '%.4f'|format(o.qty) }}</td>
        <td><span class="badge badge-yellow">{{ o.status }}</span></td>
        <td>{{ ('$%.4f'|format(o.stop_loss_price)) if o.stop_loss_price else '–' }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>

  <div style="color:#8b949e;font-size:12px">อัปเดตล่าสุด: {{ state.saved_at }}
    &nbsp;·&nbsp; Cycle {{ state.cycle }}</div>
  {% else %}
  <div class="card">
    <p style="color:#8b949e">บอทยังไม่ได้รัน หรือยังไม่มี state file</p>
  </div>
  {% endif %}
</div>
</body></html>
"""

CONFIG_TMPL = """
<!DOCTYPE html><html>
<head>""" + _BASE_STYLE + """</head>
<body>""" + _NAV + """
<div class="container">
  <h2>Config (.env)</h2>
  {% if msg %}
  <div class="alert alert-{{ msg_type }}">{{ msg }}</div>
  {% endif %}
  <form method="POST" action="/config/save">
  <div class="card">
    <h3>Market</h3>
    <div class="grid">
      <div>
        <label>MARKET</label>
        <select name="MARKET">
          <option value="futures" {{ 'selected' if cfg.MARKET=='futures' }}>futures</option>
          <option value="spot" {{ 'selected' if cfg.MARKET=='spot' }}>spot</option>
        </select>
      </div>
      <div><label>SYMBOL</label><input name="SYMBOL" value="{{ cfg.SYMBOL }}"></div>
      <div><label>LEVERAGE</label><input name="LEVERAGE" type="number" value="{{ cfg.LEVERAGE }}"></div>
      <div><label>MARGIN_TYPE</label>
        <select name="MARGIN_TYPE">
          <option value="ISOLATED" {{ 'selected' if cfg.MARGIN_TYPE=='ISOLATED' }}>ISOLATED</option>
          <option value="CROSSED" {{ 'selected' if cfg.MARGIN_TYPE=='CROSSED' }}>CROSSED</option>
        </select>
      </div>
    </div>

    <div class="section-title">Grid</div>
    <div class="grid">
      <div><label>GRID_COUNT</label><input name="GRID_COUNT" type="number" value="{{ cfg.GRID_COUNT }}"></div>
      <div><label>USDT_PER_GRID</label><input name="USDT_PER_GRID" type="number" step="0.1" value="{{ cfg.USDT_PER_GRID }}"></div>
      <div><label>UPPER_PRICE</label><input name="UPPER_PRICE" type="number" step="0.0001" value="{{ cfg.UPPER_PRICE }}"></div>
      <div><label>LOWER_PRICE</label><input name="LOWER_PRICE" type="number" step="0.0001" value="{{ cfg.LOWER_PRICE }}"></div>
      <div><label>GRID_MODE</label>
        <select name="GRID_MODE">
          <option value="trend" {{ 'selected' if cfg.GRID_MODE=='trend' }}>trend</option>
          <option value="both" {{ 'selected' if cfg.GRID_MODE=='both' }}>both</option>
        </select>
      </div>
      <div><label>STOP_LOSS_PCT (0=ปิด)</label><input name="STOP_LOSS_PCT" type="number" step="0.1" value="{{ cfg.STOP_LOSS_PCT }}"></div>
    </div>

    <div class="section-title">Trend Signal</div>
    <div class="grid">
      <div><label>TIMEFRAME</label>
        <select name="TIMEFRAME">
          {% for tf in ['1m','3m','5m','15m','30m'] %}
          <option value="{{ tf }}" {{ 'selected' if cfg.TIMEFRAME==tf }}>{{ tf }}</option>
          {% endfor %}
        </select>
      </div>
      <div><label>EMA_SHORT</label><input name="EMA_SHORT" type="number" value="{{ cfg.EMA_SHORT }}"></div>
      <div><label>EMA_LONG</label><input name="EMA_LONG" type="number" value="{{ cfg.EMA_LONG }}"></div>
      <div><label>RSI_PERIOD</label><input name="RSI_PERIOD" type="number" value="{{ cfg.RSI_PERIOD }}"></div>
      <div><label>EMA_MIN_GAP_PCT</label><input name="EMA_MIN_GAP_PCT" type="number" step="0.01" value="{{ cfg.EMA_MIN_GAP_PCT }}"></div>
    </div>

    <div class="section-title">Whipsaw Protection</div>
    <div class="grid">
      <div><label>TREND_CONFIRM_BARS</label><input name="TREND_CONFIRM_BARS" type="number" value="{{ cfg.TREND_CONFIRM_BARS }}"></div>
      <div><label>MIN_RESET_INTERVAL_SECONDS</label><input name="MIN_RESET_INTERVAL_SECONDS" type="number" value="{{ cfg.MIN_RESET_INTERVAL_SECONDS }}"></div>
      <div><label>HOLD_POSITION_ON_RESET</label>
        <select name="HOLD_POSITION_ON_RESET">
          <option value="true" {{ 'selected' if cfg.HOLD_POSITION_ON_RESET }}>true</option>
          <option value="false" {{ 'selected' if not cfg.HOLD_POSITION_ON_RESET }}>false</option>
        </select>
      </div>
    </div>

    <div class="section-title">Auto Range</div>
    <div class="grid">
      <div><label>AUTO_RANGE</label>
        <select name="AUTO_RANGE">
          <option value="true" {{ 'selected' if cfg.AUTO_RANGE }}>true</option>
          <option value="false" {{ 'selected' if not cfg.AUTO_RANGE }}>false</option>
        </select>
      </div>
      <div><label>RANGE_STRATEGY</label>
        <select name="RANGE_STRATEGY">
          {% for s in ['lookback','atr','bollinger'] %}
          <option value="{{ s }}" {{ 'selected' if cfg.RANGE_STRATEGY==s }}>{{ s }}</option>
          {% endfor %}
        </select>
      </div>
      <div><label>LOOKBACK_BARS</label><input name="LOOKBACK_BARS" type="number" value="{{ cfg.LOOKBACK_BARS }}"></div>
      <div><label>BUFFER_PCT</label><input name="BUFFER_PCT" type="number" step="0.01" value="{{ cfg.BUFFER_PCT }}"></div>
      <div><label>AUTO_REGRID</label>
        <select name="AUTO_REGRID">
          <option value="true" {{ 'selected' if cfg.AUTO_REGRID }}>true</option>
          <option value="false" {{ 'selected' if not cfg.AUTO_REGRID }}>false</option>
        </select>
      </div>
      <div><label>REGRID_THRESHOLD</label><input name="REGRID_THRESHOLD" type="number" step="0.05" value="{{ cfg.REGRID_THRESHOLD }}"></div>
    </div>

    <div class="section-title">Safety & Profit</div>
    <div class="grid">
      <div><label>DRY_RUN</label>
        <select name="DRY_RUN">
          <option value="true" {{ 'selected' if cfg.DRY_RUN }}>true (จำลอง)</option>
          <option value="false" {{ 'selected' if not cfg.DRY_RUN }}>false (เงินจริง ⚠)</option>
        </select>
      </div>
      <div><label>DAILY_PROFIT_TARGET_PCT</label><input name="DAILY_PROFIT_TARGET_PCT" type="number" value="{{ cfg.DAILY_PROFIT_TARGET_PCT }}"></div>
      <div><label>MAX_LOSS_PCT</label><input name="MAX_LOSS_PCT" type="number" value="{{ cfg.MAX_LOSS_PCT }}"></div>
      <div><label>POLL_INTERVAL_SECONDS</label><input name="POLL_INTERVAL_SECONDS" type="number" value="{{ cfg.POLL_INTERVAL_SECONDS }}"></div>
      <div><label>PRICE_ALERT_PCT</label><input name="PRICE_ALERT_PCT" type="number" step="0.1" value="{{ cfg.PRICE_ALERT_PCT }}"></div>
      <div><label>AUTO_STOP_PCT</label><input name="AUTO_STOP_PCT" type="number" step="0.1" value="{{ cfg.AUTO_STOP_PCT }}"></div>
    </div>

    <div style="margin-top:20px">
      <button type="submit" class="btn">💾 บันทึก .env</button>
      <span style="color:#8b949e;font-size:12px;margin-left:12px">
        หมายเหตุ: ต้องรีสตาร์ทบอทเพื่อให้ค่าใหม่มีผล
      </span>
    </div>
  </div>
  </form>
</div>
</body></html>
"""

LOG_TMPL = """
<!DOCTYPE html><html>
<head>""" + _BASE_STYLE + """</head>
<body>""" + _NAV + """
<div class="container">
  <h2>Log ({{ lines|length }} บรรทัดล่าสุด)</h2>
  <div class="card">
    <pre>{{ log_content }}</pre>
  </div>
</div>
</body></html>
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_state() -> Optional[dict]:
    if not STATE_FILE.exists():
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _read_env() -> dict:
    """อ่าน .env → dict ของ key=value ที่ไม่ใช่ comment."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def _write_env(new_values: dict) -> None:
    """เขียน .env ใหม่ โดยอัปเดตเฉพาะ key ที่ส่งมา คง comment + format เดิม."""
    if not ENV_FILE.exists():
        lines = []
    else:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    updated_keys = set()
    result_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            k = k.strip()
            if k in new_values:
                result_lines.append(f"{k}={new_values[k]}")
                updated_keys.add(k)
                continue
        result_lines.append(line)

    # เพิ่ม key ใหม่ที่ยังไม่มีใน .env
    for k, v in new_values.items():
        if k not in updated_keys:
            result_lines.append(f"{k}={v}")

    ENV_FILE.write_text("\n".join(result_lines) + "\n", encoding="utf-8")


class _CfgProxy:
    """Proxy object สำหรับ template — อ่านค่าจาก .env dict."""
    def __init__(self, env: dict):
        self._env = env

    def __getattr__(self, name: str):
        val = self._env.get(name, "")
        if val.lower() in ("true", "false"):
            return val.lower() == "true"
        try:
            return int(val) if "." not in val else float(val)
        except (ValueError, AttributeError):
            return val


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    state = _read_state()
    open_orders = []
    if state:
        open_orders = [o for o in state.get("orders", []) if o["status"] == "NEW"]
    return render_template_string(
        DASHBOARD_TMPL, state=state, open_orders=open_orders
    )


@app.route("/config")
def config_page():
    env = _read_env()
    return render_template_string(
        CONFIG_TMPL, cfg=_CfgProxy(env), msg=None, msg_type=None
    )


@app.route("/config/save", methods=["POST"])
def config_save():
    new_vals = {k: v for k, v in request.form.items()}
    try:
        _write_env(new_vals)
        env = _read_env()
        return render_template_string(
            CONFIG_TMPL, cfg=_CfgProxy(env),
            msg="✅ บันทึกแล้ว — รีสตาร์ทบอทเพื่อให้ค่าใหม่มีผล",
            msg_type="success",
        )
    except Exception as exc:
        env = _read_env()
        return render_template_string(
            CONFIG_TMPL, cfg=_CfgProxy(env),
            msg=f"❌ Error: {exc}",
            msg_type="error",
        )


@app.route("/log")
def log_page():
    lines = []
    if LOG_FILE.exists():
        all_lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        lines = all_lines[-100:]
    return render_template_string(
        LOG_TMPL, lines=lines, log_content="\n".join(lines)
    )


@app.route("/api/status")
def api_status():
    state = _read_state()
    if not state:
        return jsonify({"running": False})
    return jsonify({
        "running": True,
        "saved_at": state.get("saved_at"),
        "direction": state.get("direction"),
        "daily_profit": state.get("stats", {}).get("daily_profit_usdt", 0),
        "total_profit": state.get("stats", {}).get("realized_profit_usdt", 0),
        "open_orders": len([o for o in state.get("orders", []) if o["status"] == "NEW"]),
        "resets": state.get("stats", {}).get("resets", 0),
    })


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()
    print(f"🌐 Web UI: http://localhost:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
