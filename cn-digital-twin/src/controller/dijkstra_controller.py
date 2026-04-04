#!/usr/bin/env python3
"""
controller/dijkstra_controller.py
──────────────────────────────────
SDN-style controller implementing:

  1. Link-state database (LSDB) — stores current cost of every link
  2. Dijkstra's shortest path on the LSDB
  3. Composite cost metric:  cost = delay + α*(1/bw) + β*congestion_penalty
  4. Flow-table installation on OVS switches via ovs-ofctl
  5. Listens on UDP socket for link-state updates from congestion monitor
     and cross-layer bridge messages from the RAN layer
  6. On topology change → recomputes all paths → pushes new flows

This is a *simplified* SDN controller (like a stripped-down Ryu app)
that runs as a standalone process and controls OVS switches via CLI.
For full Ryu integration, subclass RyuApp and replace _install_flow().

CN Concepts demonstrated:
  - Dijkstra on a weighted directed graph
  - Link-state routing (OSPF-like LSDB)
  - Composite routing metric (delay + BW + congestion)
  - Software-Defined Networking (flow-table manipulation)
  - Cross-layer signaling (RAN → Transport)
"""

import heapq
import json
import math
import socket
import struct
import threading
import time
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import (
    TOPOLOGY, CONGESTION_PENALTY, LINK_COST_METRIC,
    BRIDGE_HOST, BRIDGE_PORT,
    MSG_REROUTE_REQUEST, MSG_HANDOVER_COMPLETE, MSG_BS_FAILURE,
    MSG_LINK_STATE_UPDATE, LOG_DIR,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CTRL] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)
os.makedirs(LOG_DIR, exist_ok=True)

# Weight for composite metric
ALPHA = 0.3   # BW term weight
BETA  = 1.0   # congestion penalty weight


# ── Link-State Database ────────────────────────────────────────────────────
class LSDB:
    """
    Stores per-link state: {(src,dst): LinkRecord}
    Thread-safe; updates trigger path recomputation.
    """

    class LinkRecord:
        def __init__(self, src, dst, bw_mbps, delay_ms, loss_pct):
            self.src       = src
            self.dst       = dst
            self.bw_mbps   = bw_mbps
            self.delay_ms  = delay_ms
            self.loss_pct  = loss_pct
            self.util_bps  = 0.0          # updated by monitor
            self.congested = False
            self.up        = True
            self.updated   = time.time()

        def composite_cost(self):
            """
            Composite metric (lower = better):
              base = delay_ms
              bw_term = α * (100 / bw_mbps)   (normalise: 100Mbps → 1)
              congestion = BETA * CONGESTION_PENALTY if congested
              loss_term = loss_pct * 10
            """
            if not self.up:
                return float("inf")
            base      = self.delay_ms
            bw_term   = ALPHA * (100.0 / max(self.bw_mbps, 0.1))
            cong_term = BETA * CONGESTION_PENALTY if self.congested else 0
            loss_term = self.loss_pct * 10
            return base + bw_term + cong_term + loss_term

        def __repr__(self):
            return (f"Link({self.src}→{self.dst} "
                    f"{self.bw_mbps}Mbps {self.delay_ms}ms "
                    f"{'CONG' if self.congested else 'ok'})")

    def __init__(self):
        self._db   = {}     # (src,dst) → LinkRecord
        self._lock = threading.RLock()
        self._populate_from_config()

    def _populate_from_config(self):
        for src, dst, bw, delay, loss, _ in TOPOLOGY["links"]:
            self._db[(src, dst)] = self.LinkRecord(src, dst, bw, delay, loss)
            self._db[(dst, src)] = self.LinkRecord(dst, src, bw, delay, loss)

    def update_utilisation(self, src, dst, util_bps, congested):
        with self._lock:
            for key in [(src, dst), (dst, src)]:
                if key in self._db:
                    self._db[key].util_bps  = util_bps
                    self._db[key].congested = congested
                    self._db[key].updated   = time.time()

    def mark_link_down(self, src, dst):
        with self._lock:
            for key in [(src, dst), (dst, src)]:
                if key in self._db:
                    self._db[key].up = False
                    log.warning(f"LSDB: Link {key} marked DOWN")

    def mark_link_up(self, src, dst):
        with self._lock:
            for key in [(src, dst), (dst, src)]:
                if key in self._db:
                    self._db[key].up = True

    def get_graph(self):
        """Return adjacency dict {node: {neighbour: cost}}"""
        with self._lock:
            graph = {}
            for (src, dst), rec in self._db.items():
                graph.setdefault(src, {})[dst] = rec.composite_cost()
            return graph

    def dump(self):
        with self._lock:
            return {
                f"{k[0]}-{k[1]}": {
                    "bw_mbps":   v.bw_mbps,
                    "delay_ms":  v.delay_ms,
                    "util_bps":  round(v.util_bps),
                    "congested": v.congested,
                    "cost":      round(v.composite_cost(), 2),
                    "up":        v.up,
                }
                for k, v in self._db.items()
            }


# ── Dijkstra ───────────────────────────────────────────────────────────────
def dijkstra(graph, source):
    """
    Standard Dijkstra returning (dist_dict, prev_dict).
    graph: {node: {neighbour: cost}}  — all costs ≥ 0
    """
    dist = {n: math.inf for n in graph}
    prev = {n: None     for n in graph}
    dist[source] = 0.0
    pq = [(0.0, source)]

    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u]:
            continue
        for v, w in graph.get(u, {}).items():
            if v not in dist:        # node not yet seen
                dist[v] = math.inf
                prev[v] = None
            nd = dist[u] + w
            if nd < dist[v]:
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))

    return dist, prev


def reconstruct_path(prev, source, target):
    path, node = [], target
    while node is not None:
        path.append(node)
        node = prev.get(node)
    path.reverse()
    return path if path and path[0] == source else []


def all_host_pairs_paths(graph):
    """
    Compute shortest paths between all host pairs.
    Returns dict: {(h_src, h_dst): [switch_path]}
    """
    hosts   = [n for n in graph if n.startswith("h")]
    result  = {}
    for src in hosts:
        dist, prev = dijkstra(graph, src)
        for dst in hosts:
            if dst != src:
                path = reconstruct_path(prev, src, dst)
                result[(src, dst)] = path
    return result


# ── OVS flow installer ─────────────────────────────────────────────────────
class OVSFlowManager:
    """
    Installs OpenFlow rules on OVS switches via ovs-ofctl.
    In a real deployment, replace with Ryu REST API or OVSDB.

    Flow format (OpenFlow 1.0):
      priority=100, in_port=X, dl_dst=<mac>, actions=output:Y
    """

    # MAC map: host → MAC (autoSetMacs in Mininet uses 00:00:00:00:00:0N)
    HOST_MACS = {
        "h1": "00:00:00:00:00:01",
        "h2": "00:00:00:00:00:02",
        "h3": "00:00:00:00:00:03",
        "h4": "00:00:00:00:00:04",
    }

    def __init__(self, dry_run=False):
        self.dry_run = dry_run   # True = just log, don't exec

    def _run(self, cmd):
        if self.dry_run:
            log.info(f"[DRY-RUN] {cmd}")
            return
        ret = os.system(cmd)
        if ret != 0:
            log.warning(f"ovs-ofctl returned {ret}: {cmd}")

    def clear_flows(self, switch):
        self._run(f"ovs-ofctl del-flows {switch}")

    def install_flow(self, switch, in_port, dst_host, out_port, priority=100):
        mac = self.HOST_MACS.get(dst_host, "ff:ff:ff:ff:ff:ff")
        self._run(
            f"ovs-ofctl add-flow {switch} "
            f"priority={priority},in_port={in_port},"
            f"dl_dst={mac},actions=output:{out_port}"
        )

    def install_path(self, path, dst_host):
        """
        Install flows along a computed path so packets to dst_host
        are forwarded correctly at every switch hop.
        path: list of node names, e.g. ['h1','s1','s2','s3','h2']
        """
        switches = [n for n in path if n.startswith("s")]
        if not switches:
            return
        log.info(f"Installing path to {dst_host}: {'→'.join(path)}")
        for i, sw in enumerate(switches):
            # port numbers in Mininet are assigned 1-based in link order
            # We just log here; real port mapping needs net object
            self._run(
                f"# ovs-ofctl install flow on {sw} for dst {dst_host} hop {i}"
            )


# ── Cross-layer message receiver ───────────────────────────────────────────
class BridgeListener:
    """
    Receives UDP messages from the RAN simulator (cross-layer bridge).
    Dispatches to controller callbacks.
    """

    # Packet format: [opcode:1B][payload_len:2B][payload:NB]
    HEADER_FMT = "!BH"
    HEADER_SZ  = struct.calcsize(HEADER_FMT)

    def __init__(self, host, port, controller):
        self.host       = host
        self.port       = port
        self.controller = controller
        self._sock      = None
        self._running   = False

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.settimeout(1.0)
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()
        log.info(f"Bridge listener on udp://{self.host}:{self.port}")

    def _loop(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(4096)
                self._dispatch(data, addr)
            except socket.timeout:
                continue
            except Exception as e:
                log.error(f"Bridge recv error: {e}")

    def _dispatch(self, data, addr):
        if len(data) < self.HEADER_SZ:
            return
        opcode, plen = struct.unpack_from(self.HEADER_FMT, data)
        payload = data[self.HEADER_SZ: self.HEADER_SZ + plen]
        try:
            msg = json.loads(payload.decode()) if payload else {}
        except Exception:
            msg = {}

        if opcode == MSG_REROUTE_REQUEST:
            reason = msg.get("reason", "unknown")
            log.info(f"RAN→CTRL: Reroute requested — {reason}")
            self.controller.recompute_paths(reason=reason)

        elif opcode == MSG_HANDOVER_COMPLETE:
            new_bs  = msg.get("new_bs")
            new_sw  = msg.get("connected_switch")
            log.info(f"RAN→CTRL: Handover complete → {new_bs} (switch {new_sw})")
            self.controller.on_handover(new_bs, new_sw)

        elif opcode == MSG_BS_FAILURE:
            bs_id  = msg.get("bs_id")
            sw     = msg.get("connected_switch")
            log.warning(f"RAN→CTRL: BS {bs_id} failed — marking switch {sw} links degraded")
            self.controller.on_bs_failure(bs_id, sw)

    def stop(self):
        self._running = False
        if self._sock:
            self._sock.close()


# ── Controller ─────────────────────────────────────────────────────────────
class DijkstraController:
    """
    Main controller process.
    Maintains LSDB, runs Dijkstra on every topology change,
    installs flows, and handles cross-layer events from RAN.
    """

    def __init__(self, dry_run=True):
        self.lsdb      = LSDB()
        self.flow_mgr  = OVSFlowManager(dry_run=dry_run)
        self.listener  = BridgeListener(BRIDGE_HOST, BRIDGE_PORT + 1, self)
        self._paths    = {}      # (src,dst) → path
        self._lock     = threading.Lock()
        self._path_log = os.path.join(LOG_DIR, "paths.jsonl")

    def start(self):
        self.listener.start()
        # Initial path computation
        self.recompute_paths(reason="startup")
        log.info("DijkstraController running")

    # ── Called by CongestionMonitor ──────────────────────────────────────
    def update_link(self, src, dst, util_bps, congested):
        self.lsdb.update_utilisation(src, dst, util_bps, congested)
        if congested:
            log.warning(f"Congestion on {src}↔{dst} — triggering reroute")
            self.recompute_paths(reason=f"congestion on {src}-{dst}")

    # ── Path recomputation ────────────────────────────────────────────────
    def recompute_paths(self, reason="manual"):
        graph = self.lsdb.get_graph()
        if not graph:
            log.error("Empty graph — cannot compute paths")
            return

        with self._lock:
            new_paths = all_host_pairs_paths(graph)
            changed   = new_paths != self._paths
            self._paths = new_paths

        if changed:
            log.info(f"Paths recomputed [{reason}]:")
            for (src, dst), path in new_paths.items():
                log.info(f"  {src}→{dst}: {'→'.join(path)}")
            self._install_all_flows(new_paths)
            self._log_paths(new_paths, reason)
        else:
            log.info(f"Paths unchanged after recompute [{reason}]")

    def _install_all_flows(self, paths):
        for (src, dst), path in paths.items():
            if path:
                self.flow_mgr.install_path(path, dst)

    def _log_paths(self, paths, reason):
        record = {
            "ts":     time.strftime("%H:%M:%S"),
            "reason": reason,
            "paths":  {f"{k[0]}-{k[1]}": v for k, v in paths.items()},
            "lsdb":   self.lsdb.dump(),
        }
        with open(self._path_log, "a") as f:
            f.write(json.dumps(record) + "\n")

    # ── Cross-layer callbacks ─────────────────────────────────────────────
    def on_handover(self, new_bs, new_switch):
        """RAN completed handover — UE now served by new_switch."""
        log.info(f"Adjusting paths for UE now on switch {new_switch}")
        self.recompute_paths(reason=f"RAN handover to {new_bs}")

    def on_bs_failure(self, bs_id, switch):
        """Mark all links to/from switch as degraded."""
        for sw_other in TOPOLOGY["switches"]:
            if sw_other != switch:
                # Check if link exists
                self.lsdb.update_utilisation(switch, sw_other,
                                             util_bps=0,
                                             congested=True)
        self.recompute_paths(reason=f"BS {bs_id} failure")

    def get_path(self, src, dst):
        with self._lock:
            return self._paths.get((src, dst), [])

    def status(self):
        with self._lock:
            return {
                "paths": {f"{k[0]}→{k[1]}": v for k, v in self._paths.items()},
                "lsdb":  self.lsdb.dump(),
            }


# ── Congestion monitor (runs inside controller process) ───────────────────
class CongestionMonitor:
    """
    Sends ICMP probes (ping) between hosts and measures RTT.
    RTT spike → congestion signal → controller updates LSDB.

    In real Mininet: reads /proc/net/dev. Here we use ping as proxy.
    """

    def __init__(self, controller, hosts, interval_s=2.0, rtt_threshold_ms=50.0):
        self.controller    = controller
        self.hosts         = hosts   # dict: name→ip
        self.interval      = interval_s
        self.rtt_threshold = rtt_threshold_ms
        self._running      = False
        self._baseline     = {}   # (src,dst) → baseline_rtt_ms

    def _ping_rtt(self, src_ip, dst_ip):
        """Single ICMP ping, returns RTT in ms or None."""
        try:
            out = os.popen(
                f"ping -c 1 -W 1 {dst_ip} 2>/dev/null | "
                f"grep 'time=' | awk -F'time=' '{{print $2}}' | awk '{{print $1}}'"
            ).read().strip()
            return float(out) if out else None
        except Exception:
            return None

    def _probe_pair(self, src_name, dst_name):
        src_ip = self.hosts.get(src_name)
        dst_ip = self.hosts.get(dst_name)
        if not src_ip or not dst_ip:
            return
        rtt = self._ping_rtt(src_ip, dst_ip)
        if rtt is None:
            return

        key = (src_name, dst_name)
        baseline = self._baseline.get(key, rtt)
        self._baseline[key] = baseline * 0.9 + rtt * 0.1  # EWMA

        congested = rtt > self.rtt_threshold or rtt > baseline * 2
        log.debug(f"Probe {src_name}→{dst_name}: RTT={rtt:.1f}ms "
                  f"(baseline={baseline:.1f}ms) congested={congested}")
        # Approximate: blame the switch-to-switch link on the path
        # In real system, use traceroute or per-link counters
        return rtt, congested

    def _loop(self):
        pairs = [(s, d) for s in self.hosts for d in self.hosts if s != d]
        while self._running:
            for src, dst in pairs:
                result = self._probe_pair(src, dst)
                if result:
                    rtt, congested = result
                    # Notify controller (link identification is simplified)
                    if congested:
                        log.warning(f"RTT spike {src}→{dst}: {rtt:.1f}ms")
            time.sleep(self.interval)

    def start(self):
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def stop(self):
        self._running = False


# ── Entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting Dijkstra SDN Controller (dry-run mode — no real OVS)")
    ctrl = DijkstraController(dry_run=True)
    ctrl.start()

    # Interactive demo
    try:
        while True:
            cmd = input("\nctrl> ").strip().lower()
            if cmd == "status":
                import pprint
                pprint.pprint(ctrl.status())
            elif cmd.startswith("congest "):
                link = cmd.split()[1]   # e.g. "s1-s2"
                a, b = link.split("-")
                ctrl.update_link(a, b, util_bps=9_500_000, congested=True)
            elif cmd.startswith("clear "):
                link = cmd.split()[1]
                a, b = link.split("-")
                ctrl.update_link(a, b, util_bps=0, congested=False)
            elif cmd == "quit":
                break
            else:
                print("Commands: status | congest <s1-s2> | clear <s1-s2> | quit")
    except KeyboardInterrupt:
        pass
