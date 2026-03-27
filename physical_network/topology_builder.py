"""
Dynamic Mininet Topology Builder from YAML Configuration
Reads YAML config and constructs Mininet network topology
"""

import yaml
import argparse
import logging
from typing import Dict, List
from mininet.net import Mininet
from mininet.node import Controller, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel
import sys
import os

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_layer.storage import NetworkDatabase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class YAMLTopologyBuilder:
    """Build Mininet topology from YAML configuration"""
    
    def __init__(self, config_path: str, db: NetworkDatabase = None):
        """Initialize topology builder
        
        Args:
            config_path: Path to YAML configuration file
            db: Database instance for storing topology
        """
        self.config_path = config_path
        self.config = None
        self.net = None
        self.db = db
        self.nodes = {}
        self.links = []
        
    def load_config(self):
        """Load YAML configuration file"""
        try:
            with open(self.config_path, 'r') as f:
                self.config = yaml.safe_load(f)
            logger.info(f"Loaded configuration: {self.config['network']['name']}")
            return True
        except FileNotFoundError:
            logger.error(f"Configuration file not found: {self.config_path}")
            return False
        except yaml.YAMLError as e:
            logger.error(f"YAML parsing error: {e}")
            return False
    
    def build_topology(self):
        """Construct Mininet topology from configuration"""
        if not self.config:
            logger.error("Configuration not loaded")
            return False
        
        try:
            # Initialize Mininet with controller
            logger.info("Initializing Mininet network...")
            self.net = Mininet(
                controller=Controller,
                switch=OVSSwitch,
                link=TCLink,
                autoSetMacs=True
            )
            
            # Add default controller
            self.net.addController('c0')
            
            # Add nodes
            logger.info("Adding network nodes...")
            for node_config in self.config['network']['nodes']:
                self._add_node(node_config)
            
            # Add links
            logger.info("Adding network links...")
            for link_config in self.config['network']['links']:
                self._add_link(link_config)
            
            logger.info("Topology construction complete")
            return True
            
        except Exception as e:
            logger.error(f"Topology build error: {e}")
            return False
    
    def _add_node(self, node_config: Dict):
        """Add a node to the Mininet network
        
        Args:
            node_config: Node configuration dictionary
        """
        name = node_config['name']
        node_type = node_config['type']
        
        if node_type == 'host':
            ip = node_config.get('ip', None)
            mac = node_config.get('mac', None)
            node = self.net.addHost(name, ip=ip, mac=mac)
            logger.info(f"Added host: {name} (IP: {ip}, MAC: {mac})")
            
            # Store in database
            if self.db:
                self.db.insert_topology_node(name, node_type, ip, mac)
                
        elif node_type == 'switch':
            node = self.net.addSwitch(name)
            logger.info(f"Added switch: {name}")
            
            # Store in database
            if self.db:
                self.db.insert_topology_node(name, node_type)
        else:
            logger.warning(f"Unknown node type: {node_type}")
            return
        
        self.nodes[name] = node
    
    def _add_link(self, link_config: Dict):
        """Add a link between two nodes
        
        Args:
            link_config: Link configuration dictionary
        """
        src = link_config['src']
        dst = link_config['dst']
        bw = link_config.get('bandwidth', 100)  # Mbps
        delay = link_config.get('delay', '0ms')
        loss = link_config.get('loss', 0)
        
        if src not in self.nodes or dst not in self.nodes:
            logger.error(f"Cannot create link: {src} or {dst} not found")
            return
        
        # Add link with QoS parameters
        link = self.net.addLink(
            self.nodes[src],
            self.nodes[dst],
            bw=bw,
            delay=delay,
            loss=loss
        )
        
        self.links.append({
            'src': src,
            'dst': dst,
            'bandwidth': bw,
            'delay': delay,
            'loss': loss
        })
        
        logger.info(f"Added link: {src} <-> {dst} (BW: {bw}Mbps, Delay: {delay}, Loss: {loss}%)")
        
        # Store in database
        if self.db:
            delay_ms = float(delay.replace('ms', ''))
            self.db.insert_link(src, dst, bw, delay_ms)
            self.db.insert_link(dst, src, bw, delay_ms)  # Bidirectional
    
    def start_network(self):
        """Start the Mininet network"""
        if not self.net:
            logger.error("Network not built")
            return False
        
        try:
            logger.info("Starting network...")
            self.net.start()
            
            # Test connectivity
            logger.info("Testing connectivity...")
            self.net.pingAll()
            
            logger.info("Network started successfully")
            return True
            
        except Exception as e:
            logger.error(f"Network start error: {e}")
            return False
    
    def run_cli(self):
        """Start Mininet CLI for interactive testing"""
        if not self.net:
            logger.error("Network not started")
            return
        
        logger.info("Starting Mininet CLI...")
        logger.info("Use 'pingall' to test connectivity")
        logger.info("Use 'iperf' to test bandwidth")
        CLI(self.net)
    
    def generate_traffic(self):
        """Generate traffic based on configuration"""
        if 'traffic' not in self.config:
            logger.info("No traffic configuration found")
            return
        
        flows = self.config['traffic'].get('flows', [])
        logger.info(f"Generating {len(flows)} traffic flows...")
        
        for flow in flows:
            src_name = flow['src']
            dst_name = flow['dst']
            protocol = flow.get('protocol', 'tcp')
            bandwidth = flow.get('bandwidth', '10M')
            duration = flow.get('duration', 10)
            
            src = self.nodes.get(src_name)
            dst = self.nodes.get(dst_name)
            
            if not src or not dst:
                logger.warning(f"Cannot generate flow: {src_name} or {dst_name} not found")
                continue
            
            logger.info(f"Flow: {src_name} -> {dst_name} ({protocol}, {bandwidth}, {duration}s)")
            
            # Use iperf for traffic generation
            if protocol == 'tcp':
                # Start iperf server on destination
                dst.cmd(f'iperf -s -p 5001 &')
                # Start iperf client on source
                src.cmd(f'iperf -c {dst.IP()} -p 5001 -t {duration} -b {bandwidth} &')
            elif protocol == 'udp':
                dst.cmd(f'iperf -s -u -p 5001 &')
                src.cmd(f'iperf -c {dst.IP()} -u -p 5001 -t {duration} -b {bandwidth} &')
    
    def stop_network(self):
        """Stop and cleanup the Mininet network"""
        if self.net:
            logger.info("Stopping network...")
            self.net.stop()
            logger.info("Network stopped")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Build and run Mininet topology from YAML configuration"
    )
    parser.add_argument(
        '--config',
        default='config/topology_config.yaml',
        help='Path to YAML configuration file'
    )
    parser.add_argument(
        '--db',
        default='dtn_network.db',
        help='Database file path'
    )
    parser.add_argument(
        '--no-cli',
        action='store_true',
        help='Skip Mininet CLI (for automated testing)'
    )
    parser.add_argument(
        '--generate-traffic',
        action='store_true',
        help='Generate traffic flows from config'
    )
    
    args = parser.parse_args()
    
    # Set Mininet log level
    setLogLevel('info')
    
    # Initialize database
    db = NetworkDatabase(args.db)
    
    # Build and start topology
    builder = YAMLTopologyBuilder(args.config, db)
    
    if not builder.load_config():
        return 1
    
    if not builder.build_topology():
        return 1
    
    if not builder.start_network():
        return 1
    
    # Generate traffic if requested
    if args.generate_traffic:
        builder.generate_traffic()
    
    # Start CLI unless disabled
    if not args.no_cli:
        builder.run_cli()
    
    # Cleanup
    builder.stop_network()
    db.close()
    
    return 0


if __name__ == '__main__':
    try:
        exit(main())
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user")
        exit(0)
