"""
Digital Twin Core - Real-time Synchronization Engine
Maintains state consistency between physical network and digital twin
"""

import time
import logging
import threading
from typing import Dict, List, Optional
from datetime import datetime
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_layer.storage import NetworkDatabase

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TwinState:
    """Represents the current state of the digital twin"""
    
    def __init__(self):
        self.topology = {
            'nodes': {},
            'links': {}
        }
        self.metrics = {
            'current': {},
            'historical': []
        }
        self.last_update = None
        self.anomalies = []
        
    def update_topology(self, nodes: List[Dict], links: List[Dict]):
        """Update topology state
        
        Args:
            nodes: List of network nodes
            links: List of network links
        """
        self.topology['nodes'] = {node['node_name']: node for node in nodes}
        self.topology['links'] = {
            f"{link['src_node']}-{link['dst_node']}": link 
            for link in links
        }
        logger.debug(f"Topology updated: {len(nodes)} nodes, {len(links)} links")
    
    def update_metrics(self, metrics: List[Dict]):
        """Update current metrics state
        
        Args:
            metrics: List of recent network metrics
        """
        # Update current metrics (latest value for each link)
        for metric in metrics:
            link_id = f"{metric['node_src']}-{metric['node_dst']}"
            if link_id not in self.metrics['current']:
                self.metrics['current'][link_id] = metric
            elif metric['timestamp'] > self.metrics['current'][link_id]['timestamp']:
                self.metrics['current'][link_id] = metric
        
        # Keep limited historical data in memory
        self.metrics['historical'] = metrics[:100]  # Last 100 samples
        self.last_update = datetime.now()
    
    def detect_anomalies(self, threshold_latency: float = 100.0,
                        threshold_loss: float = 5.0):
        """Detect network anomalies based on thresholds
        
        Args:
            threshold_latency: Latency threshold in ms
            threshold_loss: Packet loss threshold in percentage
        """
        self.anomalies = []
        
        for link_id, metric in self.metrics['current'].items():
            # Check latency
            if metric.get('latency_ms') and metric['latency_ms'] > threshold_latency:
                self.anomalies.append({
                    'type': 'high_latency',
                    'link': link_id,
                    'value': metric['latency_ms'],
                    'threshold': threshold_latency,
                    'timestamp': metric['timestamp']
                })
            
            # Check packet loss
            if metric.get('packet_loss_pct') and metric['packet_loss_pct'] > threshold_loss:
                self.anomalies.append({
                    'type': 'high_packet_loss',
                    'link': link_id,
                    'value': metric['packet_loss_pct'],
                    'threshold': threshold_loss,
                    'timestamp': metric['timestamp']
                })
        
        if self.anomalies:
            logger.warning(f"Detected {len(self.anomalies)} anomalies")
        
        return self.anomalies
    
    def get_link_state(self, src: str, dst: str) -> Optional[Dict]:
        """Get current state of a specific link
        
        Args:
            src: Source node name
            dst: Destination node name
            
        Returns:
            Dictionary with link state or None
        """
        link_id = f"{src}-{dst}"
        return self.metrics['current'].get(link_id)
    
    def get_summary(self) -> Dict:
        """Get summary of twin state
        
        Returns:
            Dictionary with state summary
        """
        return {
            'node_count': len(self.topology['nodes']),
            'link_count': len(self.topology['links']),
            'monitored_links': len(self.metrics['current']),
            'anomaly_count': len(self.anomalies),
            'last_update': self.last_update.isoformat() if self.last_update else None
        }


class SyncEngine:
    """Synchronization engine for digital twin"""
    
    def __init__(self, db: NetworkDatabase, sync_interval: float = 5.0):
        """Initialize sync engine
        
        Args:
            db: Database instance
            sync_interval: Sync interval in seconds
        """
        self.db = db
        self.sync_interval = sync_interval
        self.twin_state = TwinState()
        self.running = False
        self.sync_thread = None
        self.sync_count = 0
        
    def start(self):
        """Start synchronization loop"""
        if self.running:
            logger.warning("Sync engine already running")
            return
        
        self.running = True
        self.sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self.sync_thread.start()
        logger.info(f"Sync engine started (interval: {self.sync_interval}s)")
    
    def stop(self):
        """Stop synchronization loop"""
        self.running = False
        if self.sync_thread:
            self.sync_thread.join(timeout=5)
        logger.info(f"Sync engine stopped (total syncs: {self.sync_count})")
    
    def _sync_loop(self):
        """Main synchronization loop"""
        while self.running:
            try:
                start_time = time.time()
                
                # Perform sync
                self.sync_from_physical()
                self.sync_count += 1
                
                # Log periodically
                if self.sync_count % 10 == 0:
                    summary = self.twin_state.get_summary()
                    logger.info(f"Sync #{self.sync_count}: {summary}")
                
                # Sleep to maintain interval
                elapsed = time.time() - start_time
                sleep_time = max(0, self.sync_interval - elapsed)
                time.sleep(sleep_time)
                
            except Exception as e:
                logger.error(f"Error in sync loop: {e}")
                time.sleep(self.sync_interval)
    
    def sync_from_physical(self):
        """Sync twin state from physical network (database)"""
        # Update topology
        nodes = self.db.get_topology_nodes()
        links = self.db.get_topology_links()
        self.twin_state.update_topology(nodes, links)
        
        # Update metrics
        recent_metrics = self.db.get_recent_metrics(limit=100)
        self.twin_state.update_metrics(recent_metrics)
        
        # Detect anomalies (in-memory only — control loop handles DB writes)
        anomalies = self.twin_state.detect_anomalies()
    
    def get_state(self) -> TwinState:
        """Get current twin state
        
        Returns:
            TwinState instance
        """
        return self.twin_state
    
    def predict_future_state(self, horizon_seconds: int = 60) -> Dict:
        """Predict future network state (placeholder for Phase 2)
        
        Args:
            horizon_seconds: Prediction horizon in seconds
            
        Returns:
            Dictionary with predicted state
        """
        # This is a placeholder - will be implemented in Phase 2 with ML models
        logger.info(f"Prediction requested for +{horizon_seconds}s horizon")
        
        return {
            'horizon': horizon_seconds,
            'prediction': 'not_implemented',
            'message': 'Prediction will be implemented in Phase 2 with ML models'
        }


def main():
    """Main entry point for standalone testing"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Digital Twin Synchronization Engine"
    )
    parser.add_argument(
        '--db',
        default='dtn_network.db',
        help='Database file path'
    )
    parser.add_argument(
        '--interval',
        type=float,
        default=5.0,
        help='Sync interval in seconds'
    )
    parser.add_argument(
        '--duration',
        type=int,
        help='Run duration in seconds (runs indefinitely if not specified)'
    )
    
    args = parser.parse_args()
    
    # Initialize database
    db = NetworkDatabase(args.db)
    
    # Create and start sync engine
    engine = SyncEngine(db, sync_interval=args.interval)
    engine.start()
    
    try:
        if args.duration:
            logger.info(f"Running for {args.duration} seconds...")
            time.sleep(args.duration)
        else:
            logger.info("Running indefinitely (Ctrl+C to stop)...")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        logger.info("\nShutdown requested...")
    finally:
        engine.stop()
        db.close()
    
    # Print final summary
    summary = engine.get_state().get_summary()
    print("\nFinal Twin State Summary:")
    for key, value in summary.items():
        print(f"  {key}: {value}")


if __name__ == '__main__':
    exit(main())
