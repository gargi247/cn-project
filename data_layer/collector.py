"""
Network Data Collector for Digital Twin
Collects latency, throughput, packet loss, and jitter metrics
by running commands inside Mininet host namespaces via nsenter.
"""

import time
import subprocess
import re
import logging
import argparse
import signal
import sys
import os
import sqlite3
from typing import Dict, Optional, List
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_layer.storage import NetworkDatabase

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def get_host_pid(host_name: str) -> Optional[int]:
    """Find PID of a Mininet host process to enter its network namespace."""
    try:
        result = subprocess.run(
            ['grep', '-rl', f'mininet:{host_name}', '/proc/'],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            parts = line.split('/')
            if len(parts) > 2 and parts[2].isdigit():
                return int(parts[2])
    except Exception:
        pass

    # Fallback: scan /proc manually
    try:
        for pid_dir in os.listdir('/proc'):
            if not pid_dir.isdigit():
                continue
            try:
                with open(f'/proc/{pid_dir}/cmdline', 'rb') as f:
                    cmdline = f.read().decode('utf-8', errors='ignore')
                if f'mininet:{host_name}' in cmdline:
                    return int(pid_dir)
            except Exception:
                continue
    except Exception:
        pass
    return None


def run_in_namespace(pid: int, cmd: List[str]) -> Optional[str]:
    """Run a command inside a process's network namespace using nsenter."""
    try:
        result = subprocess.run(
            ['nsenter', '-t', str(pid), '-n', '--'] + cmd,
            capture_output=True, text=True, timeout=15
        )
        return result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return None
    except Exception as e:
        logger.debug(f"nsenter error: {e}")
        return None


class NetworkCollector:
    """Collect network metrics from Mininet hosts via namespace entry."""

    def __init__(self, db: NetworkDatabase, interval: int = 1):
        self.db = db
        self.interval = interval
        self.running = False
        self.metrics_count = 0
        self._pid_cache: Dict[str, Optional[int]] = {}
        self._ip_cache:  Dict[str, Optional[str]] = {}

    def _get_node_ip(self, node_name: str) -> Optional[str]:
        """Get IP for a node — hosts only, switches have no IP."""
        if node_name not in self._ip_cache:
            # Look up from DB first
            try:
                cursor = self.db.conn.cursor()
                cursor.execute(
                    "SELECT ip_address FROM network_topology WHERE node_name=?",
                    (node_name,)
                )
                row = cursor.fetchone()
                if row and row['ip_address']:
                    self._ip_cache[node_name] = row['ip_address']
                    return self._ip_cache[node_name]
            except Exception:
                pass
            # Fallback: derive from name (h1→10.0.0.1)
            match = re.match(r'h(\d+)$', node_name)
            self._ip_cache[node_name] = f"10.0.0.{match.group(1)}" if match else None
        return self._ip_cache[node_name]

    def _get_pid(self, host_name: str) -> Optional[int]:
        """Get (cached) PID for a Mininet host namespace."""
        if host_name not in self._pid_cache:
            pid = get_host_pid(host_name)
            self._pid_cache[host_name] = pid
            if pid:
                logger.debug(f"Namespace PID for {host_name}: {pid}")
                print(f"  ✓ {host_name} → PID {pid}")
            else:
                logger.warning(f"No namespace PID found for {host_name}")
        return self._pid_cache[host_name]

    def parse_ping_output(self, output: str) -> Optional[Dict]:
        """Parse ping stdout into a metrics dict."""
        try:
            loss_match = re.search(r'(\d+(?:\.\d+)?)% packet loss', output)
            packet_loss = float(loss_match.group(1)) if loss_match else 100.0

            lat_match = re.search(
                r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)',
                output
            )
            if lat_match:
                return {
                    'latency_ms':     float(lat_match.group(2)),
                    'packet_loss_pct': packet_loss,
                    'jitter_ms':       float(lat_match.group(4))
                }
            return {'latency_ms': None, 'packet_loss_pct': packet_loss, 'jitter_ms': None}
        except Exception as e:
            logger.error(f"Ping parse error: {e}")
            return None

    def measure_latency(self, src: str, dst_ip: str, count: int = 3) -> Optional[Dict]:
        """Ping dst_ip from inside src's network namespace."""
        src_pid = self._get_pid(src)
        if src_pid:
            output = run_in_namespace(src_pid, ['ping', '-c', str(count), '-W', '2', dst_ip])
            if output:
                metrics = self.parse_ping_output(output)
                if metrics is not None:
                    return metrics

        # Fallback: ping from host machine (may show 100% loss if routing not set up)
        try:
            result = subprocess.run(
                ['ping', '-c', str(count), '-W', '2', dst_ip],
                capture_output=True, text=True, timeout=count * 3
            )
            return self.parse_ping_output(result.stdout)
        except Exception as e:
            logger.debug(f"Fallback ping failed: {e}")

        return {'latency_ms': None, 'packet_loss_pct': 100.0, 'jitter_ms': None}

    def _store_metric(self, src: str, dst: str, metrics: Dict):
        """Insert metric into DB with retry on lock."""
        for attempt in range(5):
            try:
                self.db.insert_metric(
                    node_src=src,
                    node_dst=dst,
                    latency_ms=metrics.get('latency_ms'),
                    throughput_mbps=None,
                    packet_loss_pct=metrics.get('packet_loss_pct'),
                    jitter_ms=metrics.get('jitter_ms')
                )
                self.metrics_count += 1

                lat  = metrics.get('latency_ms')
                loss = metrics.get('packet_loss_pct')
                jit  = metrics.get('jitter_ms')
                logger.debug(
                    f"{src}->{dst} | "
                    f"Lat: {f'{lat:.2f}ms' if lat is not None else 'N/A'} | "
                    f"Loss: {f'{loss:.1f}%' if loss is not None else 'N/A'} | "
                    f"Jitter: {f'{jit:.2f}ms' if jit is not None else 'N/A'}"
                )
                return

            except sqlite3.OperationalError as e:
                if 'locked' in str(e) and attempt < 4:
                    wait = 0.5 * (attempt + 1)
                    logger.warning(f"DB locked, retry {attempt+1}/5 in {wait}s")
                    time.sleep(wait)
                else:
                    logger.error(f"DB error after retries: {e}")
                    return
            except Exception as e:
                logger.error(f"Error storing metric: {e}")
                return

    def collect_host_pairs(self):
        """
        Collect metrics between all host pairs.
        Switches don't have IPs so we measure host-to-host paths,
        which traverse the switches naturally and give meaningful data.
        """
        # Get all hosts from topology
        try:
            cursor = self.db.conn.cursor()
            cursor.execute(
                "SELECT node_name, ip_address FROM network_topology "
                "WHERE node_type='host' AND status='active'"
            )
            hosts = [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Could not fetch hosts: {e}")
            return

        if len(hosts) < 2:
            logger.warning("Need at least 2 hosts to measure")
            return

        logger.info(f"Measuring {len(hosts)} hosts → {len(hosts)-1} pairs each")

        for i, src_host in enumerate(hosts):
            src = src_host['node_name']
            src_ip = self._get_node_ip(src)
            if not src_ip:
                continue

            for dst_host in hosts:
                dst = dst_host['node_name']
                if src == dst:
                    continue
                dst_ip = self._get_node_ip(dst)
                if not dst_ip:
                    continue

                metrics = self.measure_latency(src, dst_ip, count=3)
                if metrics:
                    self._store_metric(src, dst, metrics)

    def run_collection_loop(self):
        """Main collection loop."""
        self.running = True
        logger.info(f"Starting collection (interval: {self.interval}s)")
        last_cache_clear = time.time()

        try:
            while self.running:
                start_time = time.time()

                # Reconnect only if connection was lost
                try:
                    self.db.conn.execute("SELECT 1")
                except Exception:
                    logger.info("Reconnecting to database...")
                    self.db.connect()

                # Refresh PID cache every 60s in case Mininet restarted
                if time.time() - last_cache_clear > 60:
                    self._pid_cache.clear()
                    self._ip_cache.clear()
                    last_cache_clear = time.time()

                self.collect_host_pairs()

                elapsed = time.time() - start_time
                sleep_time = max(0, self.interval - elapsed)

                if self.metrics_count > 0 and self.metrics_count % 30 == 0:
                    logger.info(f"Total samples collected: {self.metrics_count}")

                time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("\nStopped by user")
        finally:
            self.stop()

    def stop(self):
        self.running = False
        logger.info(f"Collector stopped. Total samples: {self.metrics_count}")


def signal_handler(signum, frame):
    logger.info("\nInterrupt received, stopping...")
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="DTN Network Metrics Collector")
    parser.add_argument('--db',       default='dtn_network.db')
    parser.add_argument('--interval', type=int, default=1)
    parser.add_argument('--duration', type=int)
    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal_handler)

    logger.info("Connecting to database...")
    db = NetworkDatabase(args.db)

    hosts = []
    try:
        cursor = db.conn.cursor()
        cursor.execute("SELECT node_name FROM network_topology WHERE node_type='host'")
        hosts = cursor.fetchall()
    except Exception:
        pass

    if not hosts:
        logger.error("No hosts found in topology DB. Run topology_builder.py first.")
        return 1

    logger.info(f"Found {len(hosts)} hosts to monitor")

    result = subprocess.run(['which', 'nsenter'], capture_output=True)
    if result.returncode != 0:
        logger.warning("nsenter not found — sudo apt-get install util-linux")
    else:
        logger.info("nsenter available — collecting from Mininet namespaces")

    collector = NetworkCollector(db, interval=args.interval)

    if args.duration:
        start = time.time()
        while time.time() - start < args.duration:
            collector.collect_host_pairs()
            time.sleep(args.interval)
        collector.stop()
    else:
        collector.run_collection_loop()

    db.close()
    return 0


if __name__ == '__main__':
    exit(main())