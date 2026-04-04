#!/usr/bin/env python3
"""
mininet_layer/topology.py
─────────────────────────
Real Mininet topology with:
  - TCLink (traffic control) for bandwidth/delay/loss emulation
  - Custom host configuration (IP addressing, routing tables)
  - iperf3-based background traffic for realistic load
  - Per-link utilisation polling via /proc/net/dev
  - Link congestion injection via tc netem + tbf

Run as root:  sudo python3 topology.py
"""

import os
import sys
import time
import threading
import subprocess
import json
import logging

# Mininet imports (must run as root with Mininet installed)
try:
    from mininet.net import Mininet
    from mininet.node import OVSKernelSwitch, RemoteController, DefaultController
    from mininet.link import TCLink
    from mininet.topo import Topo
    from mininet.log import setLogLevel
    from mininet.cli import CLI
    from mininet.util import dumpNetConnections
except ImportError:
    print("[ERROR] Mininet not installed. Run: sudo apt install mininet")
    sys.exit(1)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import TOPOLOGY, CONGESTION_THRESHOLD_BPS, LOG_DIR, METRICS_FILE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MININET] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

os.makedirs(LOG_DIR, exist_ok=True)


# ── Custom Topo ────────────────────────────────────────────────────────────
class DigitalTwinTopo(Topo):
    """
    Topology:

        h1 ── s1 ──(10Mbps,10ms)── s2 ──(10Mbps,10ms)── s3 ── h3
                │                   │                   │
               (5Mbps,20ms)     (5Mbps,15ms)            │
                │                   │                   │
                s5 ────────────────(5Mbps,20ms)─────────┘
                │
               h4
    """

    def build(self):
        switches = {}
        for sw in TOPOLOGY["switches"]:
            switches[sw] = self.addSwitch(sw, cls=OVSKernelSwitch, failMode="standalone")

        hosts = {}
        for h in TOPOLOGY["hosts"]:
            hosts[h] = self.addHost(h)

        for src, dst, bw, delay, loss, queue in TOPOLOGY["links"]:
            self.addLink(
                src, dst,
                cls=TCLink,
                bw=bw,
                delay=f"{delay}ms",
                loss=loss,
                max_queue_size=queue,
            )


# ── Link state monitor ─────────────────────────────────────────────────────
class LinkMonitor:
    """
    Reads /proc/net/dev on each switch's interfaces every PROBE_INTERVAL_S
    to compute bytes/sec → reports congestion when threshold exceeded.

    In real Mininet each veth pair appears as ethX on the switch namespace.
    We use 'ip -s link show dev <intf>' via subprocess (works without OVS tools).
    """

    def __init__(self, net, callback):
        self.net     = net
        self.callback = callback   # fn(link_name, util_bps, congested: bool)
        self._prev   = {}          # intf → bytes at last sample
        self._running = False
        self._thread  = None

    def _bytes_for_intf(self, node, intf_name):
        """Read TX+RX bytes for an interface via ip command in node namespace."""
        try:
            out = node.cmd(f"cat /proc/net/dev | grep {intf_name}")
            # /proc/net/dev line: intf: rx_bytes rx_pkts ... tx_bytes ...
            fields = out.split()
            if len(fields) < 10:
                return 0
            rx = int(fields[1])
            tx = int(fields[9])
            return rx + tx
        except Exception:
            return 0

    def _poll(self, interval=2.0):
        while self._running:
            ts = time.time()
            for src, dst, bw, delay, loss, _ in TOPOLOGY["links"]:
                # Only monitor switch–switch links (interesting for routing)
                if not (src.startswith("s") and dst.startswith("s")):
                    continue
                link_name = f"{src}-{dst}"
                node  = self.net.get(src)
                # Find the interface connecting src→dst
                intf  = None
                for i in node.intfList():
                    if dst in i.name or (i.link and dst in str(i.link)):
                        intf = i
                        break
                if intf is None:
                    continue

                total_bytes = self._bytes_for_intf(node, intf.name)
                prev_bytes  = self._prev.get(link_name, total_bytes)
                delta_bytes = max(0, total_bytes - prev_bytes)
                util_bps    = (delta_bytes * 8) / interval
                self._prev[link_name] = total_bytes

                bw_bps      = bw * 1_000_000
                congested   = util_bps > CONGESTION_THRESHOLD_BPS

                self.callback(link_name, util_bps, bw_bps, congested)

            time.sleep(interval)

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False


# ── Congestion injection ───────────────────────────────────────────────────
class CongestionInjector:
    """
    Uses Linux tc (traffic control) to emulate congestion on a link.
    Two mechanisms:
      1. tbf  (token bucket filter) — hard bandwidth cap
      2. netem — adds extra delay, loss, jitter
    """

    def __init__(self, net):
        self.net = net

    def _get_intf(self, switch_name, neighbour_name):
        sw = self.net.get(switch_name)
        for intf in sw.intfList():
            if neighbour_name in intf.name:
                return sw, intf.name
        return sw, None

    def inject(self, link_str, extra_delay_ms=80, loss_pct=20, bw_limit_mbps=1):
        """
        link_str: "s1-s2"
        Applies tc on BOTH directions of the link.
        """
        a, b = link_str.split("-")
        for src, dst in [(a, b), (b, a)]:
            sw, intf = self._get_intf(src, dst)
            if intf is None:
                log.warning(f"Interface {src}↔{dst} not found")
                continue
            # Remove existing qdiscs
            sw.cmd(f"tc qdisc del dev {intf} root 2>/dev/null")
            # Add tbf (rate limit) with netem (delay + loss) stacked
            sw.cmd(
                f"tc qdisc add dev {intf} root handle 1: tbf "
                f"rate {bw_limit_mbps}mbit burst 32kbit latency 400ms"
            )
            sw.cmd(
                f"tc qdisc add dev {intf} parent 1:1 handle 10: netem "
                f"delay {extra_delay_ms}ms 10ms loss {loss_pct}%"
            )
            log.info(f"Congestion injected on {src}:{intf} "
                     f"(+{extra_delay_ms}ms delay, {loss_pct}% loss, "
                     f"{bw_limit_mbps}Mbps cap)")

    def clear(self, link_str):
        a, b = link_str.split("-")
        for src, dst in [(a, b), (b, a)]:
            sw, intf = self._get_intf(src, dst)
            if intf is None:
                continue
            sw.cmd(f"tc qdisc del dev {intf} root 2>/dev/null")
            log.info(f"Congestion cleared on {src}:{intf}")

    def clear_all(self):
        for sw_name in TOPOLOGY["switches"]:
            sw = self.net.get(sw_name)
            for intf in sw.intfList():
                if intf.name != "lo":
                    sw.cmd(f"tc qdisc del dev {intf.name} root 2>/dev/null")


# ── Traffic generator ──────────────────────────────────────────────────────
class TrafficGenerator:
    """Starts iperf3 servers/clients for realistic background load."""

    def __init__(self, net):
        self.net = net
        self._procs = []

    def start_iperf_server(self, host_name, port=5201):
        h = self.net.get(host_name)
        h.cmd(f"iperf3 -s -p {port} -D")  # -D daemonise
        log.info(f"iperf3 server on {host_name}:{port}")

    def start_iperf_client(self, src_host, dst_ip, duration_s=30,
                           bw="5M", port=5201):
        src = self.net.get(src_host)
        cmd = (f"iperf3 -c {dst_ip} -p {port} -t {duration_s} "
               f"-b {bw} -J > /tmp/iperf_{src_host}.json &")
        src.cmd(cmd)
        log.info(f"iperf3 client {src_host}→{dst_ip} @ {bw}")

    def start_ping_flood(self, src_host, dst_ip, count=1000):
        src = self.net.get(src_host)
        src.cmd(f"ping -f -c {count} {dst_ip} > /tmp/ping_{src_host}.txt &")

    def stop_all(self):
        for sw_name in TOPOLOGY["switches"]:
            sw = self.net.get(sw_name)
            sw.cmd("pkill -f iperf3 2>/dev/null")
        for h_name in TOPOLOGY["hosts"]:
            h = self.net.get(h_name)
            h.cmd("pkill -f iperf3 2>/dev/null")


# ── Metrics logger ─────────────────────────────────────────────────────────
class MetricsLogger:
    def __init__(self, filepath=METRICS_FILE):
        self.filepath = filepath
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w") as f:
            f.write("timestamp,link,util_bps,capacity_bps,util_pct,congested\n")

    def log(self, link, util_bps, capacity_bps, congested):
        ts = time.strftime("%H:%M:%S")
        pct = round(util_bps / capacity_bps * 100, 1) if capacity_bps else 0
        with open(self.filepath, "a") as f:
            f.write(f"{ts},{link},{int(util_bps)},{int(capacity_bps)},{pct},{int(congested)}\n")


# ── Main ───────────────────────────────────────────────────────────────────
def run(use_remote_controller=False, controller_ip="127.0.0.1", controller_port=6633):
    setLogLevel("info")

    topo = DigitalTwinTopo()
    ctrl = RemoteController("c0", ip=controller_ip, port=controller_port) \
           if use_remote_controller else DefaultController

    net = Mininet(
        topo=topo,
        controller=ctrl if use_remote_controller else None,
        switch=OVSKernelSwitch,
        link=TCLink,
        autoSetMacs=True,
        autoStaticArp=True,
    )
    net.start()

    metrics_logger = MetricsLogger()
    injector       = CongestionInjector(net)
    traffic_gen    = TrafficGenerator(net)

    def on_link_update(link, util_bps, capacity_bps, congested):
        metrics_logger.log(link, util_bps, capacity_bps, congested)
        if congested:
            log.warning(f"CONGESTION on {link}: "
                        f"{util_bps/1e6:.1f}/{capacity_bps/1e6:.0f} Mbps")

    monitor = LinkMonitor(net, on_link_update)
    monitor.start()

    # Start iperf servers on h2, h3, h4
    traffic_gen.start_iperf_server("h2")
    traffic_gen.start_iperf_server("h3", port=5202)

    log.info("Mininet topology up. Starting CLI…")
    log.info("  inject_congestion: injector.inject('s1-s2')")
    log.info("  clear_congestion:  injector.clear_all()")

    # Expose injector to CLI via global (crude but effective for demo)
    import builtins
    builtins._injector = injector
    builtins._traffic  = traffic_gen
    builtins._net      = net

    CLI(net)

    monitor.stop()
    traffic_gen.stop_all()
    net.stop()


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("[ERROR] Must run as root: sudo python3 topology.py")
        sys.exit(1)
    run()
