"""
Network Data Collector for Digital Twin
Collects latency, throughput, packet loss, and jitter metrics
"""

import time
import subprocess
import re
import logging
import argparse
import signal
import sys
from typing import Dict, Optional, Tuple
from datetime import datetime
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_layer.storage import NetworkDatabase

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class NetworkCollector:
    """Collect network metrics from Mininet hosts"""
    
    def __init__(self, db: NetworkDatabase, interval: int = 1):
        """Initialize network collector
        
        Args:
            db: Database instance for storing metrics
            interval: Collection interval in seconds
        """
        self.db = db
        self.interval = interval
        self.running = False
        self.metrics_count = 0
        
    def parse_ping_output(self, output: str) -> Optional[Dict[str, float]]:
        """Parse ping command output to extract metrics
        
        Args:
            output: Raw ping command output
            
        Returns:
            Dictionary with latency and packet loss, or None if parsing fails
        """
        try:
            # Extract packet loss
            loss_match = re.search(r'(\d+)% packet loss', output)
            packet_loss = float(loss_match.group(1)) if loss_match else 0.0
            
            # Extract latency statistics (min/avg/max/stddev)
            latency_match = re.search(
                r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)',
                output
            )
            
            if latency_match:
                min_lat = float(latency_match.group(1))
                avg_lat = float(latency_match.group(2))
                max_lat = float(latency_match.group(3))
                jitter = float(latency_match.group(4))  # mdev is jitter
                
                return {
                    'latency_ms': avg_lat,
                    'packet_loss_pct': packet_loss,
                    'jitter_ms': jitter
                }
            else:
                # No response received
                return {
                    'latency_ms': None,
                    'packet_loss_pct': 100.0,
                    'jitter_ms': None
                }
                
        except Exception as e:
            logger.error(f"Error parsing ping output: {e}")
            return None
    
    def measure_latency(self, src: str, dst: str, count: int = 5) -> Optional[Dict[str, float]]:
        """Measure latency and packet loss between two hosts
        
        Args:
            src: Source host name
            dst: Destination host name
            count: Number of ping packets
            
        Returns:
            Dictionary with metrics or None on failure
        """
        try:
            # Use Mininet CLI to execute ping from source to destination
            # In practice, you'd need to integrate with the running Mininet instance
            # For now, we'll simulate with direct ping
            
            # This is a placeholder - in real implementation, you'd use:
            # net.get(src).cmd(f'ping -c {count} {net.get(dst).IP()}')
            
            cmd = f'ping -c {count} -W 1 {dst}'
            result = subprocess.run(
                cmd.split(),
                capture_output=True,
                text=True,
                timeout=count + 2
            )
            
            if result.returncode != 0 and 'unreachable' not in result.stdout:
                # Some packets may have been lost, but we got some response
                pass
            
            metrics = self.parse_ping_output(result.stdout)
            return metrics
            
        except subprocess.TimeoutExpired:
            logger.warning(f"Ping timeout: {src} -> {dst}")
            return {
                'latency_ms': None,
                'packet_loss_pct': 100.0,
                'jitter_ms': None
            }
        except Exception as e:
            logger.error(f"Error measuring latency {src}->{dst}: {e}")
            return None
    
    def measure_throughput(self, src: str, dst: str) -> Optional[float]:
        """Measure throughput between two hosts using iperf
        
        Args:
            src: Source host name
            dst: Destination host name
            
        Returns:
            Throughput in Mbps or None on failure
        """
        try:
            # This is a placeholder - in real implementation with Mininet:
            # dst_host.cmd('iperf -s &')
            # result = src_host.cmd(f'iperf -c {dst_host.IP()} -t 1')
            
            # For demonstration, return simulated value
            # In production, parse iperf output
            return None  # Not implemented in standalone mode
            
        except Exception as e:
            logger.error(f"Error measuring throughput {src}->{dst}: {e}")
            return None
    
    def collect_link_metrics(self, src: str, dst: str):
        """Collect all metrics for a link and store in database
        
        Args:
            src: Source node name
            dst: Destination node name
        """
        # Measure latency and packet loss
        metrics = self.measure_latency(src, dst, count=3)
        
        if metrics is None:
            logger.warning(f"Failed to collect metrics for {src}->{dst}")
            return
        
        # Measure throughput (optional, can be expensive)
        throughput = self.measure_throughput(src, dst)
        
        # Store in database
        try:
            self.db.insert_metric(
                node_src=src,
                node_dst=dst,
                latency_ms=metrics.get('latency_ms'),
                throughput_mbps=throughput,
                packet_loss_pct=metrics.get('packet_loss_pct'),
                jitter_ms=metrics.get('jitter_ms')
            )
            self.metrics_count += 1
            
            logger.debug(
                f"Collected: {src}->{dst} | "
                f"Latency: {metrics.get('latency_ms'):.2f}ms | "
                f"Loss: {metrics.get('packet_loss_pct'):.1f}% | "
                f"Jitter: {metrics.get('jitter_ms'):.2f}ms"
            )
            
        except Exception as e:
            logger.error(f"Error storing metrics: {e}")
    
    def collect_topology_metrics(self):
        """Collect metrics for all links in the topology"""
        # Get all links from database
        links = self.db.get_topology_links()
        
        if not links:
            logger.warning("No links found in topology")
            return
        
        logger.info(f"Collecting metrics for {len(links)} links...")
        
        for link in links:
            src = link['src_node']
            dst = link['dst_node']
            self.collect_link_metrics(src, dst)
    
    def run_collection_loop(self):
        """Main collection loop"""
        self.running = True
        logger.info(f"Starting collection loop (interval: {self.interval}s)")
        
        try:
            while self.running:
                start_time = time.time()
                
                # Collect metrics for all links
                self.collect_topology_metrics()
                
                # Calculate sleep time to maintain interval
                elapsed = time.time() - start_time
                sleep_time = max(0, self.interval - elapsed)
                
                if self.metrics_count % 10 == 0:
                    logger.info(f"Collected {self.metrics_count} metric samples")
                
                time.sleep(sleep_time)
                
        except KeyboardInterrupt:
            logger.info("\nCollection interrupted by user")
        finally:
            self.stop()
    
    def stop(self):
        """Stop collection loop"""
        self.running = False
        logger.info(f"Collection stopped. Total samples: {self.metrics_count}")


def signal_handler(signum, frame):
    """Handle interrupt signal"""
    logger.info("\nReceived interrupt signal, stopping...")
    sys.exit(0)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Collect network metrics for Digital Twin"
    )
    parser.add_argument(
        '--db',
        default='dtn_network.db',
        help='Database file path'
    )
    parser.add_argument(
        '--interval',
        type=int,
        default=1,
        help='Collection interval in seconds'
    )
    parser.add_argument(
        '--duration',
        type=int,
        help='Collection duration in seconds (runs indefinitely if not specified)'
    )
    
    args = parser.parse_args()
    
    # Setup signal handler
    signal.signal(signal.SIGINT, signal_handler)
    
    # Initialize database
    logger.info("Connecting to database...")
    db = NetworkDatabase(args.db)
    
    # Check if topology exists
    links = db.get_topology_links()
    if not links:
        logger.warning("No network topology found in database!")
        logger.info("Please run topology_builder.py first to create the network")
        return 1
    
    logger.info(f"Found {len(links)} links in topology")
    
    # Create collector
    collector = NetworkCollector(db, interval=args.interval)
    
    # Run collection
    if args.duration:
        logger.info(f"Collecting for {args.duration} seconds...")
        start_time = time.time()
        while time.time() - start_time < args.duration:
            collector.collect_topology_metrics()
            time.sleep(args.interval)
        collector.stop()
    else:
        logger.info("Collecting indefinitely (Ctrl+C to stop)...")
        collector.run_collection_loop()
    
    # Cleanup
    db.close()
    return 0


if __name__ == '__main__':
    exit(main())
