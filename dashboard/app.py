"""
Flask Dashboard for Digital Twin Network
Real-time visualization of network metrics
"""

from flask import Flask, render_template, jsonify
import threading
import plotly.graph_objs as go
import plotly.utils
import json
from datetime import datetime, timedelta
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_layer.storage import NetworkDatabase
from twin_core.control_loop import ClosedLoopController

app = Flask(__name__)
db = NetworkDatabase('dtn_network.db')
controller = ClosedLoopController(db, loop_interval=5.0,
                                   latency_threshold=1500.0,
                                   loss_threshold=10.0)
controller.start()


@app.route('/')
def index():
    """Render main dashboard page"""
    return render_template('index.html')


@app.route('/api/topology')
def get_topology():
    """Get network topology information"""
    nodes = db.get_topology_nodes()
    links = db.get_topology_links()
    
    return jsonify({
        'nodes': nodes,
        'links': links
    })


@app.route('/api/metrics/recent/<int:limit>')
def get_recent_metrics(limit=100):
    """Get recent network metrics
    
    Args:
        limit: Number of records to return
    """
    metrics = db.get_recent_metrics(limit)
    return jsonify(metrics)


@app.route('/api/metrics/link/<src>/<dst>')
def get_link_metrics(src, dst):
    """Get metrics for a specific link
    
    Args:
        src: Source node name
        dst: Destination node name
    """
    metrics = db.get_metrics_by_link(src, dst, hours=1)
    return jsonify(metrics)


@app.route('/api/stats/links')
def get_link_stats():
    """Get aggregated statistics for all links"""
    stats = db.get_link_statistics()
    return jsonify(stats)


@app.route('/api/chart/latency/<src>/<dst>')
def get_latency_chart(src, dst):
    """Generate latency time series chart
    
    Args:
        src: Source node name
        dst: Destination node name
    """
    metrics = db.get_metrics_by_link(src, dst, hours=1)
    
    if not metrics:
        return jsonify({'error': 'No data found'})
    
    timestamps = [m['timestamp'] for m in metrics]
    latencies = [m['latency_ms'] if m['latency_ms'] else 0 for m in metrics]
    
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=timestamps,
        y=latencies,
        mode='lines+markers',
        name='Latency',
        line=dict(color='#2E86AB', width=2),
        marker=dict(size=4)
    ))
    
    fig.update_layout(
        title=f'Latency: {src} → {dst}',
        xaxis_title='Time',
        yaxis_title='Latency (ms)',
        hovermode='x unified',
        template='plotly_white',
        height=400
    )
    
    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)


@app.route('/api/chart/throughput/<src>/<dst>')
def get_throughput_chart(src, dst):
    """Generate throughput time series chart
    
    Args:
        src: Source node name
        dst: Destination node name
    """
    metrics = db.get_metrics_by_link(src, dst, hours=1)
    
    if not metrics:
        return jsonify({'error': 'No data found'})
    
    timestamps = [m['timestamp'] for m in metrics]
    throughputs = [m['throughput_mbps'] if m['throughput_mbps'] else 0 for m in metrics]
    
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=timestamps,
        y=throughputs,
        mode='lines+markers',
        name='Throughput',
        line=dict(color='#A23B72', width=2),
        marker=dict(size=4),
        fill='tozeroy'
    ))
    
    fig.update_layout(
        title=f'Throughput: {src} → {dst}',
        xaxis_title='Time',
        yaxis_title='Throughput (Mbps)',
        hovermode='x unified',
        template='plotly_white',
        height=400
    )
    
    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)


@app.route('/api/chart/packet_loss')
def get_packet_loss_chart():
    """Generate packet loss comparison chart for all links"""
    stats = db.get_link_statistics()
    
    if not stats:
        return jsonify({'error': 'No data found'})
    
    links = [f"{s['node_src']}→{s['node_dst']}" for s in stats]
    packet_loss = [s['avg_packet_loss'] if s['avg_packet_loss'] else 0 for s in stats]
    
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=links,
        y=packet_loss,
        marker=dict(
            color=packet_loss,
            colorscale='Reds',
            showscale=True
        ),
        text=[f"{pl:.2f}%" for pl in packet_loss],
        textposition='auto'
    ))
    
    fig.update_layout(
        title='Average Packet Loss by Link',
        xaxis_title='Link',
        yaxis_title='Packet Loss (%)',
        template='plotly_white',
        height=400
    )
    
    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)


@app.route('/api/chart/overview')
def get_overview_chart():
    """Generate overview chart with multiple metrics"""
    stats = db.get_link_statistics()
    
    if not stats:
        return jsonify({'error': 'No data found'})
    
    # Sort by average latency
    stats = sorted(stats, key=lambda x: x['avg_latency'] if x['avg_latency'] else 0, reverse=True)
    stats = stats[:10]  # Top 10
    
    links = [f"{s['node_src']}→{s['node_dst']}" for s in stats]
    avg_latency = [s['avg_latency'] if s['avg_latency'] else 0 for s in stats]
    min_latency = [s['min_latency'] if s['min_latency'] else 0 for s in stats]
    max_latency = [s['max_latency'] if s['max_latency'] else 0 for s in stats]
    
    fig = go.Figure()
    
    fig.add_trace(go.Bar(
        name='Min Latency',
        x=links,
        y=min_latency,
        marker_color='lightblue'
    ))
    
    fig.add_trace(go.Bar(
        name='Avg Latency',
        x=links,
        y=avg_latency,
        marker_color='steelblue'
    ))
    
    fig.add_trace(go.Bar(
        name='Max Latency',
        x=links,
        y=max_latency,
        marker_color='darkblue'
    ))
    
    fig.update_layout(
        title='Latency Distribution - Top 10 Links',
        xaxis_title='Link',
        yaxis_title='Latency (ms)',
        barmode='group',
        template='plotly_white',
        height=500
    )
    
    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)


@app.route('/api/health')
def health_check():
    """Health check endpoint"""
    try:
        # Check database connection
        stats = db.get_link_statistics()
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'links_monitored': len(stats)
        })
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 500




@app.route('/api/phase2/status')
def get_phase2_status():
    try:
        return jsonify(controller.get_status())
    except Exception as e:
        return jsonify({'error': str(e), 'running': False})

@app.route('/api/phase2/congestion')
def get_phase2_congestion():
    try:
        events = controller.detector.scan_all_links()
        return jsonify({'events': events, 'count': len(events),
                        'timestamp': datetime.now().isoformat()})
    except Exception as e:
        return jsonify({'error': str(e), 'events': []})

@app.route('/api/phase2/routes')
def get_phase2_routes():
    try:
        return jsonify({'active_reroutes': controller.optimizer.get_active_reroutes()})
    except Exception as e:
        return jsonify({'error': str(e), 'active_reroutes': {}})

@app.route('/api/phase2/inject/<host>')
def inject_congestion(host):
    try:
        success = controller.inject_demo_congestion(host, delay_ms=500, loss_pct=20.0)
        return jsonify({'success': success, 'host': host})
    except Exception as e:
        return jsonify({'error': str(e), 'success': False})

@app.route('/api/phase2/clear/<host>')
def clear_congestion(host):
    try:
        success = controller.clear_demo_congestion(host)
        return jsonify({'success': success, 'host': host})
    except Exception as e:
        return jsonify({'error': str(e), 'success': False})

@app.route('/api/phase2/events')
def get_phase2_events():
    try:
        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM network_events ORDER BY timestamp DESC LIMIT 50")
        return jsonify([dict(r) for r in cursor.fetchall()])
    except Exception as e:
        return jsonify({'error': str(e)})

if __name__ == '__main__':
    print("=" * 60)
    print("Digital Twin Network Dashboard")
    print("=" * 60)
    print(f"Dashboard: http://localhost:5000")
    print(f"API Status: http://localhost:5000/api/health")
    print("=" * 60)
    
    app.run(debug=True, host='0.0.0.0', port=5000)