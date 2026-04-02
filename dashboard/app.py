"""
Flask Dashboard for Digital Twin Network - Phase 1 + Phase 2
Real-time visualization + Closed-Loop Control
"""

from flask import Flask, render_template, jsonify
import plotly.graph_objs as go
import plotly.utils
import json
from datetime import datetime, timedelta
import sys
import os
import threading
import subprocess

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_layer.storage import NetworkDatabase
from twin_core.control_loop import ClosedLoopController

app = Flask(__name__)
db = NetworkDatabase('dtn_network.db')

# Phase 2: Closed-loop controller embedded in dashboard
controller = ClosedLoopController(
    db,
    loop_interval=5.0,
    latency_threshold=1500.0,
    loss_threshold=10.0
)
controller.start()


# ── Phase 1 Routes ─────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/topology')
def get_topology():
    nodes = db.get_topology_nodes()
    links = db.get_topology_links()
    return jsonify({'nodes': nodes, 'links': links})


@app.route('/api/metrics/recent/<int:limit>')
def get_recent_metrics(limit=100):
    metrics = db.get_recent_metrics(limit)
    return jsonify(metrics)


@app.route('/api/metrics/link/<src>/<dst>')
def get_link_metrics(src, dst):
    metrics = db.get_metrics_by_link(src, dst, hours=1)
    return jsonify(metrics)


@app.route('/api/stats/links')
def get_link_stats():
    stats = db.get_link_statistics()
    return jsonify(stats)


@app.route('/api/chart/latency/<src>/<dst>')
def get_latency_chart(src, dst):
    metrics = db.get_metrics_by_link(src, dst, hours=1)
    if not metrics:
        return jsonify({'error': 'No data found'})
    timestamps = [m['timestamp'] for m in metrics]
    latencies = [m['latency_ms'] if m['latency_ms'] else 0 for m in metrics]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=timestamps, y=latencies, mode='lines+markers',
        name='Latency', line=dict(color='#2E86AB', width=2), marker=dict(size=4)))
    fig.update_layout(title=f'Latency: {src} → {dst}', xaxis_title='Time',
        yaxis_title='Latency (ms)', hovermode='x unified',
        template='plotly_white', height=400)
    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)


@app.route('/api/chart/throughput/<src>/<dst>')
def get_throughput_chart(src, dst):
    metrics = db.get_metrics_by_link(src, dst, hours=1)
    if not metrics:
        return jsonify({'error': 'No data found'})
    timestamps = [m['timestamp'] for m in metrics]
    throughputs = [m['throughput_mbps'] if m['throughput_mbps'] else 0 for m in metrics]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=timestamps, y=throughputs, mode='lines+markers',
        name='Throughput', line=dict(color='#A23B72', width=2),
        marker=dict(size=4), fill='tozeroy'))
    fig.update_layout(title=f'Throughput: {src} → {dst}', xaxis_title='Time',
        yaxis_title='Throughput (Mbps)', hovermode='x unified',
        template='plotly_white', height=400)
    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)


@app.route('/api/chart/packet_loss')
def get_packet_loss_chart():
    stats = db.get_link_statistics()
    if not stats:
        return jsonify({'error': 'No data found'})
    links = [f"{s['node_src']}→{s['node_dst']}" for s in stats]
    packet_loss = [s['avg_packet_loss'] if s['avg_packet_loss'] else 0 for s in stats]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=links, y=packet_loss,
        marker=dict(color=packet_loss, colorscale='Reds', showscale=True),
        text=[f"{pl:.2f}%" for pl in packet_loss], textposition='auto'))
    fig.update_layout(title='Average Packet Loss by Link', xaxis_title='Link',
        yaxis_title='Packet Loss (%)', template='plotly_white', height=400)
    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)


@app.route('/api/chart/overview')
def get_overview_chart():
    stats = db.get_link_statistics()
    if not stats:
        return jsonify({'error': 'No data found'})
    stats = sorted(stats, key=lambda x: x['avg_latency'] if x['avg_latency'] else 0, reverse=True)
    stats = stats[:10]
    links = [f"{s['node_src']}→{s['node_dst']}" for s in stats]
    avg_latency = [s['avg_latency'] if s['avg_latency'] else 0 for s in stats]
    min_latency = [s['min_latency'] if s['min_latency'] else 0 for s in stats]
    max_latency = [s['max_latency'] if s['max_latency'] else 0 for s in stats]
    fig = go.Figure()
    fig.add_trace(go.Bar(name='Min Latency', x=links, y=min_latency, marker_color='lightblue'))
    fig.add_trace(go.Bar(name='Avg Latency', x=links, y=avg_latency, marker_color='steelblue'))
    fig.add_trace(go.Bar(name='Max Latency', x=links, y=max_latency, marker_color='darkblue'))
    fig.update_layout(title='Latency Distribution - Top 10 Links', xaxis_title='Link',
        yaxis_title='Latency (ms)', barmode='group',
        template='plotly_white', height=500)
    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)


@app.route('/api/health')
def health_check():
    try:
        stats = db.get_link_statistics()
        return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat(),
                       'links_monitored': len(stats)})
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500


# ── Phase 2 Routes ─────────────────────────────────────────

@app.route('/api/phase2/status')
def get_phase2_status():
    """Full controller status including congestion + reroutes"""
    try:
        status = controller.get_status()
        # Also build current twin graph for display
        controller.optimizer.build_graph()
        graph = controller.optimizer.graph
        nodes = list(graph.nodes)
        edges = [
            {'src': src, 'dst': dst, 'weight': round(w, 1), 'link_id': lid}
            for src, neighbors in graph.edges.items()
            for dst, w, lid in neighbors
        ]
        status['graph'] = {'nodes': nodes, 'edges': edges}
        return jsonify(status)
    except Exception as e:
        return jsonify({'error': str(e), 'running': False,
                       'active_congestion': [], 'active_reroutes': {},
                       'stats': {}, 'graph': {'nodes': [], 'edges': []}})


@app.route('/api/phase2/inject/<host>')
def inject_congestion(host):
    """Inject demo congestion via sudo nsenter"""
    try:
        pid = _find_mininet_pid(host)
        if not pid:
            return jsonify({'success': False,
                           'error': f'Mininet not running or {host} not found'})
        iface = f"{host}-eth0"
        # Use sudo explicitly so nsenter has permission even under Flask
        subprocess.run(
            f"sudo nsenter -t {pid} -n -- tc qdisc del dev {iface} root 2>/dev/null; true",
            shell=True
        )
        result = subprocess.run(
            f"sudo nsenter -t {pid} -n -- tc qdisc add dev {iface} root netem delay 500ms loss 20%",
            shell=True, capture_output=True, text=True
        )
        if result.returncode != 0:
            # Try without sudo (if already root)
            result2 = subprocess.run(
                f"nsenter -t {pid} -n -- tc qdisc add dev {iface} root netem delay 500ms loss 20%",
                shell=True, capture_output=True, text=True
            )
            if result2.returncode != 0:
                return jsonify({'success': False,
                               'error': f'nsenter failed: {result.stderr.strip()}. '
                                        f'Run dashboard with: sudo python3 dashboard/app.py'})
        db.insert_event('congestion', 'warning', host,
                       f'Demo: +500ms delay, 20% loss on {host}')
        return jsonify({'success': True, 'host': host,
                       'message': f'Congestion injected on {host}. Detection in ~10s.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/phase2/clear/<host>')
def clear_congestion(host):
    """Clear injected congestion from host namespace"""
    try:
        iface = f"{host}-eth0"
        pids = _find_all_mininet_pids(host)
        if not pids:
            return jsonify({'success': False,
                           'error': f'Mininet not running or {host} not found'})

        cleared = False
        errors = []
        for pid in pids:
            for prefix in ['sudo ', '']:
                # Delete ALL qdiscs on the interface
                subprocess.run(
                    f"{prefix}nsenter -t {pid} -n -- "
                    f"tc qdisc del dev {iface} root 2>/dev/null || true",
                    shell=True
                )
                # Verify
                chk = subprocess.run(
                    f"{prefix}nsenter -t {pid} -n -- tc qdisc show dev {iface}",
                    shell=True, capture_output=True, text=True
                )
                if chk.returncode == 0 and 'netem' not in chk.stdout:
                    cleared = True
                    break
            if cleared:
                break

        db.insert_event('recovery', 'info', host,
                       f'Congestion cleared on {host}')
        return jsonify({
            'success': True, 'host': host, 'cleared': cleared,
            'message': f'Congestion cleared on {host}. Recovery visible in ~15s.'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/phase2/events')
def get_phase2_events():
    try:
        cursor = db.conn.cursor()
        cursor.execute(
            "SELECT * FROM network_events ORDER BY timestamp DESC LIMIT 30"
        )
        return jsonify([dict(r) for r in cursor.fetchall()])
    except Exception as e:
        return jsonify({'error': str(e)})




@app.route('/api/phase2/reset')
def reset_network():
    """Reset all injected congestion on all hosts"""
    try:
        hosts = ['h1','h2','h3','h4','h5','h6']
        results = {}
        for host in hosts:
            iface = f"{host}-eth0"
            pids = _find_all_mininet_pids(host)
            if not pids:
                results[host] = 'not_found'
                continue
            for pid in pids:
                for prefix in ['sudo ', '']:
                    subprocess.run(
                        f"{prefix}nsenter -t {pid} -n -- "
                        f"tc qdisc del dev {iface} root 2>/dev/null || true",
                        shell=True
                    )
            results[host] = 'cleared'

        # Clear controller state
        controller.active_congestion = {}
        controller.optimizer.active_reroutes = {}
        controller.stats['recoveries'] += len(hosts)

        # Remove all OpenFlow reroute rules, restore flood
        for sw in ['s1','s2','s3']:
            subprocess.run(f"sudo ovs-ofctl del-flows {sw}", shell=True, capture_output=True)
            subprocess.run(f"ovs-ofctl del-flows {sw}", shell=True, capture_output=True)
            subprocess.run(f"sudo ovs-ofctl add-flow {sw} action=flood",
                          shell=True, capture_output=True)
            subprocess.run(f"ovs-ofctl add-flow {sw} action=flood",
                          shell=True, capture_output=True)

        db.insert_event('recovery', 'info', None,
                       'Full network reset: all congestion cleared, flood rules restored')

        return jsonify({
            'success': True,
            'message': 'All congestion cleared, OpenFlow rules reset to flood baseline',
            'results': results
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

def _find_mininet_pid(host_name: str):
    """Find first PID of Mininet host process"""
    pids = _find_all_mininet_pids(host_name)
    return pids[0] if pids else None


def _find_all_mininet_pids(host_name: str):
    """Find ALL PIDs associated with a Mininet host namespace"""
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


if __name__ == '__main__':
    print("=" * 60)
    print("Digital Twin Network Dashboard - Phase 1 + Phase 2")
    print("=" * 60)
    print(f"Dashboard: http://localhost:5000")
    print(f"API Status: http://localhost:5000/api/health")
    print(f"Phase 2:   http://localhost:5000/api/phase2/status")
    print("=" * 60)
    app.run(debug=False, host='0.0.0.0', port=5000)