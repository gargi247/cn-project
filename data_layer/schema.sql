-- Digital Twin Network Database Schema
-- Structured data storage for network metrics

-- Main network metrics table
CREATE TABLE IF NOT EXISTS network_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    node_src TEXT NOT NULL,
    node_dst TEXT NOT NULL,
    latency_ms REAL,
    throughput_mbps REAL,
    packet_loss_pct REAL,
    jitter_ms REAL,
    CONSTRAINT valid_latency CHECK (latency_ms >= 0),
    CONSTRAINT valid_throughput CHECK (throughput_mbps >= 0),
    CONSTRAINT valid_packet_loss CHECK (packet_loss_pct >= 0 AND packet_loss_pct <= 100)
);

-- Index for faster time-series queries
CREATE INDEX IF NOT EXISTS idx_timestamp ON network_metrics(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_nodes ON network_metrics(node_src, node_dst);

-- Network topology table
CREATE TABLE IF NOT EXISTS network_topology (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_name TEXT NOT NULL UNIQUE,
    node_type TEXT NOT NULL CHECK(node_type IN ('host', 'switch', 'controller')),
    ip_address TEXT,
    mac_address TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'inactive', 'failed'))
);

-- Links between network nodes
CREATE TABLE IF NOT EXISTS network_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_node TEXT NOT NULL,
    dst_node TEXT NOT NULL,
    bandwidth_mbps REAL,
    delay_ms REAL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'down', 'degraded')),
    FOREIGN KEY (src_node) REFERENCES network_topology(node_name),
    FOREIGN KEY (dst_node) REFERENCES network_topology(node_name),
    UNIQUE(src_node, dst_node)
);

-- Events and alerts table
CREATE TABLE IF NOT EXISTS network_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT NOT NULL CHECK(event_type IN ('congestion', 'failure', 'recovery', 'anomaly', 'configuration')),
    severity TEXT NOT NULL CHECK(severity IN ('info', 'warning', 'critical')),
    node_name TEXT,
    description TEXT,
    resolved BOOLEAN DEFAULT 0,
    FOREIGN KEY (node_name) REFERENCES network_topology(node_name)
);

-- View for recent metrics (last 1000 records)
CREATE VIEW IF NOT EXISTS recent_metrics AS
SELECT * FROM network_metrics
ORDER BY timestamp DESC
LIMIT 1000;

-- View for link statistics
CREATE VIEW IF NOT EXISTS link_stats AS
SELECT 
    node_src,
    node_dst,
    COUNT(*) as sample_count,
    AVG(latency_ms) as avg_latency,
    MIN(latency_ms) as min_latency,
    MAX(latency_ms) as max_latency,
    AVG(throughput_mbps) as avg_throughput,
    AVG(packet_loss_pct) as avg_packet_loss
FROM network_metrics
GROUP BY node_src, node_dst;
