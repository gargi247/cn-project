"""
Dynamic Mininet Topology Builder from YAML Configuration
Reads YAML config and constructs Mininet network topology
"""

import yaml
import argparse
import logging
from typing import Dict, List
from mininet.net import Mininet
from mininet.node import Controller, OVSSwitch, OVSController
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
    
    def register_topology_in_db(self):
        """Write topology to DB then immediately close the connection.
        Called once before Mininet starts so the DB is free for other processes.
        """
        if not self.db:
            return
        for node_config in self.config['network']['nodes']:
            name = node_config['name']
            node_type = node_config['type']
            ip  = node_config.get('ip')
            mac = node_config.get('mac')
            self.db.insert_topology_node(name, node_type, ip, mac)

        for link_config in self.config['network']['links']:
            src   = link_config['src']
            dst   = link_config['dst']
            bw    = link_config.get('bandwidth', 100)
            delay = link_config.get('delay', '0ms')
            delay_ms = float(delay.replace('ms', ''))
            self.db.insert_link(src, dst, bw, delay_ms)
            self.db.insert_link(dst, src, bw, delay_ms)

        # ---- CRITICAL: release the DB lock before Mininet CLI starts ----
        self.db.close()
        self.db = None
        logger.info("Topology registered in DB — connection closed (free for collector)")

    def build_topology(self):
        """Construct Mininet topology from configuration"""
        if not self.config:
            logger.error("Configuration not loaded")
            return False
        
        try:
            logger.info("Initializing Mininet network...")
            self.net = Mininet(
                controller=OVSController,
                switch=OVSSwitch,
                link=TCLink,
                autoSetMacs=True
            )
            self.net.addController('c0')
            
            logger.info("Adding network nodes...")
            for node_config in self.config['network']['nodes']:
                self._add_node(node_config)
            
            logger.info("Adding network links...")
            for link_config in self.config['network']['links']:
                self._add_link(link_config)
            
            logger.info("Topology construction complete")
            return True
            
        except Exception as e:
            logger.error(f"Topology build error: {e}")
            return False
    
    def _add_node(self, node_config: Dict):
        """Add a node to the Mininet network (no DB writes here)"""
        name = node_config['name']
        node_type = node_config['type']
        
        if node_type == 'host':
            ip  = node_config.get('ip')
            mac = node_config.get('mac')
            node = self.net.addHost(name, ip=ip, mac=mac)
            logger.info(f"Added host: {name} (IP: {ip}, MAC: {mac})")
        elif node_type == 'switch':
            node = self.net.addSwitch(name)
            logger.info(f"Added switch: {name}")
        else:
            logger.warning(f"Unknown node type: {node_type}")
            return
        
        self.nodes[name] = node
    
    def _add_link(self, link_config: Dict):
        """Add a link between two nodes (no DB writes here)"""
        src  = link_config['src']
        dst  = link_config['dst']
        bw   = link_config.get('bandwidth', 100)
        delay = link_config.get('delay', '0ms')
        loss  = link_config.get('loss', 0)
        
        if src not in self.nodes or dst not in self.nodes:
            logger.error(f"Cannot create link: {src} or {dst} not found")
            return
        
        self.net.addLink(self.nodes[src], self.nodes[dst], bw=bw, delay=delay, loss=loss)
        self.links.append({'src': src, 'dst': dst, 'bandwidth': bw, 'delay': delay, 'loss': loss})
        logger.info(f"Added link: {src} <-> {dst} (BW: {bw}Mbps, Delay: {delay}, Loss: {loss}%)")
    
    def start_network(self):
        """Start the Mininet network"""
        if not self.net:
            logger.error("Network not built")
            return False
        try:
            logger.info("Starting network...")
            self.net.start()
            logger.info("Network started successfully")
            self.setup_flood_flows()
            self.setup_static_arp()
            logger.info("Ready — type 'pingall' in CLI to verify connectivity")
            return True
        except Exception as e:
            logger.error(f"Network start error: {e}")
            return False
    


    def setup_flood_flows(self):
        """Install flood flow rules on all switches so packets reach all ports.
        This bypasses the need for a learning controller and ensures full connectivity."""
        logger.info("Installing flood flow rules on all switches...")
        switches = [n for n in self.nodes.values() 
                    if hasattr(n, 'dpid')]  # only switches have dpid
        # Get switch names from config
        switch_names = [n['name'] for n in self.config['network']['nodes'] 
                        if n['type'] == 'switch']
        for sw_name in switch_names:
            self.net.get(sw_name).cmd(f'ovs-ofctl add-flow {sw_name} action=flood')
            logger.info(f"  {sw_name}: flood flow installed")
        logger.info("Flood flows installed — all hosts can now reach each other")

    def setup_static_arp(self):
        """Set static ARP entries on all hosts so cross-switch pings work
        without needing ARP broadcasts to traverse switches."""
        logger.info("Setting up static ARP entries on all hosts...")
        
        # Collect all host IPs and MACs
        host_info = {}
        for node_config in self.config['network']['nodes']:
            if node_config['type'] == 'host':
                name = node_config['name']
                ip  = node_config.get('ip')
                mac = node_config.get('mac')
                if ip and mac:
                    host_info[name] = {'ip': ip, 'mac': mac}
        
        # Set ARP entry on every host for every other host
        for src_name, src_data in host_info.items():
            src_host = self.nodes.get(src_name)
            if not src_host:
                continue
            for dst_name, dst_data in host_info.items():
                if src_name == dst_name:
                    continue
                src_host.cmd(f"arp -s {dst_data['ip']} {dst_data['mac']}")
            logger.info(f"  {src_name}: ARP entries set for {len(host_info)-1} peers")
        
        logger.info("Static ARP setup complete")

    def run_cli(self):
        """Start Mininet CLI for interactive testing"""
        if not self.net:
            logger.error("Network not started")
            return
        logger.info("Starting Mininet CLI — DB is free for collector now")
        CLI(self.net)
    
    def generate_traffic(self):
        """Generate traffic based on configuration"""
        if 'traffic' not in self.config:
            return
        flows = self.config['traffic'].get('flows', [])
        logger.info(f"Generating {len(flows)} traffic flows...")
        for flow in flows:
            src_name  = flow['src']
            dst_name  = flow['dst']
            protocol  = flow.get('protocol', 'tcp')
            bandwidth = flow.get('bandwidth', '10M')
            duration  = flow.get('duration', 10)
            src = self.nodes.get(src_name)
            dst = self.nodes.get(dst_name)
            if not src or not dst:
                logger.warning(f"Cannot generate flow: {src_name} or {dst_name} not found")
                continue
            logger.info(f"Flow: {src_name} -> {dst_name} ({protocol}, {bandwidth}, {duration}s)")
            if protocol == 'tcp':
                dst.cmd('iperf -s -p 5001 &')
                src.cmd(f'iperf -c {dst.IP()} -p 5001 -t {duration} -b {bandwidth} &')
            elif protocol == 'udp':
                dst.cmd('iperf -s -u -p 5001 &')
                src.cmd(f'iperf -c {dst.IP()} -u -p 5001 -t {duration} -b {bandwidth} &')
    
    def stop_network(self):
        """Stop and cleanup the Mininet network"""
        if self.net:
            logger.info("Stopping network...")
            self.net.stop()
            logger.info("Network stopped")


def main():
    parser = argparse.ArgumentParser(
        description="Build and run Mininet topology from YAML configuration"
    )
    parser.add_argument('--config', default='config/topology_config.yaml')
    parser.add_argument('--db',     default='dtn_network.db')
    parser.add_argument('--no-cli', action='store_true')
    parser.add_argument('--generate-traffic', action='store_true')
    args = parser.parse_args()

    setLogLevel('info')

    # 1. Register topology in DB, then immediately close DB connection
    db = NetworkDatabase(args.db)
    builder = YAMLTopologyBuilder(args.config, db)

    if not builder.load_config():
        return 1

    # Write nodes/links to DB and close the connection RIGHT HERE
    builder.register_topology_in_db()   # <-- DB lock released after this line

    # 2. Build Mininet topology (no DB involvement from here on)
    if not builder.build_topology():
        return 1

    if not builder.start_network():
        return 1

    if args.generate_traffic:
        builder.generate_traffic()

    # 3. CLI runs with DB fully free for collector/sync_engine
    if not args.no_cli:
        builder.run_cli()

    builder.stop_network()
    return 0


if __name__ == '__main__':
    try:
        exit(main())
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user")
        exit(0)