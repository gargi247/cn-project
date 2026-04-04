#!/usr/bin/env python3
"""
dashboard/server.py
────────────────────
Minimal dashboard — reads metrics CSVs and bridge state, serves JSON.
The HTML is a single file that polls /api/state every 2s.
No heavy framework — just Python's built-in http.server + flask-socketio
for the real-time push.

This is the SECONDARY component — the primary output is the terminal.
"""

import os
import sys
import json
import time
import csv
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import LOG_DIR, METRICS_FILE, DASHBOARD_HOST, DASHBOARD_PORT

try:
    from flask import Flask, jsonify, send_from_directory
    from flask_socketio import SocketIO, emit
    FLASK_OK = True
except ImportError:
    FLASK_OK = False


class DashboardServer:
    def __init__(self, ran_sim, controller, bridge):
        self.ran_sim    = ran_sim
        self.controller = controller
        self.bridge     = bridge

        if not FLASK_OK:
            print("[Dashboard] Flask not available — skipping")
            return

        self.app = Flask(__name__, static_folder="static")
        self.sio = SocketIO(self.app, cors_allowed_origins="*",
                            async_mode="threading")
        self._setup_routes()

    def _setup_routes(self):
        app = self.app

        @app.route("/")
        def index():
            # Serve the dashboard HTML inline
            return DASHBOARD_HTML

        @app.route("/api/state")
        def api_state():
            return jsonify(self._build_state())

        @app.route("/api/metrics/ran")
        def api_ran_metrics():
            rows = self._read_csv(os.path.join(LOG_DIR, "ran_metrics.csv"))
            return jsonify(rows[-50:])   # last 50 rows

        @app.route("/api/metrics/transport")
        def api_transport_metrics():
            rows = self._read_csv(METRICS_FILE)
            return jsonify(rows[-50:])

        @app.route("/api/events")
        def api_events():
            br = self.bridge.get_state()
            return jsonify(br.get("events", [])[-20:])

    def _build_state(self):
        ran  = self.ran_sim.get_state()
        ctrl = self.controller.status()
        br   = self.bridge.get_state()
        return {
            "ts":         time.strftime("%H:%M:%S"),
            "ran":        ran,
            "transport":  ctrl,
            "bridge":     br,
        }

    def _read_csv(self, path):
        try:
            with open(path) as f:
                reader = csv.DictReader(f)
                return list(reader)
        except FileNotFoundError:
            return []

    def _push_loop(self):
        """Push state to all connected WebSocket clients every 1s."""
        while True:
            time.sleep(1)
            try:
                state = self._build_state()
                self.sio.emit("state", state)
            except Exception:
                pass

    def run(self):
        if not FLASK_OK:
            return
        t = threading.Thread(target=self._push_loop, daemon=True)
        t.start()
        self.sio.run(self.app, host=DASHBOARD_HOST,
                     port=DASHBOARD_PORT, debug=False)


# ── Inline dashboard HTML ─────────────────────────────────────────────────
# Minimal — just enough to show metrics. CN content is in the algorithms.
DASHBOARD_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>Digital Twin — CN Dashboard</title>
<style>
body{background:#0d0f14;color:#e2e4ea;font:13px/1.5 monospace;padding:20px}
h2{color:#36c;margin:16px 0 6px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:#151820;border:1px solid #1e2435;border-radius:8px;padding:14px}
.card h3{font-size:11px;text-transform:uppercase;color:#556;margin-bottom:8px}
table{width:100%;border-collapse:collapse;font-size:12px}
td,th{padding:4px 8px;text-align:left;border-bottom:1px solid #1e2435}
th{color:#778}
.good{color:#1D9E75}.warn{color:#BA7517}.bad{color:#D85A30}
#log{height:180px;overflow-y:auto;font-size:11px}
.ev{padding:3px 6px;border-left:2px solid #36c;margin-bottom:2px}
</style></head><body>
<h2>Digital Twin Network — CN Metrics</h2>
<div id="ts" style="color:#556;margin-bottom:12px">Loading…</div>
<div class="grid">
  <div class="card"><h3>RAN Layer</h3><div id="ran-tbl"></div></div>
  <div class="card"><h3>Transport / Routing</h3><div id="ctrl-tbl"></div></div>
</div>
<div class="card" style="margin-top:16px"><h3>Cross-Layer Events</h3>
  <div id="log"></div></div>
<script src="https://cdn.jsdelivr.net/npm/socket.io-client@4/dist/socket.io.min.js"></script>
<script>
const socket = io();
socket.on('state', s => {
  document.getElementById('ts').textContent = 'Last update: ' + s.ts;
  // RAN
  const m = s.ran?.metrics || {};
  let rt = '<table><tr><th>BS</th><th>RSS(dBm)</th><th>SINR(dB)</th><th>C(Mbps)</th><th>Status</th></tr>';
  for(const [bs, v] of Object.entries(m)){
    const cls = v.sinr_db>10?'good':v.sinr_db>5?'warn':'bad';
    const srv = bs===s.ran?.current_bs?' ◀':'';
    rt += `<tr><td>${bs}${srv}</td><td>${v.rss_dbm.toFixed(1)}</td>
           <td class="${cls}">${v.sinr_db.toFixed(1)}</td>
           <td>${v.capacity_mbps.toFixed(1)}</td>
           <td>${s.ran?.bs_states?.[bs]?.active?'active':'OFFLINE'}</td></tr>`;
  }
  document.getElementById('ran-tbl').innerHTML = rt + '</table>';
  // Transport paths
  const paths = s.transport?.paths || {};
  let pt = '<table><tr><th>Source→Dest</th><th>Path</th></tr>';
  for(const [pair, path] of Object.entries(paths)){
    pt += `<tr><td>${pair}</td><td>${Array.isArray(path)?path.join('→'):path}</td></tr>`;
  }
  document.getElementById('ctrl-tbl').innerHTML = pt + '</table>';
  // Events
  const evs = (s.bridge?.events || []).slice(-10).reverse();
  const log = document.getElementById('log');
  log.innerHTML = evs.map(e =>
    `<div class="ev">[${e.ts}] ${e.direction} → <b>${e.opcode}</b></div>`
  ).join('');
});
</script></body></html>"""
