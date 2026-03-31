"""
Route Optimizer for Digital Twin Network - Phase 2
Dijkstra shortest path on the digital twin graph.
Computes optimal routes based on real-time latency metrics.
This is the 'Model Domain' computation from ITU-T Y.3090.
"""

import heapq
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import sys, os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_layer.storage import NetworkDatabase

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class NetworkGraph:
    """
    In-memory graph representation of the digital twin network.
    Edge weights = current avg latency (from metrics) or configured delay.
    """

    def __init__(self):
        self.nodes = set()
        self.edges: Dict[str, List[Tuple[str, float, str]]] = {}
        # edges[src] = [(dst, weight, link_id), ...]

    def add_node(self, node: str):
        self.nodes.add(node)
        if node not in self.edges:
            self.edges[node] = []

    def add_edge(self, src: str, dst: str, weight: float):
        self.add_node(src)
        self.add_node(dst)
        # Remove existing edge if present
        self.edges[src] = [(d, w, l) for d, w, l in self.edges[src] if d != dst]
        self.edges[src].append((dst, weight, f"{src}-{dst}"))

    def dijkstra(self, src: str, dst: str) -> Optional[Tuple[List[str], float]]:
        """
        Dijkstra's algorithm from src to dst.
        Returns (path, total_cost) or None if no path exists.
        """
        if src not in self.nodes or dst not in self.nodes:
            return None

        dist = {node: float('inf') for node in self.nodes}
        prev = {node: None for node in self.nodes}
        dist[src] = 0
        pq = [(0, src)]

        while pq:
            cost, u = heapq.heappop(pq)
            if cost > dist[u]:
                continue
            if u == dst:
                break
            for v, w, _ in self.edges.get(u, []):
                alt = dist[u] + w
                if alt < dist[v]:
                    dist[v] = alt
                    prev[v] = u
                    heapq.heappush(pq, (alt, v))

        if dist[dst] == float('inf'):
            return None

        # Reconstruct path
        path = []
        cur = dst
        while cur is not None:
            path.append(cur)
            cur = prev[cur]
        path.reverse()
        return path, dist[dst]

    def get_all_paths(self, src: str, dst: str,
                      max_hops: int = 5) -> List[Tuple[List[str], float]]:
        """Find multiple paths using modified DFS (for what-if analysis)."""
        all_paths = []

        def dfs(current, target, path, cost, visited):
            if len(path) > max_hops:
                return
            if current == target:
                all_paths.append((list(path), cost))
                return
            for neighbor, weight, _ in self.edges.get(current, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    path.append(neighbor)
                    dfs(neighbor, target, path, cost + weight, visited)
                    path.pop()
                    visited.remove(neighbor)

        dfs(src, dst, [src], 0, {src})
        return sorted(all_paths, key=lambda x: x[1])


class RouteOptimizer:
    """
    Builds and maintains the digital twin graph.
    Computes optimal routes and rerouting decisions.
    """

    def __init__(self, db: NetworkDatabase):
        self.db = db
        self.graph = NetworkGraph()
        self.active_reroutes: Dict[str, Dict] = {}
        self.route_history: List[Dict] = []

    def build_graph(self, congested_links: List[str] = None):
        """
        Build graph from topology + current latency metrics.
        Congested links get high weight (penalty) so Dijkstra avoids them.
        
        Args:
            congested_links: List of link_ids to penalize (e.g. ['h1-h3'])
        """
        self.graph = NetworkGraph()
        congested_links = congested_links or []

        # Add all topology nodes
        try:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT node_name FROM network_topology WHERE status='active'")
            for row in cursor.fetchall():
                self.graph.add_node(row['node_name'])
        except Exception as e:
            logger.error(f"Error loading nodes: {e}")

        # Add edges with latency-based weights
        try:
            cursor = self.db.conn.cursor()
            # Get avg latency per link from recent metrics
            cursor.execute("""
                SELECT node_src, node_dst,
                       AVG(latency_ms) as avg_lat,
                       COUNT(*) as samples
                FROM network_metrics
                WHERE latency_ms IS NOT NULL
                  AND timestamp >= datetime('now', '-5 minutes')
                GROUP BY node_src, node_dst
            """)
            metric_rows = {(r['node_src'], r['node_dst']): r['avg_lat']
                          for r in cursor.fetchall()}
        except Exception as e:
            logger.error(f"Error loading metrics: {e}")
            metric_rows = {}

        # Add edges from topology links
        try:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT src_node, dst_node, delay_ms FROM network_links WHERE status='active'")
            for row in cursor.fetchall():
                src, dst = row['src_node'], row['dst_node']
                link_id = f"{src}-{dst}"

                # Use measured latency if available, else configured delay
                base_weight = metric_rows.get((src, dst), row['delay_ms'] or 10.0)

                # Penalize congested links heavily
                if link_id in congested_links:
                    weight = base_weight * 100  # 100x penalty
                    logger.debug(f"Penalizing congested link {link_id}: {base_weight:.1f} → {weight:.1f}")
                else:
                    weight = base_weight

                self.graph.add_edge(src, dst, weight)
        except Exception as e:
            logger.error(f"Error loading links: {e}")

        # Also add direct host-to-host edges based on measured metrics
        # (hosts can reach each other through switches)
        for (src, dst), lat in metric_rows.items():
            if src.startswith('h') and dst.startswith('h'):
                link_id = f"{src}-{dst}"
                weight = lat * 100 if link_id in congested_links else lat
                self.graph.add_edge(src, dst, weight)

        logger.info(f"Graph built: {len(self.graph.nodes)} nodes, "
                   f"{sum(len(v) for v in self.graph.edges.values())} edges")

    def find_optimal_route(self, src: str, dst: str,
                           congested_links: List[str] = None) -> Optional[Dict]:
        """
        Find optimal route from src to dst, avoiding congested links.
        Returns route decision dict.
        """
        congested_links = congested_links or []

        # Build graph with congestion penalties
        self.build_graph(congested_links)

        # Find best path
        result = self.graph.dijkstra(src, dst)
        if not result:
            logger.warning(f"No path found from {src} to {dst}")
            return None

        path, cost = result

        # Also find alternative paths for what-if display
        all_paths = self.graph.get_all_paths(src, dst)

        route_decision = {
            'src': src,
            'dst': dst,
            'optimal_path': path,
            'total_cost_ms': cost,
            'hop_count': len(path) - 1,
            'alternative_paths': [
                {'path': p, 'cost': c}
                for p, c in all_paths[:3]  # top 3 alternatives
                if p != path
            ],
            'congested_links_avoided': congested_links,
            'computed_at': datetime.now().isoformat()
        }

        logger.info(
            f"Optimal route {src}→{dst}: "
            f"{' → '.join(path)} (cost: {cost:.1f}ms, {len(path)-1} hops)"
        )

        return route_decision

    def compute_rerouting(self, congestion_events: List[Dict]) -> List[Dict]:
        """
        For each congested link, compute rerouting decision.
        This is the core 'what-if' analysis of the digital twin.
        """
        reroute_decisions = []
        congested_link_ids = [e['link_id'] for e in congestion_events]

        for event in congestion_events:
            src = event['src']
            dst = event['dst']

            logger.info(f"Computing reroute for congested link {src}→{dst}")

            route = self.find_optimal_route(src, dst, congested_link_ids)
            if route:
                decision = {
                    'congestion_event': event,
                    'route': route,
                    'action': 'reroute' if len(route['optimal_path']) > 2 else 'no_alternate',
                    'timestamp': datetime.now().isoformat()
                }
                reroute_decisions.append(decision)
                self.active_reroutes[f"{src}-{dst}"] = decision
                self.route_history.append(decision)

                # Log to DB
                self.db.insert_event(
                    event_type='configuration',
                    severity='info',
                    node_name=src,
                    description=(
                        f"Reroute computed: {src}→{dst} via "
                        f"{' → '.join(route['optimal_path'])} "
                        f"(cost: {route['total_cost_ms']:.1f}ms)"
                    )
                )

        return reroute_decisions

    def get_active_reroutes(self) -> Dict:
        return self.active_reroutes

    def clear_reroute(self, link_id: str):
        self.active_reroutes.pop(link_id, None)
        logger.info(f"Cleared reroute for {link_id}")
