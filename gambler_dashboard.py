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
    from flask import Flask, render_template_string, jsonify, request
except ImportError:
    print("ติดตั้ง Flask ก่อน:  pip install flask")
    raise

BASE_DIR      = Path(__file__).parent
STATE_FILE    = BASE_DIR / "gambler_state.json"
LIVE_FILE     = BASE_DIR / "gambler_live.json"
LOG_FILE      = BASE_DIR / "gambler_bot.log"
COPILOT_FILE   = BASE_DIR / "gambler_copilot.json"
SETTINGS_FILE  = BASE_DIR / "gambler_settings.json"
HISTORY_FILE   = BASE_DIR / "gambler_history.json"

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
  .btn-toggle{padding:3px 12px;border-radius:12px;font-size:12px;cursor:pointer;
    font-family:monospace;transition:opacity .15s}
  .btn-toggle:hover{opacity:.8}
  .btn-on{background:#0f3d20;color:#3fb950;border:1px solid #3fb950}
  .btn-off{background:#3d0f0f;color:#f85149;border:1px solid #f85149}
  input[type=range]{-webkit-appearance:none;width:100%;height:4px;background:#21262d;
    border-radius:2px;outline:none;cursor:pointer}
  input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;
    background:#58a6ff;border-radius:50%;cursor:pointer}
  .srow{display:flex;align-items:center;gap:10px;margin:10px 0}
  .srow label{color:#8b949e;font-size:12px;min-width:200px;flex-shrink:0}
  .srow .rval{color:#58a6ff;font-weight:bold;min-width:52px;text-align:right;font-size:13px}
  .preview-card{background:#0d1117;border:1px solid #30363d;border-radius:6px;
    padding:12px;text-align:center}
  .preview-card .pv{font-size:20px;font-weight:bold;line-height:1.2}
  .preview-card .pl{font-size:11px;color:#8b949e;margin-top:4px}
  .preview-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:4px}
  @media(max-width:700px){.preview-grid{grid-template-columns:1fr 1fr}}
  .balance-row{display:flex;align-items:center;gap:10px;margin-bottom:12px}
  .balance-row label{color:#8b949e;font-size:12px;white-space:nowrap}
  .balance-row input{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;
    padding:6px 10px;border-radius:6px;font-family:monospace;width:130px}
  .cp-range{margin-top:8px;font-size:12px;color:#8b949e;border-top:1px solid #21262d;padding-top:8px}
  .save-btn{background:#238636;color:#fff;border:none;padding:7px 18px;border-radius:6px;
    cursor:pointer;font-size:13px;font-family:monospace}
  .save-btn:hover{background:#2ea043}
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js"></script>
"""

_NAV = """
<nav>
  <span class="brand">Gambler 8x</span>
  <a href="/">Dashboard</a>
  <a href="/log">Log</a>
  <a href="/api/status">JSON</a>
  <span style="flex:1"></span>
  <button id="cpBtn" class="btn-toggle" onclick="toggleCopilot()">...</button>
</nav>
<script>
function _setCopilotBtn(enabled) {
  var btn = document.getElementById('cpBtn');
  if (!btn) return;
  if (enabled) {
    btn.textContent = 'AI co-pilot: ON';
    btn.className = 'btn-toggle btn-on';
  } else {
    btn.textContent = 'AI co-pilot: OFF';
    btn.className = 'btn-toggle btn-off';
  }
}
function toggleCopilot() {
  fetch('/api/copilot/toggle', {method:'POST'})
    .then(r => r.json())
    .then(d => _setCopilotBtn(d.enabled));
}
fetch('/api/copilot/state').then(r=>r.json()).then(d=>_setCopilotBtn(d.enabled));
</script>
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

  <!-- TF Score History Chart -->
  <div class="card">
    <h3>TF Score History
      <span style="font-size:11px;color:#8b949e">
        &nbsp;<span style="color:#79c0ff">&#9632;</span> 3m (w&times;1.0)
        &nbsp;<span style="color:#3fb950">&#9632;</span> 5m (w&times;1.5)
        &nbsp;<span style="color:#e3b341">&#9632;</span> 15m (w&times;2.0)
        &nbsp;<span style="color:#555">&#8212; &plusmn;3.5 threshold</span>
      </span>
    </h3>
    <canvas id="histChart" height="90"></canvas>
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

  <!-- Position Sizing Settings -->
  <div class="card">
    <h3>Position Sizing &amp; Targets
      <span style="font-size:11px;color:#8b949e;font-weight:normal;margin-left:8px">
        บันทึกแล้ว bot รับค่าทันที (ยกเว้น Leverage ต้องรีสตาร์ท)
      </span>
    </h3>

    <div class="srow">
      <label>Capital per trade (%)</label>
      <input type="range" id="s_capital_pct" min="5" max="100" step="5"
             value="{{ (settings.capital_pct*100)|int }}" oninput="syncVal(this,'v_capital_pct');updatePreview()">
      <span class="rval"><span id="v_capital_pct">{{ (settings.capital_pct*100)|int }}</span>%</span>
    </div>
    <div class="srow">
      <label>Leverage</label>
      <input type="range" id="s_leverage" min="1" max="20" step="1"
             value="{{ settings.leverage }}" oninput="syncVal(this,'v_leverage');updatePreview()">
      <span class="rval"><span id="v_leverage">{{ settings.leverage }}</span>x</span>
    </div>
    <div class="srow">
      <label>Score threshold (entry)</label>
      <input type="range" id="s_score_threshold" min="1.0" max="8.0" step="0.5"
             value="{{ settings.score_threshold }}" oninput="syncVal(this,'v_score_threshold')">
      <span class="rval"><span id="v_score_threshold">{{ settings.score_threshold }}</span></span>
    </div>
    <div class="srow">
      <label>TP target (ROE%)</label>
      <input type="range" id="s_tp_roe_pct" min="10" max="100" step="5"
             value="{{ settings.tp_roe_pct|int }}" oninput="syncVal(this,'v_tp_roe_pct');updatePreview()">
      <span class="rval">+<span id="v_tp_roe_pct">{{ settings.tp_roe_pct|int }}</span>%</span>
    </div>
    <div class="srow">
      <label>SL Hard (ROE%)</label>
      <input type="range" id="s_sl_hard_roe_pct" min="5" max="50" step="5"
             value="{{ settings.sl_hard_roe_pct|int }}" oninput="syncVal(this,'v_sl_hard_roe_pct');updatePreview()">
      <span class="rval">-<span id="v_sl_hard_roe_pct">{{ settings.sl_hard_roe_pct|int }}</span>%</span>
    </div>

    <div style="margin-top:14px;display:flex;align-items:center;gap:12px">
      <button class="save-btn" onclick="saveSettings()">Save to bot</button>
      <span id="save_msg" style="font-size:12px"></span>
    </div>
  </div>

  <!-- Trade Preview -->
  <div class="card">
    <h3>Trade Preview <span style="font-size:11px;color:#8b949e;font-weight:normal">(ประมาณการณ์)</span></h3>
    <div class="balance-row">
      <label>Balance (USDT)</label>
      <input type="number" id="p_balance" value="100" step="10" min="1"
             oninput="updatePreview()" placeholder="100">
    </div>
    <div class="preview-grid" id="preview_grid"></div>
    <div class="cp-range" id="preview_copilot"></div>
  </div>

</div>
<script>
function syncVal(el, targetId) {
  document.getElementById(targetId).textContent = el.value;
}
function pCard(val, label, cls) {
  return '<div class="preview-card"><div class="pv '+cls+'">'+val+'</div><div class="pl">'+label+'</div></div>';
}
function fmt(n) { return n >= 100 ? n.toFixed(1) : n.toFixed(2); }
function updatePreview() {
  var bal   = parseFloat(document.getElementById('p_balance').value) || 100;
  var capPct= parseFloat(document.getElementById('s_capital_pct').value) / 100;
  var lev   = parseInt(document.getElementById('s_leverage').value);
  var tp    = parseFloat(document.getElementById('s_tp_roe_pct').value);
  var sl    = parseFloat(document.getElementById('s_sl_hard_roe_pct').value);
  var margin   = bal * capPct;
  var notional = margin * lev;
  var tpGain   = margin * tp / 100;
  var slLoss   = margin * sl / 100;
  var rr       = tp / sl;
  var rrCls    = rr >= 2 ? 'green' : (rr >= 1 ? 'yellow' : 'red');
  document.getElementById('preview_grid').innerHTML =
    pCard('$'+fmt(margin), 'Margin (USDT)', 'blue') +
    pCard('$'+fmt(notional), 'Notional ('+lev+'x)', 'blue') +
    pCard('+$'+fmt(tpGain)+'<br><small>+'+tp+'% ROE</small>', 'TP Gain', 'green') +
    pCard('-$'+fmt(slLoss)+'<br><small>-'+sl+'% ROE</small>', 'Max Loss (SL)', 'red') +
    pCard(rr.toFixed(2)+':1', 'R:R Ratio', rrCls) +
    pCard(tp+'% / '+sl+'% = '+fmt(notional*tp/100/100)+' USDT per 1%', 'Fee Impact', 'gray');
  fetch('/api/copilot/state').then(r=>r.json()).then(function(d) {
    var el = document.getElementById('preview_copilot');
    if (d.enabled) {
      var mn = margin*0.5, mx = margin*1.5;
      el.innerHTML =
        '<span class="badge b-blue">co-pilot ON</span>' +
        ' &nbsp;Margin: <b>$'+fmt(mn)+'</b> – <b>$'+fmt(mx)+'</b>' +
        ' &nbsp;|&nbsp; TP: <span class="green">+$'+fmt(mn*tp/100)+' – +$'+fmt(mx*tp/100)+'</span>' +
        ' &nbsp;|&nbsp; SL: <span class="red">-$'+fmt(mn*sl/100)+' – -$'+fmt(mx*sl/100)+'</span>';
    } else {
      el.innerHTML = '<span class="badge b-gray">co-pilot OFF</span>' +
        ' &nbsp;Fixed margin: <b>$'+fmt(margin)+'</b> &nbsp;Notional: <b>$'+fmt(notional)+'</b>';
    }
  });
}
function saveSettings() {
  var data = {
    capital_pct:     parseFloat(document.getElementById('s_capital_pct').value) / 100,
    leverage:        parseInt(document.getElementById('s_leverage').value),
    tp_roe_pct:      parseFloat(document.getElementById('s_tp_roe_pct').value),
    sl_hard_roe_pct: parseFloat(document.getElementById('s_sl_hard_roe_pct').value),
    score_threshold: parseFloat(document.getElementById('s_score_threshold').value)
  };
  fetch('/api/settings/save', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)})
  .then(r=>r.json()).then(function(d) {
    var m = document.getElementById('save_msg');
    m.textContent = d.ok ? 'Saved!' : 'Error: '+d.error;
    m.style.color = d.ok ? '#3fb950' : '#f85149';
    setTimeout(function(){m.textContent='';}, 2500);
  });
}
updatePreview();

// ── TF Score Chart ──
async function loadChart() {
  var res = await fetch('/api/history');
  var h = await res.json();
  if (!h.length) return;
  var labels = h.map(function(d){return d.ts;});
  function mk(key, color) {
    return {label:key, data:h.map(function(d){return d[key];}),
      borderColor:color, borderWidth:1.5, pointRadius:0, tension:0.3, fill:false};
  }
  new Chart(document.getElementById('histChart').getContext('2d'), {
    type: 'line',
    data: {labels: labels, datasets: [mk('3m','#79c0ff'), mk('5m','#3fb950'), mk('15m','#e3b341')]},
    options: {
      animation: false,
      plugins: {
        legend: {labels:{color:'#8b949e',font:{family:'monospace',size:11},boxWidth:12}},
        annotation: {annotations: {
          hi:   {type:'line',yMin: 3.5,yMax: 3.5,borderColor:'#ffffff33',borderWidth:1,borderDash:[4,3]},
          lo:   {type:'line',yMin:-3.5,yMax:-3.5,borderColor:'#ffffff33',borderWidth:1,borderDash:[4,3]},
          zero: {type:'line',yMin:0,   yMax:0,   borderColor:'#ffffff18',borderWidth:1},
        }}
      },
      scales: {
        x: {ticks:{color:'#8b949e',font:{family:'monospace',size:10},maxTicksLimit:10},grid:{color:'#21262d'}},
        y: {min:-8,max:8,ticks:{color:'#8b949e',font:{family:'monospace',size:10}},grid:{color:'#21262d'}}
      }
    }
  });
}
loadChart();

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

def _read_copilot_enabled() -> bool:
    data = _read_json(COPILOT_FILE)
    if data is not None:
        return bool(data.get("enabled", False))
    return False  # default OFF


def _write_copilot_enabled(state: bool) -> None:
    COPILOT_FILE.write_text(json.dumps({"enabled": state}), encoding="utf-8")


_SETTINGS_DEFAULTS: dict = {
    "capital_pct":     0.30,
    "leverage":        8,
    "tp_roe_pct":      30.0,
    "sl_hard_roe_pct": 30.0,
    "score_threshold": 3.5,
}


def _read_settings() -> dict:
    data = _read_json(SETTINGS_FILE)
    if data:
        return {**_SETTINGS_DEFAULTS, **data}
    return dict(_SETTINGS_DEFAULTS)


def _write_settings(values: dict) -> None:
    merged = {**_SETTINGS_DEFAULTS, **values}
    SETTINGS_FILE.write_text(json.dumps(merged, indent=2), encoding="utf-8")


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

    settings = _read_settings()

    class _S:
        pass
    s = _S()
    for k, v in settings.items():
        setattr(s, k, v)

    return render_template_string(DASH_TMPL, live=live, pos=pos, summary=summary, settings=s)


@app.route("/log")
def log_page():
    lines: list[str] = []
    if LOG_FILE.exists():
        all_lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        lines = all_lines[-200:]
    return render_template_string(LOG_TMPL, content="\n".join(lines), count=len(lines))


@app.route("/api/settings")
def settings_get():
    return jsonify(_read_settings())


@app.route("/api/settings/save", methods=["POST"])
def settings_save():
    try:
        data = request.get_json(force=True)
        _write_settings(data)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/copilot/state")
def copilot_state():
    return jsonify({"enabled": _read_copilot_enabled()})


@app.route("/api/copilot/toggle", methods=["POST"])
def copilot_toggle():
    new_state = not _read_copilot_enabled()
    _write_copilot_enabled(new_state)
    return jsonify({"enabled": new_state})


@app.route("/api/history")
def api_history():
    return jsonify(_read_json(HISTORY_FILE) or [])


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
