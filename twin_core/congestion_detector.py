"""
Congestion Detector for Digital Twin Network - Phase 2
Rule-based sliding window analysis on collected metrics.
Detects high latency and packet loss on any link.
"""

import sqlite3
import logging
from typing import List, Dict, Optional
from datetime import datetime
import sys, os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_layer.storage import NetworkDatabase

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class CongestionDetector:
    """
    Detects congestion using a sliding window over recent metrics.
    Implements the 'Data Domain' analysis from ITU-T Y.3090.
    """

    def __init__(self, db: NetworkDatabase,
                 latency_threshold_ms: float = 50.0,
                 loss_threshold_pct: float = 5.0,
                 window_size: int = 3):
        """
        Args:
            db: Database instance
            latency_threshold_ms: Flag link if avg latency exceeds this
            loss_threshold_pct: Flag link if avg packet loss exceeds this
            window_size: Number of recent samples to average over
        """
        self.db = db
        self.latency_threshold = latency_threshold_ms
        self.loss_threshold = loss_threshold_pct
        self.window_size = window_size
        self.congested_links: Dict[str, Dict] = {}  # link_id -> event

    def get_recent_window(self, src: str, dst: str) -> List[Dict]:
        """Fetch last N samples for a specific link."""
        try:
            cursor = self.db.conn.cursor()
            cursor.execute("""
                SELECT latency_ms, packet_loss_pct, timestamp
                FROM network_metrics
                WHERE node_src=? AND node_dst=?
                  AND latency_ms IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT ?
            """, (src, dst, self.window_size))
            return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"DB error fetching window: {e}")
            return []

    def analyze_link(self, src: str, dst: str) -> Optional[Dict]:
        """
        Analyze a single link. Returns congestion event dict if congested,
        None if healthy.
        """
        samples = self.get_recent_window(src, dst)
        if len(samples) < 3:
            return None  # not enough data

        avg_latency = sum(s['latency_ms'] for s in samples) / len(samples)
        avg_loss    = sum(s['packet_loss_pct'] or 0 for s in samples) / len(samples)

        congested = False
        reasons = []

        if avg_latency > self.latency_threshold:
            congested = True
            reasons.append(f"latency {avg_latency:.1f}ms > {self.latency_threshold}ms")

        if avg_loss > self.loss_threshold:
            congested = True
            reasons.append(f"loss {avg_loss:.1f}% > {self.loss_threshold}%")

        if congested:
            event = {
                'src': src,
                'dst': dst,
                'link_id': f"{src}-{dst}",
                'avg_latency_ms': avg_latency,
                'avg_loss_pct': avg_loss,
                'reasons': reasons,
                'severity': 'critical' if avg_latency > self.latency_threshold * 2 else 'warning',
                'detected_at': datetime.now().isoformat(),
                'sample_count': len(samples)
            }
            return event
        return None

    def scan_all_links(self) -> List[Dict]:
        """
        Scan all host-to-host links and return list of congestion events.
        This is the main entry point called by the control loop.
        """
        try:
            cursor = self.db.conn.cursor()
            cursor.execute("""
                SELECT DISTINCT node_src, node_dst FROM network_metrics
                WHERE node_src LIKE 'h%' AND node_dst LIKE 'h%'
            """)
            pairs = [(r['node_src'], r['node_dst']) for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Error fetching link pairs: {e}")
            return []

        events = []
        self.congested_links = {}

        for src, dst in pairs:
            event = self.analyze_link(src, dst)
            if event:
                events.append(event)
                self.congested_links[f"{src}-{dst}"] = event
                logger.warning(
                    f"CONGESTION: {src}→{dst} | "
                    f"lat={event['avg_latency_ms']:.1f}ms | "
                    f"loss={event['avg_loss_pct']:.1f}% | "
                    f"{', '.join(event['reasons'])}"
                )

        if not events:
            logger.info("All links healthy")
        else:
            logger.warning(f"Detected {len(events)} congested links")

        return events

    def inject_congestion(self, src_host: str, dst_host: str,
                          delay_ms: int = 100, loss_pct: float = 20.0):
        """
        Inject artificial congestion on a link using tc netem.
        Runs inside the src host's Mininet namespace.
        Used for demo/testing purposes.
        
        Args:
            src_host: Host name (e.g. 'h1')
            dst_host: Target host name (e.g. 'h3')
            delay_ms: Extra delay to add in milliseconds
            loss_pct: Packet loss percentage to inject
        """
        import subprocess, re

        # Find the host's PID
        pid = None
        try:
            for pid_dir in os.listdir('/proc'):
                if not pid_dir.isdigit():
                    continue
                try:
                    with open(f'/proc/{pid_dir}/cmdline', 'rb') as fh:
                        cmdline = fh.read().decode('utf-8', errors='ignore')
                    if f'mininet:{src_host}' in cmdline:
                        pid = pid_dir
                        break
                except Exception:
                    continue
        except Exception:
            pass

        if not pid:
            logger.error(f"Cannot find PID for {src_host}")
            return False

        # Find the interface facing the switch (eth0 in Mininet hosts)
        iface = f"{src_host}-eth0"

        # Apply tc netem rules inside the namespace
        cmds = [
            f"nsenter -t {pid} -n -- tc qdisc del dev {iface} root 2>/dev/null || true",
            f"nsenter -t {pid} -n -- tc qdisc add dev {iface} root netem delay {delay_ms}ms loss {loss_pct}%"
        ]

        for cmd in cmds:
            try:
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                if result.returncode != 0 and 'del' not in cmd:
                    logger.error(f"tc command failed: {result.stderr}")
                    return False
            except Exception as e:
                logger.error(f"Error applying tc: {e}")
                return False

        logger.warning(
            f"INJECTED congestion on {src_host}: "
            f"+{delay_ms}ms delay, {loss_pct}% loss on {iface}"
        )

        # Log to DB
        self.db.insert_event(
            event_type='congestion',
            severity='warning',
            node_name=src_host,
            description=f"Manual congestion injection: +{delay_ms}ms delay, {loss_pct}% loss"
        )
        return True

    def clear_congestion(self, src_host: str):
        """Remove injected tc netem rules from a host."""
        import subprocess

        pid = None
        try:
            for pid_dir in os.listdir('/proc'):
                if not pid_dir.isdigit():
                    continue
                try:
                    with open(f'/proc/{pid_dir}/cmdline', 'rb') as fh:
                        cmdline = fh.read().decode('utf-8', errors='ignore')
                    if f'mininet:{src_host}' in cmdline:
                        pid = pid_dir
                        break
                except Exception:
                    continue
        except Exception:
            pass

        if not pid:
            logger.error(f"Cannot find PID for {src_host}")
            return False

        iface = f"{src_host}-eth0"
        cmd = f"nsenter -t {pid} -n -- tc qdisc del dev {iface} root 2>/dev/null || true"

        subprocess.run(cmd, shell=True)
        logger.info(f"Cleared congestion rules from {src_host}")
        self.db.insert_event(
            event_type='recovery',
            severity='info',
            node_name=src_host,
            description="Congestion rules cleared"
        )
        return True