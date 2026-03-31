"""
Closed-Loop Control Engine - Digital Twin Network Phase 2
Ties together: congestion detection → route optimization → OpenFlow control
Implements the full closed-loop architecture from ITU-T Y.3090.

Run with: sudo python3 twin_core/control_loop.py
(sudo needed for nsenter and ovs-ofctl)
"""

import time
import logging
import argparse
import threading
import signal
import sys
import os
from datetime import datetime
from typing import Dict, List

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_layer.storage import NetworkDatabase
from twin_core.congestion_detector import CongestionDetector
from twin_core.route_optimizer import RouteOptimizer
from twin_core.openflow_controller import OpenFlowController

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class ClosedLoopController:
    """
    The central closed-loop control engine.
    
    Loop:
    1. Detect congestion (CongestionDetector)
    2. Compute optimal routes (RouteOptimizer / Dijkstra)
    3. Push OpenFlow rules (OpenFlowController)
    4. Verify improvement (check metrics after reroute)
    5. Write results to DB for dashboard display
    """

    def __init__(self, db: NetworkDatabase,
                 loop_interval: float = 5.0,
                 latency_threshold: float = 50.0,
                 loss_threshold: float = 5.0):

        self.db = db
        self.loop_interval = loop_interval
        self.running = False
        self.loop_count = 0

        # Phase 2 components
        self.detector   = CongestionDetector(db, latency_threshold, loss_threshold)
        self.optimizer  = RouteOptimizer(db)
        self.controller = OpenFlowController(db)

        # State tracking
        self.active_congestion: Dict[str, Dict] = {}
        self.reroute_decisions: List[Dict] = []
        self.stats = {
            'loops': 0,
            'congestion_events': 0,
            'reroutes_applied': 0,
            'recoveries': 0
        }

    def run_one_cycle(self):
        """Execute one full closed-loop cycle."""
        cycle_start = time.time()
        self.loop_count += 1
        logger.info(f"\n{'='*50}")
        logger.info(f"Control Loop Cycle #{self.loop_count}")
        logger.info(f"{'='*50}")

        # ── Step 1: Detect congestion ──────────────────────
        congestion_events = self.detector.scan_all_links()
        self.stats['congestion_events'] += len(congestion_events)

        # Find newly congested links (not seen before)
        new_congestion = [
            e for e in congestion_events
            if e['link_id'] not in self.active_congestion
        ]

        # Find recovered links (were congested, now healthy)
        current_ids = {e['link_id'] for e in congestion_events}
        recovered = [
            lid for lid in list(self.active_congestion.keys())
            if lid not in current_ids
        ]

        # Update active congestion state
        self.active_congestion = {e['link_id']: e for e in congestion_events}

        # ── Step 2: Handle recoveries ──────────────────────
        for link_id in recovered:
            src, dst = link_id.split('-', 1)
            logger.info(f"✓ RECOVERED: {link_id} - removing reroute rules")
            self.controller.remove_reroute_rules(src, dst)
            self.optimizer.clear_reroute(link_id)
            self.stats['recoveries'] += 1

        # ── Step 3: Compute reroutes for new congestion ────
        if new_congestion:
            logger.info(f"Computing reroutes for {len(new_congestion)} new congested links...")
            reroute_decisions = self.optimizer.compute_rerouting(new_congestion)

            # ── Step 4: Apply OpenFlow rules ───────────────
            for decision in reroute_decisions:
                if decision['action'] == 'reroute':
                    success = self.controller.install_reroute_rule(decision)
                    if success:
                        self.stats['reroutes_applied'] += 1
                        logger.info(
                            f"✓ REROUTED: "
                            f"{decision['congestion_event']['src']}→"
                            f"{decision['congestion_event']['dst']} via "
                            f"{' → '.join(decision['route']['optimal_path'])}"
                        )
                    else:
                        logger.warning("OpenFlow rule installation failed, "
                                      "twin state updated but physical network unchanged")
                else:
                    logger.warning(
                        f"No alternate path for "
                        f"{decision['congestion_event']['src']}→"
                        f"{decision['congestion_event']['dst']}"
                    )

            self.reroute_decisions.extend(reroute_decisions)
        else:
            if congestion_events:
                logger.info("Congestion ongoing, rules already applied")
            else:
                logger.info("✓ All links healthy, no action needed")

        # ── Step 5: Write control state to DB ─────────────
        self._persist_control_state()

        self.stats['loops'] += 1
        elapsed = time.time() - cycle_start
        logger.info(f"Cycle #{self.loop_count} complete in {elapsed:.2f}s | "
                   f"Stats: {self.stats}")

    def _persist_control_state(self):
        """Write current control state summary to DB events table."""
        try:
            summary = (
                f"Loop #{self.loop_count} | "
                f"Congested: {len(self.active_congestion)} links | "
                f"Reroutes: {self.stats['reroutes_applied']} applied"
            )
            if self.active_congestion:
                self.db.insert_event(
                    event_type='anomaly',
                    severity='warning' if len(self.active_congestion) < 3 else 'critical',
                    node_name=None,
                    description=summary
                )
        except Exception as e:
            logger.debug(f"Could not persist state: {e}")

    def start(self):
        """Start the closed-loop control in a background thread."""
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        logger.info(f"Closed-loop controller started (interval: {self.loop_interval}s)")

    def stop(self):
        """Stop the control loop."""
        self.running = False
        logger.info(f"Controller stopped. Final stats: {self.stats}")

    def _loop(self):
        while self.running:
            try:
                self.run_one_cycle()
            except Exception as e:
                logger.error(f"Error in control loop: {e}", exc_info=True)
            time.sleep(self.loop_interval)

    def get_status(self) -> Dict:
        """Return current status for dashboard/API."""
        return {
            'running': self.running,
            'loop_count': self.loop_count,
            'active_congestion': list(self.active_congestion.values()),
            'active_reroutes': self.optimizer.get_active_reroutes(),
            'installed_of_rules': self.controller.get_installed_rules(),
            'stats': self.stats,
            'timestamp': datetime.now().isoformat()
        }

    def inject_demo_congestion(self, host: str = 'h1',
                                delay_ms: int = 150,
                                loss_pct: float = 30.0):
        """Inject congestion for demo purposes."""
        logger.info(f"Demo: Injecting congestion on {host}...")
        success = self.detector.inject_congestion(host, None, delay_ms, loss_pct)
        return success

    def clear_demo_congestion(self, host: str = 'h1'):
        """Clear injected congestion."""
        return self.detector.clear_congestion(host)


def signal_handler(signum, frame):
    logger.info("\nShutting down...")
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(
        description="DTN Phase 2 - Closed-Loop Control Engine"
    )
    parser.add_argument('--db',        default='dtn_network.db')
    parser.add_argument('--interval',  type=float, default=5.0,
                        help='Control loop interval in seconds')
    parser.add_argument('--latency',   type=float, default=50.0,
                        help='Latency congestion threshold (ms)')
    parser.add_argument('--loss',      type=float, default=5.0,
                        help='Packet loss congestion threshold (%%)')
    parser.add_argument('--demo',      action='store_true',
                        help='Inject demo congestion after 10s then clear after 30s')
    parser.add_argument('--inject-host', default='h1',
                        help='Host to inject congestion on (demo mode)')
    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal_handler)

    logger.info("="*60)
    logger.info("Digital Twin Network - Phase 2 Closed-Loop Controller")
    logger.info("="*60)
    logger.info(f"Thresholds: latency>{args.latency}ms, loss>{args.loss}%")
    logger.info(f"Loop interval: {args.interval}s")

    db = NetworkDatabase(args.db)
    controller = ClosedLoopController(
        db,
        loop_interval=args.interval,
        latency_threshold=args.latency,
        loss_threshold=args.loss
    )

    controller.start()

    if args.demo:
        logger.info("\n[DEMO MODE] Will inject congestion in 10 seconds...")
        time.sleep(10)
        controller.inject_demo_congestion(args.inject_host, delay_ms=150, loss_pct=30.0)
        logger.info("[DEMO] Congestion injected. Waiting 30s for detection + reroute...")
        time.sleep(30)
        logger.info("[DEMO] Clearing congestion to show recovery...")
        controller.clear_demo_congestion(args.inject_host)
        time.sleep(15)
        logger.info("[DEMO] Demo complete.")
    else:
        logger.info("Running (Ctrl+C to stop)...")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    controller.stop()
    db.close()

    print("\nFinal Status:")
    import json
    print(json.dumps(controller.stats, indent=2))


if __name__ == '__main__':
    main()
