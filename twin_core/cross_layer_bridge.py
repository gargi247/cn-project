import sqlite3, subprocess, yaml, threading, time, logging

logger = logging.getLogger(__name__)

class CrossLayerBridge:
    """
    Bidirectional bridge between RAN twin and Transport twin.
    
    Transport → RAN: Mininet congestion → trigger UE handoff away from congested BS
    RAN → Transport: BS overload → inject tc delay on corresponding Mininet switch
    """

    def __init__(self, db_path, ran_simulator, map_config_path, interval=5):
        self.db_path = db_path
        self.ran = ran_simulator          # your existing RAN twin object
        self.interval = interval
        
        with open(map_config_path) as f:
            cfg = yaml.safe_load(f)
        self.bs_map = cfg['bs_to_mininet']
        self.transport_threshold = cfg.get('transport_congestion_threshold_ms', 50)
        self.sinr_threshold = cfg.get('ran_sinr_threshold_db', 5.0)
        self.load_threshold = cfg.get('ran_load_threshold_pct', 80.0)
        
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("CrossLayerBridge started")

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                self._transport_to_ran()
                self._ran_to_transport()
            except Exception as e:
                logger.error(f"Bridge loop error: {e}")
            time.sleep(self.interval)

    # ── Direction 1: Transport congestion → RAN handoff ──────────────────────
    def _transport_to_ran(self):
        """
        If a Mininet switch is congested, tell RAN to move UEs
        away from the corresponding base station.
        """
        congested_switches = self._get_congested_switches()
        for bs_id, info in self.bs_map.items():
            if info['switch'] in congested_switches:
                logger.info(f"Transport congestion on {info['switch']} → "
                            f"triggering handoff away from {bs_id}")
                self.ran.trigger_load_reduction(bs_id)   # your existing method

    def _get_congested_switches(self):
        """Read recent metrics from DB, return set of congested switch names."""
        congested = set()
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            cur = conn.cursor()
            # Get avg latency per host-pair over last 3 samples
            cur.execute("""
                SELECT node_src, node_dst, AVG(latency_ms) as avg_lat
                FROM network_metrics
                WHERE timestamp > datetime('now', '-15 seconds')
                GROUP BY node_src, node_dst
                HAVING avg_lat > ?
            """, (self.transport_threshold,))
            congested_pairs = cur.fetchall()
            conn.close()
            
            # Map congested host pairs back to switches
            host_to_switch = {}
            for bs_id, info in self.bs_map.items():
                for h in info['hosts']:
                    host_to_switch[h] = info['switch']
            
            for src, dst, _ in congested_pairs:
                if src in host_to_switch:
                    congested.add(host_to_switch[src])
        except Exception as e:
            logger.error(f"DB read error in bridge: {e}")
        return congested

    # ── Direction 2: RAN overload → Transport throttling ─────────────────────
    def _ran_to_transport(self):
        """
        If a BS is overloaded (low SINR or high UE load),
        apply tc delay on its corresponding Mininet interface
        to signal/simulate the degraded wireless backhaul.
        """
        for bs_id, info in self.bs_map.items():
            bs_metrics = self.ran.get_bs_metrics(bs_id)   # implement in your RAN twin
            if bs_metrics is None:
                continue
            
            sinr = bs_metrics.get('avg_sinr', 99)
            load = bs_metrics.get('load_pct', 0)
            iface = info['inject_interface']
            
            if sinr < self.sinr_threshold or load > self.load_threshold:
                logger.info(f"RAN overload on {bs_id} (SINR={sinr:.1f}, load={load:.0f}%) "
                            f"→ applying tc on {iface}")
                self._apply_tc_delay(iface, delay_ms=200, loss_pct=5)
            else:
                self._clear_tc_delay(iface)

    def _apply_tc_delay(self, iface, delay_ms=200, loss_pct=5):
        try:
            # Remove existing rule first (ignore error if none exists)
            subprocess.run(["sudo", "tc", "qdisc", "del", "dev", iface, "root"],
                           capture_output=True)
            subprocess.run(["sudo", "tc", "qdisc", "add", "dev", iface, "root",
                            "netem", "delay", f"{delay_ms}ms", "loss", f"{loss_pct}%"],
                           check=True, capture_output=True)
        except Exception as e:
            logger.warning(f"tc apply failed on {iface}: {e}")

    def _clear_tc_delay(self, iface):
        try:
            subprocess.run(["sudo", "tc", "qdisc", "del", "dev", iface, "root"],
                           capture_output=True)   # silently ignore if no rule
        except Exception:
            pass
