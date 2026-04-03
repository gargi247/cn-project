"""
unified_dashboard.py
Single Flask app serving BOTH twins + cross-layer bridge.
Run: sudo python3 unified_dashboard.py
Open: http://localhost:5000

Architecture:
  - RAN Twin (simulator + anomaly detection + Q-learning optimizer + LLM)
  - Transport Twin (Mininet + collector + Dijkstra + OpenFlow)
  - Cross-Layer Bridge (handoff → reroute coupling)
  - Unified dashboard with 3 tabs
"""

from flask import Flask, jsonify, request, render_template_string
import threading, time, os, sys, subprocess, logging

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# ── Initialize components ──────────────────────────────────────────────────
from data_layer.storage import NetworkDatabase
from twin_core.control_loop import ClosedLoopController
from ran_twin import RANTwin
from cross_layer_bridge import CrossLayerBridge

# Transport twin
db = NetworkDatabase('dtn_network.db')
transport_ctrl = ClosedLoopController(
    db, loop_interval=5.0,
    latency_threshold=1500.0,
    loss_threshold=10.0
)
transport_ctrl.start()

# RAN twin
ran = RANTwin(num_ues=30, seed=42, optimizer_mode='rule')
ran.start()

# Cross-layer bridge
bridge = CrossLayerBridge(ran, db, transport_ctrl)
bridge.start()

app = Flask(__name__)


# ── Transport Twin API (Phase 1+2) ─────────────────────────────────────────

@app.route('/api/topology')
def get_topology():
    return jsonify({'nodes': db.get_topology_nodes(),
                    'links': db.get_topology_links()})

@app.route('/api/stats/links')
def get_link_stats():
    return jsonify(db.get_link_statistics())


@app.route('/api/chart/overview')
def get_overview_chart():
    import plotly.graph_objs as go, plotly.utils, json
    stats = db.get_link_statistics()
    if not stats:
        return jsonify({'error': 'No data'})
    stats = sorted(stats, key=lambda x: x['avg_latency'] or 0, reverse=True)[:10]
    links = [f"{s['node_src']}→{s['node_dst']}" for s in stats]
    fig = go.Figure()
    fig.add_trace(go.Bar(name='Min', x=links,
        y=[s['min_latency'] or 0 for s in stats], marker_color='lightblue'))
    fig.add_trace(go.Bar(name='Avg', x=links,
        y=[s['avg_latency'] or 0 for s in stats], marker_color='steelblue'))
    fig.add_trace(go.Bar(name='Max', x=links,
        y=[s['max_latency'] or 0 for s in stats], marker_color='darkblue'))
    fig.update_layout(title='Latency Distribution', barmode='group',
        template='plotly_white', height=350)
    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)



@app.route('/api/phase2/status')
def get_phase2_status():
    try:
        status = transport_ctrl.get_status()
        transport_ctrl.optimizer.build_graph()
        graph = transport_ctrl.optimizer.graph
        status['graph'] = {
            'nodes': list(graph.nodes),
            'edges': [
                {'src': s, 'dst': d, 'weight': round(w, 1), 'link_id': l}
                for s, nbrs in graph.edges.items()
                for d, w, l in nbrs
            ]
        }
        status['verification_results'] = transport_ctrl.verification_results[-5:]
        return jsonify(status)
    except Exception as e:
        return jsonify({'error': str(e), 'active_congestion': [],
                       'active_reroutes': {}, 'stats': {}})

@app.route('/api/phase2/inject/<host>')
def inject_host(host):
    pid = _find_pid(host)
    if not pid:
        return jsonify({'success': False, 'error': f'Host {host} not found'})
    iface = f"{host}-eth0"
    subprocess.run(
        f"sudo nsenter -t {pid} -n -- tc qdisc del dev {iface} root 2>/dev/null; true",
        shell=True)
    r = subprocess.run(
        f"sudo nsenter -t {pid} -n -- tc qdisc add dev {iface} root netem delay 500ms loss 20%",
        shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        r2 = subprocess.run(
            f"nsenter -t {pid} -n -- tc qdisc add dev {iface} root netem delay 500ms loss 20%",
            shell=True, capture_output=True, text=True)
        success = r2.returncode == 0
    else:
        success = True
    db.insert_event('congestion', 'warning', host, f'Demo: +500ms, 20% loss on {host}')
    return jsonify({'success': success, 'host': host,
                   'message': f'Congestion on {host}. Detection ~10s.'})

@app.route('/api/phase2/clear/<host>')
def clear_host(host):
    iface = f"{host}-eth0"
    pids = _find_all_pids(host)
    for pid in pids:
        for prefix in ['sudo ', '']:
            subprocess.run(
                f"{prefix}nsenter -t {pid} -n -- "
                f"tc qdisc del dev {iface} root 2>/dev/null || true",
                shell=True)
    db.insert_event('recovery', 'info', host, f'Congestion cleared on {host}')
    return jsonify({'success': True, 'host': host,
                   'message': f'{host} cleared. Recovery ~15s.'})

@app.route('/api/phase2/inject_backbone/<link>')
def inject_backbone(link):
    success = transport_ctrl.inject_backbone_congestion(link, delay_ms=200, loss_pct=15.0)
    return jsonify({'success': success, 'link': link,
                   'message': f'Backbone {link} congested. ~10s detection.' if success
                              else 'Failed'})

@app.route('/api/phase2/clear_backbone/<link>')
def clear_backbone(link):
    success = transport_ctrl.clear_backbone_congestion(link)
    return jsonify({'success': success, 'link': link})

@app.route('/api/phase2/reset')
def reset_network():
    for host in ['h1','h2','h3','h4','h5','h6']:
        iface = f"{host}-eth0"
        for pid in _find_all_pids(host):
            for prefix in ['sudo ', '']:
                subprocess.run(
                    f"{prefix}nsenter -t {pid} -n -- "
                    f"tc qdisc del dev {iface} root 2>/dev/null || true",
                    shell=True)
    transport_ctrl.active_congestion = {}
    transport_ctrl.optimizer.active_reroutes = {}
    for sw in ['s1','s2','s3']:
        subprocess.run(f"sudo ovs-ofctl del-flows {sw}", shell=True, capture_output=True)
        subprocess.run(f"sudo ovs-ofctl add-flow {sw} action=flood",
                      shell=True, capture_output=True)
    db.insert_event('recovery', 'info', None, 'Full reset: all congestion cleared')
    return jsonify({'success': True, 'message': 'Network reset complete'})

@app.route('/api/phase2/events')
def get_events():
    try:
        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM network_events ORDER BY timestamp DESC LIMIT 30")
        return jsonify([dict(r) for r in cursor.fetchall()])
    except Exception as e:
        return jsonify({'error': str(e)})


# ── RAN Twin API ───────────────────────────────────────────────────────────

@app.route('/api/ran/status')
def ran_status():
    return jsonify(ran.get_status())

@app.route('/api/ran/records')
def ran_records():
    return jsonify(ran.get_records())

@app.route('/api/ran/anomalies')
def ran_anomalies():
    return jsonify(ran.get_anomalies(15))

@app.route('/api/ran/bs_summary')
def ran_bs():
    return jsonify(ran.get_bs_summary())

@app.route('/api/ran/actions')
def ran_actions():
    return jsonify(ran.get_actions(15))

@app.route('/api/ran/optimizer', methods=['GET', 'POST'])
def ran_optimizer():
    if request.method == 'POST':
        ran.set_optimizer_mode(request.json.get('mode', 'rule'))
    return jsonify(ran.get_optimizer_info())

@app.route('/api/ran/explain', methods=['POST'])
def ran_explain():
    return jsonify(ran.explain_latest())

@app.route('/api/ran/whatif', methods=['POST'])
def ran_whatif():
    q = request.json.get('question', '')
    return jsonify({'answer': ran.what_if(q) if q else 'Enter a question.'})

@app.route('/api/ran/fault', methods=['POST'])
def ran_fault():
    d = request.json
    if d.get('action', 'fail') == 'fail':
        ran.fail_bs(d['bs_id'])
        return jsonify({'status': f"{d['bs_id']} failed"})
    ran.restore_bs(d['bs_id'])
    return jsonify({'status': f"{d['bs_id']} restored"})


# ── Cross-Layer API ────────────────────────────────────────────────────────

@app.route('/api/bridge/events')
def bridge_events():
    return jsonify(bridge.get_events(20))

@app.route('/api/bridge/stats')
def bridge_stats():
    return jsonify(bridge.get_stats())

@app.route('/api/bridge/topology')
def bridge_topology():
    return jsonify(bridge.get_topology_mapping())


# ── Helpers ────────────────────────────────────────────────────────────────

def _find_pid(host_name):
    pids = _find_all_pids(host_name)
    return pids[0] if pids else None

def _find_all_pids(host_name):
    found = []
    try:
        for pid_dir in os.listdir('/proc'):
            if not pid_dir.isdigit():
                continue
            try:
                with open(f'/proc/{pid_dir}/cmdline', 'rb') as f:
                    cmdline = f.read().decode('utf-8', errors='ignore')
                if f'mininet:{host_name}' in cmdline:
                    found.append(int(pid_dir))
            except Exception:
                continue
    except Exception:
        pass
    return found


# ── Main Dashboard HTML ────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(UNIFIED_HTML)


UNIFIED_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>6G Digital Twin Network — Unified Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0f1117;color:#e2e8f0}
header{padding:14px 22px;border-bottom:1px solid #2d3748;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
header h1{font-size:18px;font-weight:700;color:#e2e8f0}
.live{width:9px;height:9px;border-radius:50%;background:#48bb78;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.badge{font-size:11px;padding:3px 10px;border-radius:12px;background:#1a365d;color:#90cdf4}
.gbadge{font-size:11px;padding:3px 10px;border-radius:12px;background:#1c4532;color:#68d391}
.xbadge{font-size:11px;padding:3px 10px;border-radius:12px;background:#44337a;color:#d6bcfa}

/* Tabs */
.tabs{display:flex;gap:4px;padding:10px 22px;border-bottom:1px solid #2d3748;background:#141720}
.tab{padding:8px 20px;border-radius:8px 8px 0 0;cursor:pointer;font-size:13px;font-weight:600;
     border:1px solid transparent;color:#718096;transition:all 0.2s}
.tab.active{background:#1a202c;border-color:#2d3748;border-bottom-color:#1a202c;color:#e2e8f0}
.tab:hover:not(.active){color:#a0aec0}
.tab-content{display:none;padding:18px 22px}
.tab-content.active{display:block}

/* Cards */
.card{background:#1a202c;border:1px solid #2d3748;border-radius:10px;padding:16px;margin-bottom:16px}
.card h2,.sect h2{font-size:11px;font-weight:600;color:#a0aec0;text-transform:uppercase;
                   letter-spacing:.05em;margin-bottom:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:700px){.grid2{grid-template-columns:1fr}}
.grid4{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px}
.kpi{background:#1a202c;border:1px solid #2d3748;border-radius:10px;padding:13px}
.kpi label{font-size:10px;color:#718096;text-transform:uppercase;letter-spacing:.05em;display:block;margin-bottom:4px}
.kpi .val{font-size:22px;font-weight:700}
.ok{color:#68d391}.mid{color:#f6ad55}.bad{color:#fc8181}

/* Tables */
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;color:#718096;font-weight:500;padding:5px 8px;border-bottom:1px solid #2d3748}
td{padding:5px 8px;border-bottom:1px solid #1e2533}
.arow td{color:#fc8181}
canvas{max-height:200px}

/* Buttons */
.btn{background:#2b6cb0;color:#fff;border:none;padding:7px 14px;border-radius:6px;
     cursor:pointer;font-size:12px;font-weight:600;transition:all 0.2s}
.btn:hover{background:#2c5282;transform:translateY(-1px)}
.btn-red{background:#c53030}.btn-red:hover{background:#9b2c2c}
.btn-green{background:#276749}.btn-green:hover{background:#1c4532}
.btn-purple{background:#6b46c1}.btn-purple:hover{background:#553c9a}
.demo-box{background:#141720;border:1px solid #f6e05e44;border-radius:8px;padding:14px;margin-bottom:14px}
.demo-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;align-items:center}
.demo-row span{font-size:12px;color:#718096}
input[type=text]{background:#2d3748;border:1px solid #4a5568;color:#e2e8f0;
  padding:7px 11px;border-radius:6px;font-size:12px;width:100%;margin-top:8px}
.lbox{background:#0f1117;border-radius:8px;padding:10px 12px;font-size:13px;
      line-height:1.6;color:#cbd5e0;min-height:50px;margin-top:10px}
.status-msg{font-size:12px;color:#718096;margin-top:8px;min-height:18px}

/* Route pills */
.route-pill{display:inline-block;background:#1a365d44;border:1px solid #2b6cb066;
  border-radius:6px;padding:5px 10px;font-family:monospace;font-size:12px;
  color:#90cdf4;margin:3px 0;display:block}

/* Cross-layer */
.cl-event{background:#2d1b4e;border:1px solid #44337a;border-radius:6px;
  padding:8px 12px;margin:4px 0;font-size:12px}
.cl-handoff{border-color:#2b6cb0;background:#1a2e4a}
.cl-transport{border-color:#c53030;background:#2d1515}
.tag{display:inline-block;font-size:10px;padding:2px 6px;border-radius:4px;font-weight:600;margin-right:4px}
.tag-HANDOFF{background:#2a4365;color:#90cdf4}
.tag-POWER_BOOST{background:#2d3a0e;color:#c6f135}
.tag-POWER_REDUCE{background:#3d2a00;color:#f6ad55}
.tag-LOAD_BALANCE{background:#3d1f5c;color:#d6bcfa}
.fb{font-size:11px;padding:4px 10px;border-radius:4px;cursor:pointer;
  border:1px solid #4a5568;background:#2d3748;color:#e2e8f0;margin:3px}
.fb.failed{background:#9b2c2c;border-color:#fc8181}
</style></head>
<body>
<header>
  <div class="live"></div>
  <h1>6G Digital Twin Network</h1>
  <span class="badge">Transport Twin</span>
  <span class="badge">RAN Twin</span>
  <span class="xbadge">Cross-Layer Bridge</span>
  <span class="gbadge">Live</span>
</header>

<div class="tabs">
  <div class="tab active" onclick="showTab('transport')">🔀 Transport Twin (Phase 1+2)</div>
  <div class="tab" onclick="showTab('ran')">📡 RAN Twin (6G Radio)</div>
  <div class="tab" onclick="showTab('bridge')">🌉 Cross-Layer Bridge</div>
</div>

<!-- ═══ TAB 1: TRANSPORT TWIN ═══════════════════════════════════════════ -->
<div id="tab-transport" class="tab-content active">

  <div class="grid4" id="transport-kpis"></div>

  <div class="grid2">
    <div class="card"><div id="overview-chart" style="min-height:60px">Loading...</div></div>
    <div class="card">
      <h2>Network Topology</h2>
      <div id="topology-info" style="font-size:12px;color:#718096">Loading...</div>
    </div>
  </div>

  <div class="card">
    <h2>Phase 2: Closed-Loop Control &nbsp;<span id="health-badge" style="font-size:11px;padding:2px 8px;border-radius:10px;background:#1c4532;color:#68d391">All Healthy</span></h2>
    <div class="grid4" style="margin-bottom:14px">
      <div class="kpi"><label>Control Loops</label><div class="val ok" id="p2-loops">0</div></div>
      <div class="kpi"><label>Congested</label><div class="val bad" id="p2-cong">0</div></div>
      <div class="kpi"><label>Reroutes</label><div class="val mid" id="p2-reroutes">0</div></div>
      <div class="kpi"><label>Verified</label><div class="val ok" id="p2-verified">0</div></div>
    </div>

    <div class="demo-box">
      <strong style="color:#f6e05e;font-size:13px">🎮 Demo Controls</strong>
      <div class="demo-row">
        <span>Backbone inject:</span>
        <button class="btn btn-red" onclick="injectBackbone('s1-s2')">s1↔s2</button>
        <button class="btn btn-red" onclick="injectBackbone('s2-s3')">s2↔s3</button>
        <button class="btn btn-red" onclick="injectBackbone('s1-s3')">s1↔s3</button>
        <span style="margin-left:8px">Clear:</span>
        <button class="btn btn-green" onclick="clearBackbone('s1-s2')">s1↔s2</button>
        <button class="btn btn-green" onclick="clearBackbone('s2-s3')">s2↔s3</button>
        <button class="btn btn-green" onclick="clearBackbone('s1-s3')">s1↔s3</button>
        <button class="btn btn-purple" onclick="resetNetwork()" style="margin-left:12px">🔄 Reset All</button>
      </div>
      <div id="transport-msg" class="status-msg"></div>
    </div>

    <h2 style="margin-bottom:8px">Active Congestion</h2>
    <div id="congestion-table">Loading...</div>

    <h2 style="margin-top:14px;margin-bottom:8px">Active Reroutes (Dijkstra)</h2>
    <div id="reroutes-section">No reroutes</div>

    <h2 style="margin-top:14px;margin-bottom:8px">Verification Results</h2>
    <div id="verify-section" style="font-size:12px;color:#718096">No verifications yet</div>

    <h2 style="margin-top:14px;margin-bottom:8px">Recent Events</h2>
    <div id="events-section">Loading...</div>
  </div>
</div>

<!-- ═══ TAB 2: RAN TWIN ══════════════════════════════════════════════════ -->
<div id="tab-ran" class="tab-content">

  <div class="grid4" id="ran-kpis"></div>

  <div class="grid2">
    <div class="card"><h2>SINR Distribution</h2><canvas id="sinr-chart"></canvas></div>
    <div class="card">
      <h2>Base Station Summary</h2>
      <table><thead><tr><th>BS</th><th>Type</th><th>UEs</th><th>SINR</th><th>Tput</th></tr></thead>
      <tbody id="bs-table"></tbody></table>
    </div>
  </div>

  <div class="card">
    <h2>Recent Anomalies</h2>
    <table><thead><tr><th>UE</th><th>BS</th><th>SINR</th><th>Tput</th><th>Reason</th></tr></thead>
    <tbody id="anomaly-table"></tbody></table>
  </div>

  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
      <h2 style="margin:0">Self-Healing Optimizer</h2>
      <div style="display:flex;gap:6px">
        <button class="btn" id="btn-rule" onclick="setOptMode('rule')">Rule-based</button>
        <button class="btn" style="background:#2d3748" id="btn-ml" onclick="setOptMode('ml')">Q-Learning</button>
      </div>
    </div>
    <div id="opt-desc" style="font-size:12px;color:#718096;margin-bottom:10px"></div>
    <table><thead><tr><th>Action</th><th>UE</th><th>From BS</th><th>To BS</th><th>Reason</th></tr></thead>
    <tbody id="action-table"></tbody></table>
  </div>

  <div class="card">
    <h2>Fault Injection</h2>
    <p style="font-size:11px;color:#718096;margin-bottom:10px">Fail a base station and watch the optimizer self-heal.</p>
    <div id="fault-buttons"></div>
  </div>

  <div class="card">
    <h2>LLM Anomaly Explainer <span class="gbadge">Novel</span></h2>
    <div class="lbox" id="explain-box">Click to explain the latest anomaly.</div>
    <div style="margin-top:8px"><button class="btn" onclick="explainLatest()">Explain latest anomaly</button></div>
  </div>

  <div class="card">
    <h2>What-If Query</h2>
    <input type="text" id="wi-input" placeholder="What happens if BS_MAC_0 fails?">
    <div style="margin-top:8px"><button class="btn" onclick="askWhatIf()">Ask</button></div>
    <div class="lbox" id="wi-box">Ask any question about the live RAN.</div>
  </div>
</div>

<!-- ═══ TAB 3: CROSS-LAYER BRIDGE ═══════════════════════════════════════ -->
<div id="tab-bridge" class="tab-content">

  <div class="grid4" id="bridge-kpis"></div>

  <div class="card">
    <h2>Cross-Layer Architecture</h2>
    <div style="font-size:13px;color:#a0aec0;line-height:1.7">
      <strong style="color:#e2e8f0">Novel contribution:</strong> This bridge couples the RAN twin (radio access)
      with the transport twin (IP switching) into a single end-to-end closed loop.<br><br>
      <strong style="color:#90cdf4">RAN → Transport:</strong> When a UE handoff crosses switch boundaries
      (e.g. BS_MAC_0[s1] → BS_MAC_2[s2]), the bridge triggers a transport layer reroute check
      to optimise the IP path for that UE's data flow.<br><br>
      <strong style="color:#fc8181">Transport → RAN:</strong> When transport congestion is detected
      (e.g. s1↔s2 link overloaded), the bridge hints the RAN optimizer to consider load-balancing
      UEs away from base stations served by the congested switch.
    </div>
  </div>

  <div class="card">
    <h2>BS → Host Mapping</h2>
    <table><thead><tr><th>Base Station</th><th>Type</th><th>Transport Host</th><th>Switch</th></tr></thead>
    <tbody id="mapping-table"></tbody></table>
  </div>

  <div class="card">
    <h2>Cross-Layer Events (Live)</h2>
    <div id="bridge-events">Loading...</div>
  </div>
</div>

<script>
// ── Tab switching ─────────────────────────────────────────────────────────
function showTab(name) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.currentTarget.classList.add('active');
}

// ── SINR chart ────────────────────────────────────────────────────────────
const sinrChart = new Chart(document.getElementById('sinr-chart').getContext('2d'), {
  type: 'bar',
  data: {
    labels: ['<-20','-20–-10','-10–0','0–10','10–20','>20'],
    datasets: [{
      data: [0,0,0,0,0,0],
      backgroundColor: ['#fc8181','#f6ad55','#faf089','#68d391','#4299e1','#9f7aea'],
      borderRadius: 3
    }]
  },
  options: {
    plugins: {legend: {display: false}},
    scales: {
      x: {ticks: {color:'#718096'}, grid: {color:'#2d3748'}},
      y: {ticks: {color:'#718096'}, grid: {color:'#2d3748'}, beginAtZero: true}
    }
  }
});

// ── Fault buttons ─────────────────────────────────────────────────────────
const BSS = ['BS_MAC_0','BS_MAC_1','BS_MAC_2','BS_MAC_3','BS_MAC_4','BS_MIC_0','BS_MIC_1','BS_MIC_2'];
const failedBS = new Set();
const fbw = document.getElementById('fault-buttons');
BSS.forEach(id => {
  const b = document.createElement('button');
  b.className = 'fb'; b.textContent = id; b.id = 'fb_' + id;
  b.onclick = () => toggleFault(id, b);
  fbw.appendChild(b);
});
async function toggleFault(id, btn) {
  const act = failedBS.has(id) ? 'restore' : 'fail';
  await fetch('/api/ran/fault', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({bs_id: id, action: act})});
  if (act === 'fail') { failedBS.add(id); btn.classList.add('failed'); btn.textContent = id + ' ✕'; }
  else { failedBS.delete(id); btn.classList.remove('failed'); btn.textContent = id; }
}

// ── Optimizer mode ────────────────────────────────────────────────────────
const OPT_DESCS = {
  rule: 'Rule-based: SINR < −10dB → handoff | BS overloaded → offload | marginal → power boost. Deterministic, fully explainable.',
  ml:   'Q-Learning: learns optimal actions through experience. Watch ε decrease and rewards improve over ~50 ticks.'
};
async function setOptMode(m) {
  await fetch('/api/ran/optimizer', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({mode: m})});
  document.getElementById('btn-rule').style.background = m === 'rule' ? '#2b6cb0' : '#2d3748';
  document.getElementById('btn-ml').style.background   = m === 'ml'   ? '#2b6cb0' : '#2d3748';
  document.getElementById('opt-desc').textContent = OPT_DESCS[m];
}
setOptMode('rule');

// ── Transport controls ────────────────────────────────────────────────────
const tmsg = () => document.getElementById('transport-msg');
async function injectBackbone(link) {
  tmsg().textContent = `Injecting on backbone ${link}...`;
  const r = await fetch(`/api/phase2/inject_backbone/${link}`).then(r=>r.json());
  tmsg().textContent = r.success ? `✓ ${r.message}` : `✗ ${r.message}`;
  setTimeout(loadTransport, 3000); setTimeout(loadTransport, 8000);
}
async function clearBackbone(link) {
  tmsg().textContent = `Clearing ${link}...`;
  const r = await fetch(`/api/phase2/clear_backbone/${link}`).then(r=>r.json());
  tmsg().textContent = r.success ? `✓ Cleared ${link}. Recovery ~15s.` : `✗ Failed`;
  setTimeout(loadTransport, 3000);
}
async function resetNetwork() {
  tmsg().textContent = 'Resetting...';
  const r = await fetch('/api/phase2/reset').then(r=>r.json());
  tmsg().textContent = r.success ? '✓ ' + r.message : '✗ ' + r.error;
  setTimeout(loadTransport, 3000);
}

// ── LLM ──────────────────────────────────────────────────────────────────
async function explainLatest() {
  document.getElementById('explain-box').textContent = 'Asking Claude...';
  const r = await fetch('/api/ran/explain', {method:'POST'}).then(r=>r.json());
  document.getElementById('explain-box').textContent = r.explanation;
}
async function askWhatIf() {
  const q = document.getElementById('wi-input').value.trim();
  if (!q) return;
  document.getElementById('wi-box').textContent = 'Thinking...';
  const r = await fetch('/api/ran/whatif', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({question: q})}).then(r=>r.json());
  document.getElementById('wi-box').textContent = r.answer;
}

// ── Data loading ──────────────────────────────────────────────────────────
const sc = v => v > 10 ? 'ok' : v > 0 ? 'mid' : 'bad';
const ICONS = {HANDOFF:'↗',POWER_BOOST:'⬆',POWER_REDUCE:'⬇',LOAD_BALANCE:'⇄',NO_ACTION:'—'};

async function loadTransport() {
  try {
    const [stats, topo, p2] = await Promise.all([
      fetch('/api/stats/links').then(r=>r.json()),
      fetch('/api/topology').then(r=>r.json()),
      fetch('/api/phase2/status').then(r=>r.json()),
    ]);

    // KPIs
    const validLat = stats.filter(s=>s.avg_latency);
    const avgLat = validLat.length ? validLat.reduce((s,x)=>s+x.avg_latency,0)/validLat.length : 0;
    const avgLoss = stats.length ? stats.reduce((s,x)=>s+(x.avg_packet_loss||0),0)/stats.length : 0;
    document.getElementById('transport-kpis').innerHTML = [
      ['Nodes', topo.nodes.length, 'ok'],
      ['Links', topo.links.length, 'ok'],
      ['Avg Latency', avgLat.toFixed(1)+'ms', avgLat < 1500 ? 'ok' : 'bad'],
      ['Packet Loss', avgLoss.toFixed(2)+'%', avgLoss < 5 ? 'ok' : 'bad'],
    ].map(([l,v,c])=>`<div class="kpi"><label>${l}</label><div class="val ${c}">${v}</div></div>`).join('');

    // Topology
    document.getElementById('topology-info').innerHTML =
      topo.nodes.map(n=>`${n.node_name} (${n.node_type}${n.ip_address?' — '+n.ip_address:''})`).join('<br>');

    // Load overview chart
    fetch('/api/chart/overview').then(r=>r.json()).then(d=>{
      if (d.data) Plotly.react('overview-chart', d.data, d.layout, {responsive:true,displayModeBar:false});
    });

    // P2 stats
    document.getElementById('p2-loops').textContent    = p2.loop_count || 0;
    document.getElementById('p2-cong').textContent     = (p2.active_congestion||[]).length;
    document.getElementById('p2-reroutes').textContent = p2.stats?.reroutes_applied || 0;
    document.getElementById('p2-verified').textContent = p2.stats?.reroutes_verified || 0;

    const badge = document.getElementById('health-badge');
    const ev = p2.active_congestion || [];
    badge.textContent = ev.length ? `${ev.length} Congested` : 'All Healthy';
    badge.style.background = ev.length ? '#c53030' : '#1c4532';
    badge.style.color = ev.length ? '#fff' : '#68d391';

    // Congestion table
    const ct = document.getElementById('congestion-table');
    ct.innerHTML = ev.length === 0
      ? '<p style="color:#68d391;font-size:12px">✓ All links healthy</p>'
      : `<table><thead><tr><th>Link</th><th>Latency</th><th>Loss</th><th>Severity</th><th>Reason</th></tr></thead>
         <tbody>${ev.map(e=>`<tr class="arow">
           <td><strong>${e.src}→${e.dst}</strong></td>
           <td>${e.avg_latency_ms?.toFixed(1)}ms</td>
           <td>${e.avg_loss_pct?.toFixed(1)}%</td>
           <td>${e.severity?.toUpperCase()}</td>
           <td style="font-size:11px">${(e.reasons||[]).join('; ')}</td>
         </tr>`).join('')}</tbody></table>`;

    // Reroutes
    const rr = document.getElementById('reroutes-section');
    const reroutes = Object.entries(p2.active_reroutes || {});
    rr.innerHTML = reroutes.length === 0
      ? '<p style="color:#718096;font-size:12px">No active reroutes</p>'
      : reroutes.map(([lid, d]) => {
          const path = d.route?.optimal_path || [];
          return `<div class="route-pill">${lid}: ${path.join(' → ')} | ${d.route?.total_cost_ms?.toFixed(1)}ms</div>`;
        }).join('');

    // Verification
    const ver = p2.verification_results || [];
    document.getElementById('verify-section').innerHTML = ver.length === 0
      ? '<p style="color:#718096;font-size:12px">Runs 15s after each reroute</p>'
      : ver.map(v=>`<span style="color:${v.improved?'#68d391':'#fc8181'}">
          ${v.improved?'✓':'✗'} ${v.src}→${v.dst}: ${v.before_ms?.toFixed(0)}ms → ${v.after_ms?.toFixed(0)}ms
          ${v.improvement_pct?'('+v.improvement_pct.toFixed(1)+'% better)':''}
        </span><br>`).join('');

    // Events
    const events = await fetch('/api/phase2/events').then(r=>r.json());
    document.getElementById('events-section').innerHTML = events.length === 0
      ? '<p style="color:#718096;font-size:12px">No events yet</p>'
      : `<table><thead><tr><th>Time</th><th>Type</th><th>Severity</th><th>Node</th><th>Description</th></tr></thead>
         <tbody>${events.slice(0,8).map(e=>`<tr>
           <td>${e.timestamp?.slice(11,19)||'-'}</td>
           <td>${e.event_type}</td><td>${e.severity}</td>
           <td>${e.node_name||'-'}</td>
           <td style="font-size:11px;max-width:300px">${e.description||'-'}</td>
         </tr>`).join('')}</tbody></table>`;

  } catch(e) { console.error('transport load error', e); }
}

async function loadRAN() {
  try {
    const [status, recs, anoms, bss, acts] = await Promise.all([
      fetch('/api/ran/status').then(r=>r.json()),
      fetch('/api/ran/records').then(r=>r.json()),
      fetch('/api/ran/anomalies').then(r=>r.json()),
      fetch('/api/ran/bs_summary').then(r=>r.json()),
      fetch('/api/ran/actions').then(r=>r.json()),
    ]);

    // KPIs
    document.getElementById('ran-kpis').innerHTML = [
      ['Active UEs',    status.num_ues, 'ok'],
      ['Avg SINR',      (status.avg_sinr_db||0)+' dB', sc(status.avg_sinr_db||0)],
      ['Avg RSRP',      (status.avg_rsrp_dbm||0)+' dBm', (status.avg_rsrp_dbm||0)>-90?'ok':'bad'],
      ['Poor SINR UEs', status.ues_below_0db_sinr, (status.ues_below_0db_sinr||0)>5?'bad':'ok'],
      ['Avg Throughput',(status.avg_throughput_mbps||0)+' Mbps','mid'],
      ['Avg Latency',   (status.avg_latency_ms||0)+' ms', (status.avg_latency_ms||0)<20?'ok':'mid'],
      ['Active Issues', status.active_issues||0, (status.active_issues||0)>10?'bad':'ok'],
      ['Anomalies',     status.total_anomalies||0, (status.total_anomalies||0)>10?'bad':'ok'],
    ].map(([l,v,c])=>`<div class="kpi"><label>${l}</label><div class="val ${c}">${v}</div></div>`).join('');

    // SINR histogram
    const bins = [0,0,0,0,0,0];
    recs.forEach(r => {
      const s = r.sinr_db;
      if(s<-20)bins[0]++;else if(s<-10)bins[1]++;else if(s<0)bins[2]++;
      else if(s<10)bins[3]++;else if(s<20)bins[4]++;else bins[5]++;
    });
    sinrChart.data.datasets[0].data = bins;
    sinrChart.update('none');

    // BS table
    document.getElementById('bs-table').innerHTML = bss.map(b=>
      `<tr><td>${b.bs_id}</td><td>${b.cell_type}</td><td>${b.num_ues}</td>
       <td class="${sc(b.avg_sinr)}">${b.avg_sinr}</td><td>${b.avg_tput}</td></tr>`).join('');

    // Anomaly table
    document.getElementById('anomaly-table').innerHTML = anoms.length === 0
      ? `<tr><td colspan="5" style="color:#718096;text-align:center;padding:10px">No anomalies</td></tr>`
      : anoms.slice().reverse().map(a=>
          `<tr class="arow"><td>${a.ue_id}</td><td>${a.bs_id}</td>
           <td>${a.sinr_db.toFixed(1)}</td><td>${a.throughput_mbps.toFixed(1)}</td>
           <td style="font-size:11px">${a.reason}</td></tr>`).join('');

    // Action table
    document.getElementById('action-table').innerHTML = acts.length === 0
      ? `<tr><td colspan="5" style="color:#718096;text-align:center;padding:10px">No actions — network healthy or warming up</td></tr>`
      : acts.slice().reverse().map(a=>
          `<tr><td><span class="tag tag-${a.action_type}">${ICONS[a.action_type]||''} ${a.action_type}</span></td>
           <td>${a.target_ue||'—'}</td><td>${a.target_bs||'—'}</td><td>${a.new_bs||'—'}</td>
           <td style="font-size:11px;color:#a0aec0">${a.reason.substring(0,80)}</td></tr>`).join('');

  } catch(e) { console.error('RAN load error', e); }
}

async function loadBridge() {
  try {
    const [events, stats, topo] = await Promise.all([
      fetch('/api/bridge/events').then(r=>r.json()),
      fetch('/api/bridge/stats').then(r=>r.json()),
      fetch('/api/bridge/topology').then(r=>r.json()),
    ]);

    // KPIs
    document.getElementById('bridge-kpis').innerHTML = [
      ['Handoffs Processed',    stats.handoffs_processed||0,              'ok'],
      ['Transport Reroutes',    stats.transport_reroutes_triggered||0,    'mid'],
      ['RAN Load Reductions',   stats.ran_load_reductions||0,             'mid'],
    ].map(([l,v,c])=>`<div class="kpi"><label>${l}</label><div class="val ${c}">${v}</div></div>`).join('');

    // Mapping table
    const bsToHost = topo.bs_to_host || {};
    const bsToSw   = topo.bs_to_switch || {};
    document.getElementById('mapping-table').innerHTML =
      Object.entries(bsToHost).map(([bs, host]) =>
        `<tr><td>${bs}</td>
         <td>${bs.includes('MAC')?'Macro':'Micro'}</td>
         <td>${host}</td>
         <td>${bsToSw[bs]||'-'}</td></tr>`).join('');

    // Events
    const evDiv = document.getElementById('bridge-events');
    if (events.length === 0) {
      evDiv.innerHTML = '<p style="color:#718096;font-size:12px">No cross-layer events yet. Try injecting backbone congestion or waiting for UE handoffs.</p>';
    } else {
      evDiv.innerHTML = events.slice().reverse().slice(0,15).map(e => {
        const isHandoff = e.type === 'ran_handoff';
        const cls = isHandoff ? 'cl-handoff' : 'cl-transport';
        const icon = isHandoff ? '↗' : '⚠';
        const desc = isHandoff
          ? `${icon} RAN Handoff: ${e.ue_id} | ${e.from_bs}(${e.from_switch}) → ${e.to_bs}(${e.to_switch})`
          : `${icon} Transport → RAN: link ${e.transport_link} congested → ${e.affected_bs} load reduce suggested`;
        const action = e.transport_action || e.action || '';
        return `<div class="cl-event ${cls}">
          <strong style="font-size:12px">${desc}</strong>
          ${action ? `<br><span style="font-size:11px;color:#a0aec0">Action: ${action}</span>` : ''}
        </div>`;
      }).join('');
    }
  } catch(e) { console.error('bridge load error', e); }
}

// ── Auto-refresh ──────────────────────────────────────────────────────────
function refreshAll() {
  loadTransport();
  loadRAN();
  loadBridge();
}
setInterval(refreshAll, 3000);
refreshAll();
</script>
</body></html>
"""


if __name__ == '__main__':
    print("\n" + "="*60)
    print("6G Digital Twin Network — Unified Dashboard")
    print("="*60)
    print("Transport Twin: Mininet + OpenFlow closed-loop control")
    print("RAN Twin:       6G simulator + Q-Learning + LLM")
    print("Cross-Layer:    Handoff → reroute coupling")
    print("="*60)
    print("Dashboard: http://localhost:5000")
    print("="*60 + "\n")
    app.run(host='0.0.0.0', port=5000, debug=False)
'''


"""
unified_dashboard.py
Single Flask app serving BOTH twins + cross-layer bridge.
Run: sudo python3 unified_dashboard.py
Open: http://localhost:5000

Architecture:
  - RAN Twin (simulator + anomaly detection + Q-learning optimizer + LLM)
  - Transport Twin (Mininet + collector + Dijkstra + OpenFlow)
  - Cross-Layer Bridge (handoff → reroute coupling)
  - Unified dashboard with 3 tabs
"""
"""
unified_dashboard.py
Single Flask app serving BOTH twins + cross-layer bridge.
Run: sudo python3 unified_dashboard.py
Open: http://localhost:5000

Architecture:
  - RAN Twin (simulator + anomaly detection + Q-learning optimizer + LLM)
  - Transport Twin (Mininet + collector + Dijkstra + OpenFlow)
  - Cross-Layer Bridge (handoff → reroute coupling)
  - Unified dashboard with 3 tabs
"""

from flask import Flask, jsonify, request, render_template_string
import threading, time, os, sys, subprocess, logging

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# ── Initialize components ──────────────────────────────────────────────────
from data_layer.storage import NetworkDatabase
from twin_core.control_loop import ClosedLoopController
from ran_twin import RANTwin
from cross_layer_bridge import CrossLayerBridge

# Transport twin
db = NetworkDatabase('dtn_network.db')
transport_ctrl = ClosedLoopController(
    db, loop_interval=5.0,
    latency_threshold=50.0,   # was 1500 — now triggers on real Mininet congestion
    loss_threshold=5.0
)
transport_ctrl.start()

# RAN twin
ran = RANTwin(num_ues=30, seed=42, optimizer_mode='rule')
ran.start()

# Cross-layer bridge
bridge = CrossLayerBridge(ran, db, transport_ctrl)
bridge.start()

app = Flask(__name__)


# ── Transport Twin API (Phase 1+2) ─────────────────────────────────────────

@app.route('/api/topology')
def get_topology():
    return jsonify({'nodes': db.get_topology_nodes(),
                    'links': db.get_topology_links()})

@app.route('/api/stats/links')
def get_link_stats():
    return jsonify(db.get_link_statistics())

@app.route('/api/chart/overview')
def get_overview_chart():
    # Pure JSON - rendered by Chart.js on client (no plotly dependency)
    try:
        cursor = db.conn.cursor()
        cursor.execute("""
            SELECT node_src, node_dst,
                   MIN(latency_ms)  AS min_latency,
                   AVG(latency_ms)  AS avg_latency,
                   MAX(latency_ms)  AS max_latency,
                   AVG(packet_loss_pct) AS avg_packet_loss,
                   COUNT(*)         AS sample_count
            FROM network_metrics
            WHERE latency_ms IS NOT NULL
              AND node_src LIKE 'h%'
              AND node_dst LIKE 'h%'
            GROUP BY node_src, node_dst
            HAVING COUNT(*) >= 1
            ORDER BY AVG(latency_ms) DESC
            LIMIT 12
        """)
        rows = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        return jsonify({'labels':[], 'min':[], 'avg':[], 'max':[], 'loss':[], 'error': str(e)})

    if not rows:
        return jsonify({'labels':[], 'min':[], 'avg':[], 'max':[], 'loss':[]})

    return jsonify({
        'labels': [r['node_src'] + '->' + r['node_dst'] for r in rows],
        'min':    [round(r['min_latency']  or 0, 1) for r in rows],
        'avg':    [round(r['avg_latency']  or 0, 1) for r in rows],
        'max':    [round(r['max_latency']  or 0, 1) for r in rows],
        'loss':   [round(r['avg_packet_loss'] or 0, 2) for r in rows],
        'count':  [r['sample_count'] for r in rows],
    })

@app.route('/api/phase2/status')
def get_phase2_status():
    try:
        status = transport_ctrl.get_status()
        transport_ctrl.optimizer.build_graph()
        graph = transport_ctrl.optimizer.graph
        status['graph'] = {
            'nodes': list(graph.nodes),
            'edges': [
                {'src': s, 'dst': d, 'weight': round(w, 1), 'link_id': l}
                for s, nbrs in graph.edges.items()
                for d, w, l in nbrs
            ]
        }
        status['verification_results'] = transport_ctrl.verification_results[-5:]
        return jsonify(status)
    except Exception as e:
        return jsonify({'error': str(e), 'active_congestion': [],
                       'active_reroutes': {}, 'stats': {}})

@app.route('/api/phase2/inject/<host>')
def inject_host(host):
    pid = _find_pid(host)
    if not pid:
        return jsonify({'success': False, 'error': f'Host {host} not found'})
    iface = f"{host}-eth0"
    subprocess.run(
        f"sudo nsenter -t {pid} -n -- tc qdisc del dev {iface} root 2>/dev/null; true",
        shell=True)
    r = subprocess.run(
        f"sudo nsenter -t {pid} -n -- tc qdisc add dev {iface} root netem delay 500ms loss 20%",
        shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        r2 = subprocess.run(
            f"nsenter -t {pid} -n -- tc qdisc add dev {iface} root netem delay 500ms loss 20%",
            shell=True, capture_output=True, text=True)
        success = r2.returncode == 0
    else:
        success = True
    db.insert_event('congestion', 'warning', host, f'Demo: +500ms, 20% loss on {host}')
    return jsonify({'success': success, 'host': host,
                   'message': f'Congestion on {host}. Detection ~10s.'})

@app.route('/api/phase2/clear/<host>')
def clear_host(host):
    iface = f"{host}-eth0"
    pids = _find_all_pids(host)
    for pid in pids:
        for prefix in ['sudo ', '']:
            subprocess.run(
                f"{prefix}nsenter -t {pid} -n -- "
                f"tc qdisc del dev {iface} root 2>/dev/null || true",
                shell=True)
    db.insert_event('recovery', 'info', host, f'Congestion cleared on {host}')
    return jsonify({'success': True, 'host': host,
                   'message': f'{host} cleared. Recovery ~15s.'})

@app.route('/api/phase2/inject_backbone/<link>')
def inject_backbone(link):
    success = transport_ctrl.inject_backbone_congestion(link, delay_ms=200, loss_pct=15.0)
    return jsonify({'success': success, 'link': link,
                   'message': f'Backbone {link} congested. ~10s detection.' if success
                              else 'Failed'})

@app.route('/api/phase2/clear_backbone/<link>')
def clear_backbone(link):
    success = transport_ctrl.clear_backbone_congestion(link)
    return jsonify({'success': success, 'link': link})

@app.route('/api/phase2/reset')
def reset_network():
    for host in ['h1','h2','h3','h4','h5','h6']:
        iface = f"{host}-eth0"
        for pid in _find_all_pids(host):
            for prefix in ['sudo ', '']:
                subprocess.run(
                    f"{prefix}nsenter -t {pid} -n -- "
                    f"tc qdisc del dev {iface} root 2>/dev/null || true",
                    shell=True)
    transport_ctrl.active_congestion = {}
    transport_ctrl.optimizer.active_reroutes = {}
    for sw in ['s1','s2','s3']:
        subprocess.run(f"sudo ovs-ofctl del-flows {sw}", shell=True, capture_output=True)
        subprocess.run(f"sudo ovs-ofctl add-flow {sw} action=flood",
                      shell=True, capture_output=True)
    db.insert_event('recovery', 'info', None, 'Full reset: all congestion cleared')
    return jsonify({'success': True, 'message': 'Network reset complete'})

@app.route('/api/phase2/events')
def get_events():
    try:
        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM network_events ORDER BY timestamp DESC LIMIT 30")
        return jsonify([dict(r) for r in cursor.fetchall()])
    except Exception as e:
        return jsonify({'error': str(e)})


# ── RAN Twin API ───────────────────────────────────────────────────────────

@app.route('/api/ran/status')
def ran_status():
    return jsonify(ran.get_status())

@app.route('/api/ran/records')
def ran_records():
    return jsonify(ran.get_records())

@app.route('/api/ran/anomalies')
def ran_anomalies():
    return jsonify(ran.get_anomalies(15))

@app.route('/api/ran/bs_summary')
def ran_bs():
    return jsonify(ran.get_bs_summary())

@app.route('/api/ran/actions')
def ran_actions():
    return jsonify(ran.get_actions(15))

@app.route('/api/ran/optimizer', methods=['GET', 'POST'])
def ran_optimizer():
    if request.method == 'POST':
        ran.set_optimizer_mode(request.json.get('mode', 'rule'))
    return jsonify(ran.get_optimizer_info())

@app.route('/api/ran/explain', methods=['POST'])
def ran_explain():
    return jsonify(ran.explain_latest())

@app.route('/api/ran/whatif', methods=['POST'])
def ran_whatif():
    q = request.json.get('question', '')
    return jsonify({'answer': ran.what_if(q) if q else 'Enter a question.'})

@app.route('/api/ran/fault', methods=['POST'])
def ran_fault():
    d = request.json
    if d.get('action', 'fail') == 'fail':
        ran.fail_bs(d['bs_id'])
        return jsonify({'status': f"{d['bs_id']} failed"})
    ran.restore_bs(d['bs_id'])
    return jsonify({'status': f"{d['bs_id']} restored"})


# ── Cross-Layer API ────────────────────────────────────────────────────────

@app.route('/api/bridge/events')
def bridge_events():
    return jsonify(bridge.get_events(20))

@app.route('/api/bridge/stats')
def bridge_stats():
    return jsonify(bridge.get_stats())

@app.route('/api/bridge/topology')
def bridge_topology():
    return jsonify(bridge.get_topology_mapping())


# ── Helpers ────────────────────────────────────────────────────────────────

def _find_pid(host_name):
    pids = _find_all_pids(host_name)
    return pids[0] if pids else None

def _find_all_pids(host_name):
    found = []
    try:
        for pid_dir in os.listdir('/proc'):
            if not pid_dir.isdigit():
                continue
            try:
                with open(f'/proc/{pid_dir}/cmdline', 'rb') as f:
                    cmdline = f.read().decode('utf-8', errors='ignore')
                if f'mininet:{host_name}' in cmdline:
                    found.append(int(pid_dir))
            except Exception:
                continue
    except Exception:
        pass
    return found


# ── Main Dashboard HTML ────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(UNIFIED_HTML)


UNIFIED_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>6G Digital Twin Network — Unified Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0f1117;color:#e2e8f0}
header{padding:14px 22px;border-bottom:1px solid #2d3748;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
header h1{font-size:18px;font-weight:700;color:#e2e8f0}
.live{width:9px;height:9px;border-radius:50%;background:#48bb78;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.badge{font-size:11px;padding:3px 10px;border-radius:12px;background:#1a365d;color:#90cdf4}
.gbadge{font-size:11px;padding:3px 10px;border-radius:12px;background:#1c4532;color:#68d391}
.xbadge{font-size:11px;padding:3px 10px;border-radius:12px;background:#44337a;color:#d6bcfa}

/* Tabs */
.tabs{display:flex;gap:4px;padding:10px 22px;border-bottom:1px solid #2d3748;background:#141720}
.tab{padding:8px 20px;border-radius:8px 8px 0 0;cursor:pointer;font-size:13px;font-weight:600;
     border:1px solid transparent;color:#718096;transition:all 0.2s}
.tab.active{background:#1a202c;border-color:#2d3748;border-bottom-color:#1a202c;color:#e2e8f0}
.tab:hover:not(.active){color:#a0aec0}
.tab-content{display:none;padding:18px 22px}
.tab-content.active{display:block}

/* Cards */
.card{background:#1a202c;border:1px solid #2d3748;border-radius:10px;padding:16px;margin-bottom:16px}
.card h2,.sect h2{font-size:11px;font-weight:600;color:#a0aec0;text-transform:uppercase;
                   letter-spacing:.05em;margin-bottom:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:700px){.grid2{grid-template-columns:1fr}}
.grid4{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px}
.kpi{background:#1a202c;border:1px solid #2d3748;border-radius:10px;padding:13px}
.kpi label{font-size:10px;color:#718096;text-transform:uppercase;letter-spacing:.05em;display:block;margin-bottom:4px}
.kpi .val{font-size:22px;font-weight:700}
.ok{color:#68d391}.mid{color:#f6ad55}.bad{color:#fc8181}

/* Tables */
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;color:#718096;font-weight:500;padding:5px 8px;border-bottom:1px solid #2d3748}
td{padding:5px 8px;border-bottom:1px solid #1e2533}
.arow td{color:#fc8181}
canvas{max-height:200px}

/* Buttons */
.btn{background:#2b6cb0;color:#fff;border:none;padding:7px 14px;border-radius:6px;
     cursor:pointer;font-size:12px;font-weight:600;transition:all 0.2s}
.btn:hover{background:#2c5282;transform:translateY(-1px)}
.btn-red{background:#c53030}.btn-red:hover{background:#9b2c2c}
.btn-green{background:#276749}.btn-green:hover{background:#1c4532}
.btn-purple{background:#6b46c1}.btn-purple:hover{background:#553c9a}
.demo-box{background:#141720;border:1px solid #f6e05e44;border-radius:8px;padding:14px;margin-bottom:14px}
.demo-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;align-items:center}
.demo-row span{font-size:12px;color:#718096}
input[type=text]{background:#2d3748;border:1px solid #4a5568;color:#e2e8f0;
  padding:7px 11px;border-radius:6px;font-size:12px;width:100%;margin-top:8px}
.lbox{background:#0f1117;border-radius:8px;padding:10px 12px;font-size:13px;
      line-height:1.6;color:#cbd5e0;min-height:50px;margin-top:10px}
.status-msg{font-size:12px;color:#718096;margin-top:8px;min-height:18px}

/* Route pills */
.route-pill{display:inline-block;background:#1a365d44;border:1px solid #2b6cb066;
  border-radius:6px;padding:5px 10px;font-family:monospace;font-size:12px;
  color:#90cdf4;margin:3px 0;display:block}

/* Cross-layer */
.cl-event{background:#2d1b4e;border:1px solid #44337a;border-radius:6px;
  padding:8px 12px;margin:4px 0;font-size:12px}
.cl-handoff{border-color:#2b6cb0;background:#1a2e4a}
.cl-transport{border-color:#c53030;background:#2d1515}
.tag{display:inline-block;font-size:10px;padding:2px 6px;border-radius:4px;font-weight:600;margin-right:4px}
.tag-HANDOFF{background:#2a4365;color:#90cdf4}
.tag-POWER_BOOST{background:#2d3a0e;color:#c6f135}
.tag-POWER_REDUCE{background:#3d2a00;color:#f6ad55}
.tag-LOAD_BALANCE{background:#3d1f5c;color:#d6bcfa}
.fb{font-size:11px;padding:4px 10px;border-radius:4px;cursor:pointer;
  border:1px solid #4a5568;background:#2d3748;color:#e2e8f0;margin:3px}
.fb.failed{background:#9b2c2c;border-color:#fc8181}
</style></head>
<body>
<header>
  <div class="live"></div>
  <h1>6G Digital Twin Network</h1>
  <span class="badge">Transport Twin</span>
  <span class="badge">RAN Twin</span>
  <span class="xbadge">Cross-Layer Bridge</span>
  <span class="gbadge">Live</span>
</header>

<div class="tabs">
  <div class="tab active" onclick="showTab('transport', this)">🔀 Transport Twin (Phase 1+2)</div>
  <div class="tab" onclick="showTab('ran', this)">📡 RAN Twin (6G Radio)</div>
  <div class="tab" onclick="showTab('bridge', this)">🌉 Cross-Layer Bridge</div>
</div>

<!-- ═══ TAB 1: TRANSPORT TWIN ═══════════════════════════════════════════ -->
<div id="tab-transport" class="tab-content active">

  <div class="grid4" id="transport-kpis"></div>

  <div class="grid2">
    <div class="card">
      <h2>Link Latency (ms)</h2>
      <canvas id="overview-chart" style="max-height:210px"></canvas>
      <p id="chart-no-data" style="display:none;color:#718096;font-size:12px;text-align:center;padding:16px">Waiting for collector metrics...</p>
    </div>
    <div class="card">
      <h2>Network Topology</h2>
      <div id="topology-info" style="font-size:12px;color:#718096">Loading...</div>
    </div>
  </div>

  <div class="card">
    <h2>Live Latency Time-Series (rolling 40 samples)</h2>
    <canvas id="live-latency-chart" style="max-height:170px"></canvas>
    <p style="font-size:11px;color:#718096;margin-top:4px">
      Top 6 host-to-host links &mdash; spikes appear when backbone congestion is injected
    </p>
  </div>

  <div class="card">
    <h2>Phase 2: Closed-Loop Control &nbsp;<span id="health-badge" style="font-size:11px;padding:2px 8px;border-radius:10px;background:#1c4532;color:#68d391">All Healthy</span></h2>
    <div class="grid4" style="margin-bottom:14px">
      <div class="kpi"><label>Control Loops</label><div class="val ok" id="p2-loops">0</div></div>
      <div class="kpi"><label>Congested</label><div class="val bad" id="p2-cong">0</div></div>
      <div class="kpi"><label>Reroutes</label><div class="val mid" id="p2-reroutes">0</div></div>
      <div class="kpi"><label>Verified</label><div class="val ok" id="p2-verified">0</div></div>
    </div>

    <div class="demo-box">
      <strong style="color:#f6e05e;font-size:13px">🎮 Demo Controls</strong>
      <div class="demo-row">
        <span>Backbone inject:</span>
        <button class="btn btn-red" onclick="injectBackbone('s1-s2')">s1↔s2</button>
        <button class="btn btn-red" onclick="injectBackbone('s2-s3')">s2↔s3</button>
        <button class="btn btn-red" onclick="injectBackbone('s1-s3')">s1↔s3</button>
        <span style="margin-left:8px">Clear:</span>
        <button class="btn btn-green" onclick="clearBackbone('s1-s2')">s1↔s2</button>
        <button class="btn btn-green" onclick="clearBackbone('s2-s3')">s2↔s3</button>
        <button class="btn btn-green" onclick="clearBackbone('s1-s3')">s1↔s3</button>
        <button class="btn btn-purple" onclick="resetNetwork()" style="margin-left:12px">🔄 Reset All</button>
      </div>
      <div id="transport-msg" class="status-msg"></div>
    </div>

    <h2 style="margin-bottom:8px">Active Congestion</h2>
    <div id="congestion-table">Loading...</div>

    <h2 style="margin-top:14px;margin-bottom:8px">Active Reroutes (Dijkstra)</h2>
    <div id="reroutes-section">No reroutes</div>

    <h2 style="margin-top:14px;margin-bottom:8px">Verification Results</h2>
    <div id="verify-section" style="font-size:12px;color:#718096">No verifications yet</div>

    <h2 style="margin-top:14px;margin-bottom:8px">Recent Events</h2>
    <div id="events-section">Loading...</div>
  </div>
</div>

<!-- ═══ TAB 2: RAN TWIN ══════════════════════════════════════════════════ -->
<div id="tab-ran" class="tab-content">

  <div class="grid4" id="ran-kpis"></div>

  <div class="grid2">
    <div class="card"><h2>SINR Distribution</h2><canvas id="sinr-chart"></canvas></div>
    <div class="card">
      <h2>Base Station Summary</h2>
      <table><thead><tr><th>BS</th><th>Type</th><th>UEs</th><th>SINR</th><th>Tput</th></tr></thead>
      <tbody id="bs-table"></tbody></table>
    </div>
  </div>

  <div class="card">
    <h2>Recent Anomalies</h2>
    <table><thead><tr><th>UE</th><th>BS</th><th>SINR</th><th>Tput</th><th>Reason</th></tr></thead>
    <tbody id="anomaly-table"></tbody></table>
  </div>

  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
      <h2 style="margin:0">Self-Healing Optimizer</h2>
      <div style="display:flex;gap:6px">
        <button class="btn" id="btn-rule" onclick="setOptMode('rule')">Rule-based</button>
        <button class="btn" style="background:#2d3748" id="btn-ml" onclick="setOptMode('ml')">Q-Learning</button>
      </div>
    </div>
    <div id="opt-desc" style="font-size:12px;color:#718096;margin-bottom:10px"></div>
    <table><thead><tr><th>Action</th><th>UE</th><th>From BS</th><th>To BS</th><th>Reason</th></tr></thead>
    <tbody id="action-table"></tbody></table>
  </div>

  <div class="card">
    <h2>Fault Injection</h2>
    <p style="font-size:11px;color:#718096;margin-bottom:10px">Fail a base station and watch the optimizer self-heal.</p>
    <div id="fault-buttons"></div>
  </div>

  <div class="card">
    <h2>LLM Anomaly Explainer <span class="gbadge">Novel</span></h2>
    <div class="lbox" id="explain-box">Click to explain the latest anomaly.</div>
    <div style="margin-top:8px"><button class="btn" onclick="explainLatest()">Explain latest anomaly</button></div>
  </div>

  <div class="card">
    <h2>What-If Query</h2>
    <input type="text" id="wi-input" placeholder="What happens if BS_MAC_0 fails?">
    <div style="margin-top:8px"><button class="btn" onclick="askWhatIf()">Ask</button></div>
    <div class="lbox" id="wi-box">Ask any question about the live RAN.</div>
  </div>
</div>

<!-- ═══ TAB 3: CROSS-LAYER BRIDGE ═══════════════════════════════════════ -->
<div id="tab-bridge" class="tab-content">

  <div class="grid4" id="bridge-kpis"></div>

  <div class="card">
    <h2>Cross-Layer Architecture</h2>
    <div style="font-size:13px;color:#a0aec0;line-height:1.7">
      <strong style="color:#e2e8f0">Novel contribution:</strong> This bridge couples the RAN twin (radio access)
      with the transport twin (IP switching) into a single end-to-end closed loop.<br><br>
      <strong style="color:#90cdf4">RAN → Transport:</strong> When a UE handoff crosses switch boundaries
      (e.g. BS_MAC_0[s1] → BS_MAC_2[s2]), the bridge triggers a transport layer reroute check
      to optimise the IP path for that UE's data flow.<br><br>
      <strong style="color:#fc8181">Transport → RAN:</strong> When transport congestion is detected
      (e.g. s1↔s2 link overloaded), the bridge hints the RAN optimizer to consider load-balancing
      UEs away from base stations served by the congested switch.
    </div>
  </div>

  <div class="card">
    <h2>BS → Host Mapping</h2>
    <table><thead><tr><th>Base Station</th><th>Type</th><th>Transport Host</th><th>Switch</th></tr></thead>
    <tbody id="mapping-table"></tbody></table>
  </div>

  <div class="card">
    <h2>Cross-Layer Events (Live)</h2>
    <div id="bridge-events">Loading...</div>
  </div>
</div>

<script>
// ── Tab switching ─────────────────────────────────────────────────────────
function showTab(name, el) {
  document.querySelectorAll('.tab-content').forEach(e => e.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(e => e.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (el) el.classList.add('active');
}

// ── Shared helpers ────────────────────────────────────────────────────────
const sc = v => v > 10 ? 'ok' : v > 0 ? 'mid' : 'bad';
const ICONS = {HANDOFF:'↗',POWER_BOOST:'⬆',POWER_REDUCE:'⬇',LOAD_BALANCE:'⇄',NO_ACTION:'—'};

// ── Overview bar chart (latency per link) ─────────────────────────────────
let _overviewChart = null;
function initOrUpdateOverview(d) {
  const canvas = document.getElementById('overview-chart');
  const noData = document.getElementById('chart-no-data');
  if (!d.labels || d.labels.length === 0) {
    if (canvas) canvas.style.display = 'none';
    if (noData) noData.style.display = 'block';
    return;
  }
  if (canvas) canvas.style.display = 'block';
  if (noData) noData.style.display = 'none';
  if (!_overviewChart) {
    _overviewChart = new Chart(canvas.getContext('2d'), {
      type: 'bar',
      data: { labels: d.labels, datasets: [
        { label: 'Min ms', data: d.min, backgroundColor: '#4299e1bb', borderRadius: 3 },
        { label: 'Avg ms', data: d.avg, backgroundColor: '#48bb78bb', borderRadius: 3 },
        { label: 'Max ms', data: d.max, backgroundColor: '#fc8181bb', borderRadius: 3 },
      ]},
      options: {
        responsive: true, animation: false,
        plugins: { legend: { labels: { color:'#a0aec0', font:{size:11} } } },
        scales: {
          x: { ticks: { color:'#718096', font:{size:9}, maxRotation:50 }, grid: { color:'#2d3748' } },
          y: { beginAtZero: false, ticks: { color:'#718096' }, grid: { color:'#2d3748' },
               title: { display:true, text:'ms', color:'#718096' } }
        }
      }
    });
  } else {
    _overviewChart.data.labels = d.labels;
    _overviewChart.data.datasets[0].data = d.min;
    _overviewChart.data.datasets[1].data = d.avg;
    _overviewChart.data.datasets[2].data = d.max;
    _overviewChart.update('none');
  }
}

// ── Live time-series chart ────────────────────────────────────────────────
const LIVE_MAX = 40;
const _liveLabels = [];
const _liveData = {};
const _liveColors = ['#fc8181','#68d391','#f6ad55','#90cdf4','#d6bcfa','#faf089'];
let _liveChart = null;

function initLiveChart() {
  const canvas = document.getElementById('live-latency-chart');
  if (!canvas || _liveChart) return;
  _liveChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { labels: [], datasets: [] },
    options: {
      responsive: true, animation: false,
      plugins: { legend: { labels: { color:'#a0aec0', font:{size:10}, boxWidth:12 } } },
      scales: {
        x: { ticks: { color:'#718096', font:{size:9}, maxTicksLimit:8 }, grid: { color:'#2d3748' } },
        y: { beginAtZero: false, ticks: { color:'#718096' }, grid: { color:'#2d3748' },
             title: { display:true, text:'latency (ms)', color:'#718096' } }
      }
    }
  });
}

function updateLiveChart(congestionEvents) {
  // congestionEvents comes from p2.active_congestion — has avg_latency_ms, src, dst
  initLiveChart();
  if (!_liveChart) return;
  const t = new Date().toLocaleTimeString('en-GB');
  _liveLabels.push(t);
  if (_liveLabels.length > LIVE_MAX) _liveLabels.shift();
  _liveChart.data.labels = [..._liveLabels];

  const items = (congestionEvents || []).slice(0, 6);
  const activeIds = new Set(items.map(e => e.src + '->' + e.dst));

  // Remove stale datasets
  _liveChart.data.datasets = _liveChart.data.datasets.filter(d => activeIds.has(d.label));

  items.forEach((e, i) => {
    const id = e.src + '->' + e.dst;
    if (!_liveData[id]) _liveData[id] = [];
    _liveData[id].push(e.avg_latency_ms || 0);
    if (_liveData[id].length > LIVE_MAX) _liveData[id].shift();

    let ds = _liveChart.data.datasets.find(d => d.label === id);
    if (!ds) {
      ds = {
        label: id,
        data: [],
        borderColor: _liveColors[i % _liveColors.length],
        backgroundColor: 'transparent',
        borderWidth: 1.5, pointRadius: 0, tension: 0.3
      };
      _liveChart.data.datasets.push(ds);
    }
    ds.data = [..._liveData[id]];
  });

  // If no active congestion, still push a tick so chart scrolls
  if (items.length === 0) {
    _liveChart.data.datasets.forEach(ds => {
      ds.data.push(null);
      if (ds.data.length > LIVE_MAX) ds.data.shift();
    });
  }
  _liveChart.update('none');
}

// ── SINR histogram ────────────────────────────────────────────────────────
let _sinrChart = null;
function initSinrChart() {
  const canvas = document.getElementById('sinr-chart');
  if (!canvas || _sinrChart) return;
  _sinrChart = new Chart(canvas.getContext('2d'), {
    type: 'bar',
    data: {
      labels: ['<-20','-20–-10','-10–0','0–10','10–20','>20'],
      datasets: [{
        data: [0,0,0,0,0,0],
        backgroundColor: ['#fc8181','#f6ad55','#faf089','#68d391','#4299e1','#9f7aea'],
        borderRadius: 3
      }]
    },
    options: {
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color:'#718096' }, grid: { color:'#2d3748' } },
        y: { ticks: { color:'#718096' }, grid: { color:'#2d3748' }, beginAtZero: true }
      }
    }
  });
}

// ── Fault buttons ─────────────────────────────────────────────────────────
const BSS = ['BS_MAC_0','BS_MAC_1','BS_MAC_2','BS_MAC_3','BS_MAC_4','BS_MIC_0','BS_MIC_1','BS_MIC_2'];
const failedBS = new Set();
function buildFaultButtons() {
  const fbw = document.getElementById('fault-buttons');
  if (!fbw) return;
  BSS.forEach(id => {
    const b = document.createElement('button');
    b.className = 'fb'; b.textContent = id; b.id = 'fb_' + id;
    b.onclick = () => toggleFault(id, b);
    fbw.appendChild(b);
  });
}
async function toggleFault(id, btn) {
  const act = failedBS.has(id) ? 'restore' : 'fail';
  await fetch('/api/ran/fault', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({bs_id: id, action: act})});
  if (act === 'fail') { failedBS.add(id); btn.classList.add('failed'); }
  else { failedBS.delete(id); btn.classList.remove('failed'); }
}

// ── Transport demo actions ────────────────────────────────────────────────
const tmsg = () => document.getElementById('transport-msg');
async function injectBackbone(link) {
  tmsg().textContent = `Injecting on backbone ${link}...`;
  const r = await fetch('/api/phase2/inject_backbone/' + link).then(r=>r.json());
  tmsg().textContent = r.success ? '✓ ' + r.message : '✗ ' + (r.error||'failed');
}
async function clearBackbone(link) {
  tmsg().textContent = `Clearing ${link}...`;
  const r = await fetch('/api/phase2/clear_backbone/' + link).then(r=>r.json());
  tmsg().textContent = r.success ? '✓ Cleared ' + link : '✗ ' + (r.error||'failed');
  setTimeout(loadTransport, 2000);
}
async function resetNetwork() {
  tmsg().textContent = 'Resetting...';
  const r = await fetch('/api/phase2/reset').then(r=>r.json());
  tmsg().textContent = r.success ? '✓ ' + r.message : '✗ ' + r.error;
  setTimeout(loadTransport, 3000);
}

// ── LLM ──────────────────────────────────────────────────────────────────
async function explainLatest() {
  document.getElementById('explain-box').textContent = 'Asking Claude...';
  const r = await fetch('/api/ran/explain', {method:'POST'}).then(r=>r.json());
  document.getElementById('explain-box').textContent = r.explanation;
}
async function askWhatIf() {
  const q = document.getElementById('wi-input').value.trim();
  if (!q) return;
  document.getElementById('wi-box').textContent = 'Thinking...';
  const r = await fetch('/api/ran/whatif', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({question: q})}).then(r=>r.json());
  document.getElementById('wi-box').textContent = r.answer;
}

// ── Data loading: Transport Twin ──────────────────────────────────────────
async function loadTransport() {
  try {
    const [stats, topo, p2] = await Promise.all([
      fetch('/api/stats/links').then(r=>r.json()),
      fetch('/api/topology').then(r=>r.json()),
      fetch('/api/phase2/status').then(r=>r.json()),
    ]);

    // ── KPIs ──────────────────────────────────────────────────────────────
    const nodes = topo.nodes || [];
    const links = topo.links || [];
    const congList = p2.active_congestion || [];

    // Compute avg latency from congestion events (reliable, always present when congested)
    // Fall back to stats array if available
    let avgLat = 0, avgLoss = 0;
    if (congList.length > 0) {
      avgLat  = congList.reduce((s,e) => s + (e.avg_latency_ms||0), 0) / congList.length;
      avgLoss = congList.reduce((s,e) => s + (e.avg_loss_pct||0),  0) / congList.length;
    } else if (Array.isArray(stats) && stats.length > 0) {
      // link_stats VIEW may use avg_latency or avg_latency_ms — handle both
      const latKey = stats[0].avg_latency !== undefined ? 'avg_latency' : 'avg_latency_ms';
      const lossKey = stats[0].avg_packet_loss !== undefined ? 'avg_packet_loss' : 'avg_loss_pct';
      const valid = stats.filter(s => s[latKey]);
      if (valid.length) avgLat = valid.reduce((s,x) => s + (x[latKey]||0), 0) / valid.length;
      avgLoss = stats.reduce((s,x) => s + (x[lossKey]||0), 0) / stats.length;
    }

    const latClass = avgLat === 0 ? 'ok' : avgLat < 50 ? 'ok' : avgLat < 500 ? 'mid' : 'bad';
    document.getElementById('transport-kpis').innerHTML = [
      ['Nodes',       nodes.length,            'ok'],
      ['Links',       links.length,            'ok'],
      ['Avg Latency', avgLat.toFixed(1)+'ms',  latClass],
      ['Packet Loss', avgLoss.toFixed(2)+'%',  avgLoss < 5 ? 'ok' : 'bad'],
    ].map(([l,v,c])=>`<div class="kpi"><label>${l}</label><div class="val ${c}">${v}</div></div>`).join('');

    // ── Topology info ──────────────────────────────────────────────────────
    document.getElementById('topology-info').innerHTML = nodes.length
      ? nodes.map(n=>`${n.node_name} (${n.node_type}${n.ip_address?' — '+n.ip_address:''})`).join('<br>')
      : '<span style="color:#718096">No topology data. Run topology_builder.py first.</span>';

    // ── Overview chart (bar: min/avg/max per link) ─────────────────────────
    fetch('/api/chart/overview').then(r=>r.json()).then(initOrUpdateOverview)
      .catch(e => console.warn('chart fetch error', e));

    // ── Live time-series (from active_congestion — always has right fields) ─
    updateLiveChart(congList);

    // ── Phase 2 stats ──────────────────────────────────────────────────────
    document.getElementById('p2-loops').textContent    = p2.loop_count || 0;
    document.getElementById('p2-cong').textContent     = congList.length;
    document.getElementById('p2-reroutes').textContent = p2.stats?.reroutes_applied || 0;
    document.getElementById('p2-verified').textContent = p2.stats?.reroutes_verified || 0;

    const badge = document.getElementById('health-badge');
    badge.textContent = congList.length ? `${congList.length} Congested` : 'All Healthy';
    badge.style.background = congList.length ? '#c53030' : '#1c4532';
    badge.style.color = congList.length ? '#fff' : '#68d391';

    // ── Congestion table ──────────────────────────────────────────────────
    const ct = document.getElementById('congestion-table');
    ct.innerHTML = congList.length === 0
      ? '<p style="color:#68d391;font-size:12px">✓ All links healthy</p>'
      : `<div style="max-height:220px;overflow-y:auto">
         <table><thead><tr><th>Link</th><th>Latency</th><th>Loss</th><th>Severity</th><th>Reason</th></tr></thead>
         <tbody>${congList.map(e=>`<tr class="arow">
           <td><strong>${e.src}→${e.dst}</strong></td>
           <td>${(e.avg_latency_ms||0).toFixed(1)}ms</td>
           <td>${(e.avg_loss_pct||0).toFixed(1)}%</td>
           <td>${(e.severity||'').toUpperCase()}</td>
           <td style="font-size:11px">${(e.reasons||[]).join('; ')}</td>
         </tr>`).join('')}</tbody></table></div>`;

    // ── Reroutes ──────────────────────────────────────────────────────────
    const rr = document.getElementById('reroutes-section');
    const reroutes = Object.entries(p2.active_reroutes || {});
    rr.innerHTML = reroutes.length === 0
      ? '<p style="color:#718096;font-size:12px">No active reroutes</p>'
      : reroutes.map(([lid, d]) => {
          const path = d.route?.optimal_path || [];
          return `<div class="route-pill">${lid}: ${path.join(' → ')} | ${(d.route?.total_cost_ms||0).toFixed(1)}ms</div>`;
        }).join('');

    // ── Verification ──────────────────────────────────────────────────────
    const ver = p2.verification_results || [];
    document.getElementById('verify-section').innerHTML = ver.length === 0
      ? '<p style="color:#718096;font-size:12px">Runs 15s after each reroute</p>'
      : ver.map(v=>`<span style="color:${v.improved?'#68d391':'#fc8181'}">
          ${v.improved?'✓':'✗'} ${v.src}→${v.dst}: ${(v.before_ms||0).toFixed(0)}ms → ${(v.after_ms||0).toFixed(0)}ms
          ${v.improvement_pct?'('+v.improvement_pct.toFixed(1)+'% better)':''}
        </span><br>`).join('');

    // ── Events table ──────────────────────────────────────────────────────
    const events = await fetch('/api/phase2/events').then(r=>r.json()).catch(()=>[]);
    document.getElementById('events-section').innerHTML = !events.length
      ? '<p style="color:#718096;font-size:12px">No events yet</p>'
      : `<div style="max-height:200px;overflow-y:auto">
         <table><thead><tr><th>Time</th><th>Type</th><th>Severity</th><th>Node</th><th>Description</th></tr></thead>
         <tbody>${events.slice(0,15).map(e=>`<tr>
           <td>${(e.timestamp||'').slice(11,19)||'-'}</td>
           <td>${e.event_type}</td><td>${e.severity}</td>
           <td>${e.node_name||'-'}</td>
           <td style="font-size:11px;max-width:300px">${e.description||'-'}</td>
         </tr>`).join('')}</tbody></table></div>`;

  } catch(err) { console.error('transport load error:', err); }
}

// ── Data loading: RAN Twin ────────────────────────────────────────────────
async function loadRAN() {
  try {
    initSinrChart();
    const [status, recs, anoms, bss, acts] = await Promise.all([
      fetch('/api/ran/status').then(r=>r.json()),
      fetch('/api/ran/records').then(r=>r.json()),
      fetch('/api/ran/anomalies').then(r=>r.json()),
      fetch('/api/ran/bs_summary').then(r=>r.json()),
      fetch('/api/ran/actions').then(r=>r.json()),
    ]);

    document.getElementById('ran-kpis').innerHTML = [
      ['Active UEs',    status.num_ues,                                             'ok'],
      ['Avg SINR',      (status.avg_sinr_db||0)+' dB',      sc(status.avg_sinr_db||0)],
      ['Avg RSRP',      (status.avg_rsrp_dbm||0)+' dBm',    (status.avg_rsrp_dbm||0)>-90?'ok':'bad'],
      ['Poor SINR UEs', status.ues_below_0db_sinr,           (status.ues_below_0db_sinr||0)>5?'bad':'ok'],
      ['Avg Throughput',(status.avg_throughput_mbps||0)+' Mbps','mid'],
      ['Avg Latency',   (status.avg_latency_ms||0)+' ms',   (status.avg_latency_ms||0)<20?'ok':'mid'],
      ['Active Issues', status.active_issues||0,             (status.active_issues||0)>10?'bad':'ok'],
      ['Anomalies',     status.total_anomalies||0,           (status.total_anomalies||0)>10?'bad':'ok'],
    ].map(([l,v,c])=>`<div class="kpi"><label>${l}</label><div class="val ${c}">${v}</div></div>`).join('');

    // SINR histogram
    if (_sinrChart) {
      const bins = [0,0,0,0,0,0];
      (recs||[]).forEach(r => {
        const s = r.sinr_db;
        if(s<-20)bins[0]++;else if(s<-10)bins[1]++;else if(s<0)bins[2]++;
        else if(s<10)bins[3]++;else if(s<20)bins[4]++;else bins[5]++;
      });
      _sinrChart.data.datasets[0].data = bins;
      _sinrChart.update('none');
    }

    document.getElementById('bs-table').innerHTML = (bss||[]).map(b=>
      `<tr><td>${b.bs_id}</td><td>${b.cell_type}</td><td>${b.num_ues}</td>
       <td class="${sc(b.avg_sinr)}">${b.avg_sinr}</td><td>${b.avg_tput}</td></tr>`).join('');

    document.getElementById('anomaly-table').innerHTML = !(anoms||[]).length
      ? `<tr><td colspan="5" style="color:#718096;text-align:center;padding:10px">No anomalies</td></tr>`
      : anoms.slice().reverse().map(a=>
          `<tr class="arow"><td>${a.ue_id}</td><td>${a.bs_id}</td>
           <td>${a.sinr_db.toFixed(1)}</td><td>${a.throughput_mbps.toFixed(1)}</td>
           <td style="font-size:11px">${a.reason}</td></tr>`).join('');

    document.getElementById('action-table').innerHTML = !(acts||[]).length
      ? `<tr><td colspan="5" style="color:#718096;text-align:center;padding:10px">No actions yet</td></tr>`
      : acts.slice().reverse().map(a=>
          `<tr><td><span class="tag tag-${a.action_type}">${ICONS[a.action_type]||''} ${a.action_type}</span></td>
           <td>${a.target_ue||'—'}</td><td>${a.target_bs||'—'}</td><td>${a.new_bs||'—'}</td>
           <td style="font-size:11px;color:#a0aec0">${(a.reason||'').substring(0,80)}</td></tr>`).join('');

  } catch(err) { console.error('RAN load error:', err); }
}

// ── Data loading: Cross-Layer Bridge ─────────────────────────────────────
async function loadBridge() {
  try {
    const [events, stats, topo] = await Promise.all([
      fetch('/api/bridge/events').then(r=>r.json()),
      fetch('/api/bridge/stats').then(r=>r.json()),
      fetch('/api/bridge/topology').then(r=>r.json()),
    ]);

    document.getElementById('bridge-kpis').innerHTML = [
      ['Handoffs Processed',  stats.handoffs_processed||0,           'ok'],
      ['Transport Reroutes',  stats.transport_reroutes_triggered||0, 'mid'],
      ['RAN Load Reductions', stats.ran_load_reductions||0,          'mid'],
    ].map(([l,v,c])=>`<div class="kpi"><label>${l}</label><div class="val ${c}">${v}</div></div>`).join('');

    const bsToHost = topo.bs_to_host || {};
    const bsToSw   = topo.bs_to_switch || {};
    document.getElementById('mapping-table').innerHTML =
      Object.entries(bsToHost).map(([bs, host]) =>
        `<tr><td>${bs}</td>
         <td>${bs.includes('MAC')?'Macro':'Micro'}</td>
         <td>${host}</td>
         <td>${bsToSw[bs]||'-'}</td></tr>`).join('');

    const evDiv = document.getElementById('bridge-events');
    evDiv.innerHTML = !(events||[]).length
      ? '<p style="color:#718096;font-size:12px">No cross-layer events yet. Waiting for UE handoffs...</p>'
      : events.slice().reverse().slice(0,15).map(e => {
          const isHandoff = e.type === 'ran_handoff';
          const cls = isHandoff ? 'cl-handoff' : 'cl-transport';
          const desc = isHandoff
            ? `↗ RAN Handoff: ${e.ue_id} | ${e.from_bs}(${e.from_switch}) → ${e.to_bs}(${e.to_switch})`
            : `⚠ Transport → RAN: link ${e.transport_link} congested → ${e.affected_bs} load reduce suggested`;
          const action = e.transport_action || e.action || '';
          return `<div class="cl-event ${cls}">
            <strong style="font-size:12px">${desc}</strong>
            ${action ? `<br><span style="font-size:11px;color:#a0aec0">Action: ${action}</span>` : ''}
          </div>`;
        }).join('');

  } catch(err) { console.error('bridge load error:', err); }
}

// ── RAN optimizer ─────────────────────────────────────────────────────────
async function setOptimizer(mode) {
  await fetch('/api/ran/optimizer', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({mode})});
  loadRAN();
}

// ── Auto-refresh ──────────────────────────────────────────────────────────
function refreshAll() {
  loadTransport();
  loadRAN();
  loadBridge();
}

// Init on page load
buildFaultButtons();
setInterval(refreshAll, 3000);
refreshAll();
</script>
</body></html>
"""


if __name__ == '__main__':
    print("\n" + "="*60)
    print("6G Digital Twin Network — Unified Dashboard")
    print("="*60)
    print("Transport Twin: Mininet + OpenFlow closed-loop control")
    print("RAN Twin:       6G simulator + Q-Learning + LLM")
    print("Cross-Layer:    Handoff → reroute coupling")
    print("="*60)
    print("Dashboard: http://localhost:5000")
    print("="*60 + "\n")
    app.run(host='0.0.0.0', port=5000, debug=False)
    '''