"""
SQLite Database Interface for Digital Twin Network
Handles all database operations with structured schema
"""

import sqlite3
import os
from datetime import datetime
from typing import List, Dict, Optional, Tuple
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class NetworkDatabase:
    """Database manager for DTN metrics and topology"""
    
    def __init__(self, db_path: str = "dtn_network.db"):
        """Initialize database connection
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self.conn = None
        self.connect()
        
    def connect(self):
        """Establish database connection"""
        try:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row  # Enable dict-like access
            logger.info(f"Connected to database: {self.db_path}")
        except sqlite3.Error as e:
            logger.error(f"Database connection error: {e}")
            raise
    
    def initialize_schema(self, schema_file: str = "data_layer/schema.sql"):
        """Create database tables from schema file
        
        Args:
            schema_file: Path to SQL schema file
        """
        try:
            with open(schema_file, 'r') as f:
                schema_sql = f.read()
            
            cursor = self.conn.cursor()
            cursor.executescript(schema_sql)
            self.conn.commit()
            logger.info("Database schema initialized successfully")
        except FileNotFoundError:
            logger.error(f"Schema file not found: {schema_file}")
            raise
        except sqlite3.Error as e:
            logger.error(f"Schema initialization error: {e}")
            raise
    
    def insert_metric(self, node_src: str, node_dst: str, 
                     latency_ms: Optional[float] = None,
                     throughput_mbps: Optional[float] = None,
                     packet_loss_pct: Optional[float] = None,
                     jitter_ms: Optional[float] = None) -> int:
        """Insert a network metric record
        
        Args:
            node_src: Source node name
            node_dst: Destination node name
            latency_ms: Latency in milliseconds
            throughput_mbps: Throughput in Mbps
            packet_loss_pct: Packet loss percentage
            jitter_ms: Jitter in milliseconds
            
        Returns:
            ID of inserted record
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO network_metrics 
            (node_src, node_dst, latency_ms, throughput_mbps, packet_loss_pct, jitter_ms)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (node_src, node_dst, latency_ms, throughput_mbps, packet_loss_pct, jitter_ms))
        self.conn.commit()
        return cursor.lastrowid
    
    def insert_topology_node(self, node_name: str, node_type: str,
                            ip_address: Optional[str] = None,
                            mac_address: Optional[str] = None) -> int:
        """Insert a network topology node
        
        Args:
            node_name: Unique node identifier
            node_type: Type ('host', 'switch', 'controller')
            ip_address: IP address of node
            mac_address: MAC address of node
            
        Returns:
            ID of inserted record
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO network_topology 
                (node_name, node_type, ip_address, mac_address)
                VALUES (?, ?, ?, ?)
            """, (node_name, node_type, ip_address, mac_address))
            self.conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            logger.warning(f"Node {node_name} already exists")
            return -1
    
    def insert_link(self, src_node: str, dst_node: str,
                   bandwidth_mbps: float, delay_ms: float) -> int:
        """Insert a network link
        
        Args:
            src_node: Source node name
            dst_node: Destination node name
            bandwidth_mbps: Link bandwidth in Mbps
            delay_ms: Link delay in milliseconds
            
        Returns:
            ID of inserted record
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO network_links 
                (src_node, dst_node, bandwidth_mbps, delay_ms)
                VALUES (?, ?, ?, ?)
            """, (src_node, dst_node, bandwidth_mbps, delay_ms))
            self.conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            logger.warning(f"Link {src_node}->{dst_node} already exists")
            return -1
    
    def get_recent_metrics(self, limit: int = 100) -> List[Dict]:
        """Retrieve recent network metrics
        
        Args:
            limit: Maximum number of records to return
            
        Returns:
            List of metric dictionaries
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM network_metrics 
            ORDER BY timestamp DESC 
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in cursor.fetchall()]
    
    def get_metrics_by_link(self, node_src: str, node_dst: str,
                           hours: int = 1) -> List[Dict]:
        """Get metrics for a specific link within time window
        
        Args:
            node_src: Source node name
            node_dst: Destination node name
            hours: Time window in hours
            
        Returns:
            List of metric dictionaries
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM network_metrics
            WHERE node_src = ? AND node_dst = ?
            AND timestamp >= datetime('now', '-' || ? || ' hours')
            ORDER BY timestamp DESC
        """, (node_src, node_dst, hours))
        return [dict(row) for row in cursor.fetchall()]
    
    def get_link_statistics(self) -> List[Dict]:
        """Get aggregated statistics for all links
        
        Returns:
            List of link statistics dictionaries
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM link_stats")
        return [dict(row) for row in cursor.fetchall()]
    
    def get_topology_nodes(self) -> List[Dict]:
        """Get all topology nodes
        
        Returns:
            List of node dictionaries
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM network_topology WHERE status = 'active'")
        return [dict(row) for row in cursor.fetchall()]
    
    def get_topology_links(self) -> List[Dict]:
        """Get all topology links
        
        Returns:
            List of link dictionaries
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM network_links WHERE status = 'active'")
        return [dict(row) for row in cursor.fetchall()]
    
    def insert_event(self, event_type: str, severity: str,
                    node_name: Optional[str] = None,
                    description: Optional[str] = None) -> int:
        """Log a network event
        
        Args:
            event_type: Type of event
            severity: Severity level ('info', 'warning', 'critical')
            node_name: Associated node (optional)
            description: Event description
            
        Returns:
            ID of inserted record
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO network_events 
            (event_type, severity, node_name, description)
            VALUES (?, ?, ?, ?)
        """, (event_type, severity, node_name, description))
        self.conn.commit()
        return cursor.lastrowid
    
    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed")


# CLI for database initialization
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="DTN Database Manager")
    parser.add_argument("--init", action="store_true", help="Initialize database schema")
    parser.add_argument("--db", default="dtn_network.db", help="Database file path")
    
    args = parser.parse_args()
    
    db = NetworkDatabase(args.db)
    
    if args.init:
        print("Initializing database schema...")
        db.initialize_schema()
        print("Database initialized successfully!")
    else:
        print(f"Database: {args.db}")
        print("Use --init to initialize schema")
    
    db.close()
