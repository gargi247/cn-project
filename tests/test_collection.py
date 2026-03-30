"""
Unit Tests for Digital Twin Network - Phase 1
Tests database, collection, and sync components
"""

import unittest
import os
import sys
import time
from datetime import datetime

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_layer.storage import NetworkDatabase
from twin_core.sync_engine import TwinState, SyncEngine


class TestDatabase(unittest.TestCase):
    """Test database operations"""
    
    def setUp(self):
        """Create test database"""
        self.test_db = "test_dtn.db"
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        self.db = NetworkDatabase(self.test_db)
        self.db.initialize_schema()
    
    def tearDown(self):
        """Cleanup test database"""
        self.db.close()
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
    
    def test_schema_initialization(self):
        """Test database schema creation"""
        # Verify tables exist
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        
        self.assertIn('network_metrics', tables)
        self.assertIn('network_topology', tables)
        self.assertIn('network_links', tables)
        self.assertIn('network_events', tables)
    
    def test_insert_topology_node(self):
        """Test inserting network nodes"""
        node_id = self.db.insert_topology_node(
            node_name='h1',
            node_type='host',
            ip_address='10.0.0.1',
            mac_address='00:00:00:00:00:01'
        )
        
        self.assertGreater(node_id, 0)
        
        # Verify node was inserted
        nodes = self.db.get_topology_nodes()
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]['node_name'], 'h1')
        self.assertEqual(nodes[0]['ip_address'], '10.0.0.1')
    
    def test_insert_duplicate_node(self):
        """Test that duplicate nodes are rejected"""
        self.db.insert_topology_node('h1', 'host')
        duplicate_id = self.db.insert_topology_node('h1', 'host')
        
        self.assertEqual(duplicate_id, -1)
        
        # Verify only one node exists
        nodes = self.db.get_topology_nodes()
        self.assertEqual(len(nodes), 1)
    
    def test_insert_link(self):
        """Test inserting network links"""
        # Insert nodes first
        self.db.insert_topology_node('h1', 'host')
        self.db.insert_topology_node('s1', 'switch')
        
        # Insert link
        link_id = self.db.insert_link('h1', 's1', 100.0, 5.0)
        
        self.assertGreater(link_id, 0)
        
        # Verify link was inserted
        links = self.db.get_topology_links()
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]['src_node'], 'h1')
        self.assertEqual(links[0]['dst_node'], 's1')
    
    def test_insert_metric(self):
        """Test inserting network metrics"""
        metric_id = self.db.insert_metric(
            node_src='h1',
            node_dst='h2',
            latency_ms=10.5,
            throughput_mbps=95.0,
            packet_loss_pct=0.1,
            jitter_ms=0.5
        )
        
        self.assertGreater(metric_id, 0)
        
        # Verify metric was inserted
        metrics = self.db.get_recent_metrics(limit=1)
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0]['node_src'], 'h1')
        self.assertAlmostEqual(metrics[0]['latency_ms'], 10.5)
    
    def test_get_metrics_by_link(self):
        """Test retrieving metrics for specific link"""
        # Insert multiple metrics
        self.db.insert_metric('h1', 'h2', 10.0, 100.0, 0.0, 0.1)
        self.db.insert_metric('h1', 'h2', 12.0, 98.0, 0.5, 0.2)
        self.db.insert_metric('h2', 'h3', 15.0, 95.0, 1.0, 0.3)
        
        # Get metrics for h1->h2
        metrics = self.db.get_metrics_by_link('h1', 'h2', hours=1)
        
        self.assertEqual(len(metrics), 2)
        self.assertTrue(all(m['node_src'] == 'h1' for m in metrics))
        self.assertTrue(all(m['node_dst'] == 'h2' for m in metrics))
    
    def test_link_statistics(self):
        """Test link statistics aggregation"""
        # Insert metrics for multiple links
        for i in range(10):
            self.db.insert_metric('h1', 'h2', 10.0 + i, 100.0, i * 0.1, 0.1)
        
        stats = self.db.get_link_statistics()
        
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]['node_src'], 'h1')
        self.assertEqual(stats[0]['sample_count'], 10)
        self.assertAlmostEqual(stats[0]['avg_latency'], 14.5, places=1)


class TestTwinState(unittest.TestCase):
    """Test digital twin state management"""
    
    def setUp(self):
        """Create twin state instance"""
        self.state = TwinState()
    
    def test_update_topology(self):
        """Test topology state update"""
        nodes = [
            {'node_name': 'h1', 'node_type': 'host'},
            {'node_name': 's1', 'node_type': 'switch'}
        ]
        links = [
            {'src_node': 'h1', 'dst_node': 's1', 'bandwidth_mbps': 100}
        ]
        
        self.state.update_topology(nodes, links)
        
        self.assertEqual(len(self.state.topology['nodes']), 2)
        self.assertEqual(len(self.state.topology['links']), 1)
        self.assertIn('h1', self.state.topology['nodes'])
    
    def test_update_metrics(self):
        """Test metrics state update"""
        metrics = [
            {
                'node_src': 'h1',
                'node_dst': 'h2',
                'timestamp': '2024-01-01 12:00:00',
                'latency_ms': 10.0,
                'packet_loss_pct': 0.0
            }
        ]
        
        self.state.update_metrics(metrics)
        
        self.assertEqual(len(self.state.metrics['current']), 1)
        self.assertIsNotNone(self.state.last_update)
    
    def test_anomaly_detection_latency(self):
        """Test high latency anomaly detection"""
        metrics = [
            {
                'node_src': 'h1',
                'node_dst': 'h2',
                'timestamp': '2024-01-01 12:00:00',
                'latency_ms': 150.0,  # Above default threshold of 100
                'packet_loss_pct': 0.0
            }
        ]
        
        self.state.update_metrics(metrics)
        anomalies = self.state.detect_anomalies(threshold_latency=100.0)
        
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]['type'], 'high_latency')
        self.assertEqual(anomalies[0]['value'], 150.0)
    
    def test_anomaly_detection_packet_loss(self):
        """Test high packet loss anomaly detection"""
        metrics = [
            {
                'node_src': 'h1',
                'node_dst': 'h2',
                'timestamp': '2024-01-01 12:00:00',
                'latency_ms': 10.0,
                'packet_loss_pct': 10.0  # Above default threshold of 5%
            }
        ]
        
        self.state.update_metrics(metrics)
        anomalies = self.state.detect_anomalies(threshold_loss=5.0)
        
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]['type'], 'high_packet_loss')
        self.assertEqual(anomalies[0]['value'], 10.0)
    
    def test_get_link_state(self):
        """Test retrieving specific link state"""
        metrics = [
            {
                'node_src': 'h1',
                'node_dst': 'h2',
                'timestamp': '2024-01-01 12:00:00',
                'latency_ms': 10.0,
                'packet_loss_pct': 0.0
            }
        ]
        
        self.state.update_metrics(metrics)
        link_state = self.state.get_link_state('h1', 'h2')
        
        self.assertIsNotNone(link_state)
        self.assertEqual(link_state['latency_ms'], 10.0)
    
    def test_get_summary(self):
        """Test state summary generation"""
        nodes = [{'node_name': 'h1', 'node_type': 'host'}]
        links = [{'src_node': 'h1', 'dst_node': 's1', 'bandwidth_mbps': 100}]
        
        self.state.update_topology(nodes, links)
        summary = self.state.get_summary()
        
        self.assertEqual(summary['node_count'], 1)
        self.assertEqual(summary['link_count'], 1)
        self.assertIn('last_update', summary)


class TestIntegration(unittest.TestCase):
    """Integration tests for full pipeline"""
    
    def setUp(self):
        """Setup test environment"""
        self.test_db = "test_integration.db"
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        self.db = NetworkDatabase(self.test_db)
        self.db.initialize_schema()
    
    def tearDown(self):
        """Cleanup"""
        self.db.close()
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
    
    def test_full_pipeline(self):
        """Test data flow from database to twin state"""
        # 1. Insert topology
        self.db.insert_topology_node('h1', 'host', '10.0.0.1')
        self.db.insert_topology_node('h2', 'host', '10.0.0.2')
        self.db.insert_link('h1', 'h2', 100.0, 5.0)
        
        # 2. Insert metrics
        for i in range(5):
            self.db.insert_metric('h1', 'h2', 10.0 + i, 100.0, 0.0, 0.1)
        
        # 3. Create sync engine and sync
        engine = SyncEngine(self.db, sync_interval=1.0)
        engine.sync_from_physical()
        
        # 4. Verify twin state
        state = engine.get_state()
        summary = state.get_summary()
        
        self.assertEqual(summary['node_count'], 2)
        self.assertEqual(summary['link_count'], 1)
        self.assertEqual(summary['monitored_links'], 1)


def run_tests():
    """Run all tests"""
    # Create test suite
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    # Add test classes
    suite.addTests(loader.loadTestsFromTestCase(TestDatabase))
    suite.addTests(loader.loadTestsFromTestCase(TestTwinState))
    suite.addTests(loader.loadTestsFromTestCase(TestIntegration))
    
    # Run tests
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    # Return exit code
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    exit(run_tests())
