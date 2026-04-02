"""
OpenFlow Controller for Digital Twin Network - Phase 2
Pushes flow rules to physical OVS switches based on twin decisions.
Implements the closed-loop control from ITU-T Y.3090.
"""

import subprocess
import logging
import re
from typing import Dict, List, Optional
from datetime import datetime
import sys, os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_layer.storage import NetworkDatabase

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# Topology knowledge: which switch each host connects to
# Derived from topology_config.yaml
HOST_TO_SWITCH = {
    'h1': 's1', 'h2': 's1',
    'h3': 's2', 'h4': 's2',
    'h5': 's3', 'h6': 's3',
}

# Switch port mappings (auto-discovered or hardcoded from ovs-vsctl show)
# Format: {switch: {neighbor: port_number}}
SWITCH_PORTS: Dict[str, Dict[str, int]] = {}


def discover_topology() -> Dict[str, Dict[str, int]]:
    # Hardcoded port map derived from `ip link show` output on the running topology.
    # Format: {switch: {neighbor: port_number}}
    # s1-eth3@s2-eth3 → s1 port 3 faces s2
    # s1-eth4@s3-eth4 → s1 port 4 faces s3
    # s2-eth4@s3-eth3 → s2 port 4 faces s3  (and s3 port 3 faces s2)
    SWITCH_PORTS: Dict[str, Dict[str, int]] = {
        's1': {'h1': 1, 'h2': 2, 's2': 3, 's3': 4},
        's2': {'h3': 1, 'h4': 2, 's1': 3, 's3': 4},
        's3': {'h5': 1, 'h6': 2, 's2': 3, 's1': 4},
    }

# Inter-switch interfaces to use for tc netem congestion injection
# Format: {(switch, neighbor): interface_name}
    INTER_SWITCH_IFACES = {
        ('s1', 's2'): 's1-eth3',
        ('s2', 's1'): 's2-eth3',
        ('s1', 's3'): 's1-eth4',
        ('s3', 's1'): 's3-eth4',
        ('s2', 's3'): 's2-eth4',
        ('s3', 's2'): 's3-eth3',
    }


def discover_topology() -> Dict[str, Dict[str, int]]:
    """Return the hardcoded port map (already verified from ip link show)."""
    return SWITCH_PORTS

def run_ofctl(switch: str, command: str) -> bool:
    """Run an ovs-ofctl command on a switch."""
    try:
        result = subprocess.run(
            f"ovs-ofctl {command} {switch}",
            shell=True, capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            logger.error(f"ovs-ofctl error on {switch}: {result.stderr}")
            return False
        return True
    except Exception as e:
        logger.error(f"ovs-ofctl exception: {e}")
        return False


def get_host_ip(host: str) -> str:
    """Get IP from host name (h1 → 10.0.0.1)."""
    match = re.match(r'h(\d+)', host)
    return f"10.0.0.{match.group(1)}" if match else None


def get_host_mac(host: str) -> str:
    """Get MAC from host name (h1 → 00:00:00:00:00:01)."""
    match = re.match(r'h(\d+)', host)
    if match:
        n = int(match.group(1))
        return f"00:00:00:00:00:{n:02x}"
    return None


class OpenFlowController:
    """
    Pushes OpenFlow rules to OVS switches to implement
    routing decisions computed by the digital twin.
    """

    def __init__(self, db: NetworkDatabase):
        self.db = db
        self.installed_rules: List[Dict] = []
        self.switch_ports = discover_topology()
        logger.info(f"Discovered switches: {list(self.switch_ports.keys())}")

    def install_flood_baseline(self):
        """Re-install flood rules as baseline (safe default)."""
        switches = ['s1', 's2', 's3']
        for sw in switches:
            run_ofctl(sw, "add-flow")
            subprocess.run(
                f"ovs-ofctl add-flow {sw} 'priority=1,action=flood'",
                shell=True, capture_output=True
            )
        logger.info("Baseline flood rules installed on all switches")

    def get_output_port(self, switch: str, interface_name: str) -> Optional[int]:
        """Look up port number for an interface on a switch."""
        sw_ports = self.switch_ports.get(switch, {})
        return sw_ports.get(interface_name)

    def install_reroute_rule(self, reroute_decision: Dict) -> bool:
        """
        Install OpenFlow rules to implement a rerouting decision.
        
        For a path like h1 → s1 → s2 → h3, we install:
        - On s1: forward packets destined for h3's IP out the port toward s2
        - On s2: forward packets destined for h3's IP out the port toward h3
        
        Args:
            reroute_decision: Dict from RouteOptimizer.compute_rerouting()
        """
        route = reroute_decision.get('route', {})
        path = route.get('optimal_path', [])
        src_host = reroute_decision['congestion_event']['src']
        dst_host = reroute_decision['congestion_event']['dst']

        if len(path) < 2:
            logger.warning(f"Path too short to install rules: {path}")
            return False

        dst_ip = get_host_ip(dst_host)
        dst_mac = get_host_mac(dst_host)
        src_ip = get_host_ip(src_host)

        if not dst_ip:
            logger.error(f"Cannot resolve IP for {dst_host}")
            return False

        logger.info(f"Installing reroute rules for {src_host}→{dst_host}: {' → '.join(path)}")

        # Install rules on each switch in the path
        success = True
        for i, node in enumerate(path):
            if not node.startswith('s'):
                continue  # skip hosts

            # Find the next hop in the path
            next_hop = path[i + 1] if i + 1 < len(path) else None
            if not next_hop:
                continue

            # Determine output port: the interface facing next_hop
            # In Mininet, s1-eth1 connects to first linked node, etc.
            # We use ovs-ofctl dump-ports to find the right port
            out_port = self._find_port_to_neighbor(node, next_hop)

            if out_port is None:
                logger.warning(f"Cannot find port from {node} to {next_hop}, using flood")
                out_port = 'flood'

            # Install high-priority rule: match dst IP, output to specific port
            flow_rule = (
                f"priority=100,"
                f"ip,nw_dst={dst_ip},"
                f"action=output:{out_port}"
            )

            cmd = f"ovs-ofctl add-flow {node} '{flow_rule}'"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)

            if result.returncode == 0:
                logger.info(f"  ✓ {node}: dst={dst_ip} → port {out_port}")
                self.installed_rules.append({
                    'switch': node,
                    'rule': flow_rule,
                    'for_link': f"{src_host}-{dst_host}",
                    'installed_at': datetime.now().isoformat()
                })
            else:
                logger.error(f"  ✗ {node}: {result.stderr}")
                success = False

        # Also install reverse path rules
        for i, node in enumerate(reversed(path)):
            if not node.startswith('s'):
                continue
            idx = len(path) - 1 - i
            prev_hop = path[idx - 1] if idx - 1 >= 0 else None
            if not prev_hop:
                continue

            out_port = self._find_port_to_neighbor(node, prev_hop)
            if out_port is None:
                out_port = 'flood'

            flow_rule = (
                f"priority=100,"
                f"ip,nw_dst={src_ip},"
                f"action=output:{out_port}"
            )
            subprocess.run(
                f"ovs-ofctl add-flow {node} '{flow_rule}'",
                shell=True, capture_output=True
            )

        if success:
            self.db.insert_event(
                event_type='configuration',
                severity='info',
                node_name=src_host,
                description=f"OpenFlow reroute installed: {' → '.join(path)}"
            )
        return success

    def _find_port_to_neighbor(self, switch: str, neighbor: str) -> Optional[int]:
        """
        Look up the OVS port number on 'switch' that connects to 'neighbor'.
        Uses the verified SWITCH_PORTS map built from ip link show output.
        """
        port = SWITCH_PORTS.get(switch, {}).get(neighbor)
        if port is None:
            logger.warning(f"No port mapping found for {switch}→{neighbor}")
        return port

    def _get_neighbor_facing_mac(self, neighbor: str, switch: str) -> Optional[str]:
        """Get the MAC address of the interface on 'neighbor' that faces 'switch'."""
        try:
            # For hosts: fixed MAC scheme h1→00:00:00:00:00:01
            if neighbor.startswith('h'):
                return get_host_mac(neighbor)
            # For switches: read their OVS datapath MAC
            result = subprocess.run(
                ['ovs-vsctl', 'get', 'bridge', neighbor, 'other-config:hwaddr'],
                capture_output=True, text=True, timeout=5
            )
            mac = result.stdout.strip().strip('"')
            return mac if mac else None
        except Exception:
            return None

    def _build_port_map_via_vsctl(self, switch: str) -> Dict[str, int]:
        """
        Build {neighbor_name: port_num} for a switch by inspecting
        ovs-vsctl interface names and matching them to known topology.
        """
        port_map = {}
        try:
            result = subprocess.run(
                ['ovs-ofctl', 'show', switch],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                m = re.match(r'\s+(\d+)\((\S+)\):', line)
                if m:
                    port_num = int(m.group(1))
                    port_name = m.group(2)  # e.g. s1-eth1, s1-eth3
                    # Match known hosts by their fixed naming
                    for host in HOST_TO_SWITCH:
                        sw = HOST_TO_SWITCH[host]
                        if sw == switch and port_name.startswith(host):
                            port_map[host] = port_num
            # For inter-switch links we still can't resolve by name alone,
            # but at least hosts are now correctly mapped.
        except Exception as e:
            logger.error(f"vsctl port map error: {e}")
        return port_map

    def remove_reroute_rules(self, src_host: str, dst_host: str):
        """Remove reroute rules and restore flood baseline."""
        dst_ip = get_host_ip(dst_host)
        src_ip = get_host_ip(src_host)

        for sw in ['s1', 's2', 's3']:
            if dst_ip:
                subprocess.run(
                    f"ovs-ofctl del-flows {sw} 'ip,nw_dst={dst_ip}'",
                    shell=True, capture_output=True
                )
            if src_ip:
                subprocess.run(
                    f"ovs-ofctl del-flows {sw} 'ip,nw_dst={src_ip}'",
                    shell=True, capture_output=True
                )

        # Remove from installed rules list
        self.installed_rules = [
            r for r in self.installed_rules
            if r['for_link'] != f"{src_host}-{dst_host}"
        ]

        logger.info(f"Removed reroute rules for {src_host}→{dst_host}")
        self.db.insert_event(
            event_type='recovery',
            severity='info',
            node_name=src_host,
            description=f"Reroute rules removed, restored to flood baseline"
        )

    def get_installed_rules(self) -> List[Dict]:
        return self.installed_rules

    def dump_flows(self, switch: str) -> str:
        """Dump current flow table for a switch (for debugging)."""
        try:
            result = subprocess.run(
                ['ovs-ofctl', 'dump-flows', switch],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout
        except Exception as e:
            return f"Error: {e}"
