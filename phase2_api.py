"""
Phase 2 API routes to add to dashboard/app.py
Add these routes to your existing Flask app.
Also shows how to add the control loop status endpoint.
"""

# ── Add these imports to app.py ────────────────────────────
# import sys, os
# sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# from twin_core.control_loop import ClosedLoopController
# 
# # Initialize controller (add after db = NetworkDatabase(...))
# controller = ClosedLoopController(db, loop_interval=5.0, latency_threshold=50.0)
# controller.start()

# ── Add these routes to app.py ─────────────────────────────

PHASE2_ROUTES = '''

@app.route('/api/phase2/status')
def get_phase2_status():
    """Get closed-loop controller status"""
    try:
        status = controller.get_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({'error': str(e), 'running': False})


@app.route('/api/phase2/congestion')
def get_congestion():
    """Get current congestion events"""
    try:
        events = controller.detector.scan_all_links()
        return jsonify({
            'events': events,
            'count': len(events),
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({'error': str(e), 'events': []})


@app.route('/api/phase2/routes')
def get_optimal_routes():
    """Get all computed optimal routes"""
    try:
        reroutes = controller.optimizer.get_active_reroutes()
        return jsonify({
            'active_reroutes': reroutes,
            'count': len(reroutes)
        })
    except Exception as e:
        return jsonify({'error': str(e), 'active_reroutes': {}})


@app.route('/api/phase2/inject/<host>')
def inject_congestion(host):
    """Inject demo congestion on a host"""
    try:
        success = controller.inject_demo_congestion(
            host, delay_ms=150, loss_pct=30.0
        )
        return jsonify({'success': success, 'host': host})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/phase2/clear/<host>')
def clear_congestion(host):
    """Clear injected congestion from a host"""
    try:
        success = controller.clear_demo_congestion(host)
        return jsonify({'success': success, 'host': host})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/phase2/graph')
def get_twin_graph():
    """Get the digital twin graph with current weights"""
    try:
        controller.optimizer.build_graph()
        graph = controller.optimizer.graph
        nodes = list(graph.nodes)
        edges = []
        for src, neighbors in graph.edges.items():
            for dst, weight, link_id in neighbors:
                edges.append({
                    'src': src, 'dst': dst,
                    'weight': round(weight, 2),
                    'link_id': link_id
                })
        return jsonify({'nodes': nodes, 'edges': edges})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/phase2/events')
def get_network_events():
    """Get recent network events from DB"""
    try:
        cursor = db.conn.cursor()
        cursor.execute("""
            SELECT * FROM network_events
            ORDER BY timestamp DESC LIMIT 50
        """)
        events = [dict(r) for r in cursor.fetchall()]
        return jsonify(events)
    except Exception as e:
        return jsonify({'error': str(e)})
'''

print(PHASE2_ROUTES)
