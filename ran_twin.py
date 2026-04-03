"""
ran_twin.py
Wraps the simulator + data_store + optimizer into a clean API
that the unified dashboard and cross-layer bridge can consume.
Drop this in your cn-project root alongside the Mininet files.
"""
'''
import threading
import time
import logging
from typing import List, Dict, Any, Optional
from dataclasses import asdict

logger = logging.getLogger(__name__)


class RANTwin:
    """
    Self-contained RAN Digital Twin.
    Runs the 6G radio simulation + anomaly detection + optimizer
    in a background thread. Exposes clean query methods.
    """

    def __init__(self, num_ues: int = 30, seed: int = 42,
                 optimizer_mode: str = "rule"):
        # Import here so they can sit in same directory
        from simulator import NetworkSimulator
        from data_store import DataStore
        from optimizer import NetworkOptimizer

        self.sim       = NetworkSimulator(num_ues=num_ues, seed=seed)
        self.store     = DataStore()
        self.optimizer = NetworkOptimizer(mode=optimizer_mode)
        self.running   = False
        self._thread   = None
        self.tick_count = 0

        # Cross-layer event queue: handoff events for transport twin
        self._handoff_events: List[Dict] = []
        self._handoff_lock = threading.Lock()

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("RAN twin started")

    def stop(self):
        self.running = False
        logger.info("RAN twin stopped")

    def _loop(self):
        prev_assignments: Dict[str, str] = {}  # ue_id -> bs_id

        while self.running:
            try:
                records = self.sim.tick()
                self.store.ingest(records)
                actions = self.optimizer.step(self.store, self.sim)
                self.tick_count += 1

                # Detect handoffs by comparing BS assignments
                current = {r.ue_id: r.bs_id for r in records}
                for ue_id, new_bs in current.items():
                    old_bs = prev_assignments.get(ue_id)
                    if old_bs and old_bs != new_bs:
                        event = {
                            'ue_id': ue_id,
                            'from_bs': old_bs,
                            'to_bs': new_bs,
                            'timestamp': time.time(),
                            'type': 'handoff'
                        }
                        with self._handoff_lock:
                            self._handoff_events.append(event)
                            # Keep last 50 events
                            self._handoff_events = self._handoff_events[-50:]
                        logger.info(f"HANDOFF: {ue_id} {old_bs} → {new_bs}")

                prev_assignments = current

            except Exception as e:
                logger.error(f"RAN twin loop error: {e}")

            time.sleep(1.0)

    def get_status(self) -> Dict[str, Any]:
        summary = self.store.kpi_summary()
        summary['active_issues'] = self.store.active_anomaly_count()
        summary['tick_count'] = self.tick_count
        return summary

    def get_records(self) -> List[Dict]:
        return [r.to_dict() for r in self.store.latest_records()]

    def get_anomalies(self, n: int = 15) -> List[Dict]:
        return [asdict(a) for a in self.store.recent_anomalies(n)]

    def get_bs_summary(self) -> List[Dict]:
        return self.store.per_bs_summary()

    def get_actions(self, n: int = 15) -> List[Dict]:
        return [a.to_dict() for a in self.optimizer.recent_actions(n)]

    def get_handoff_events(self, n: int = 10) -> List[Dict]:
        with self._handoff_lock:
            return self._handoff_events[-n:]

    def drain_handoff_events(self) -> List[Dict]:
        """Get and clear pending handoff events (for cross-layer bridge)."""
        with self._handoff_lock:
            events = list(self._handoff_events)
            self._handoff_events.clear()
        return events

    def set_optimizer_mode(self, mode: str):
        self.optimizer.set_mode(mode)

    def get_optimizer_info(self) -> Dict:
        return {
            'mode': self.optimizer.mode,
            'ml_stats': self.optimizer.ml_stats()
        }

    def fail_bs(self, bs_id: str):
        self.sim.fail_bs(bs_id)

    def restore_bs(self, bs_id: str):
        self.sim.restore_bs(bs_id)

    def explain_latest(self) -> Dict:
        from llm_explainer import explain_anomaly
        recent = self.store.recent_anomalies(1)
        if not recent:
            return {'explanation': 'No anomalies detected yet.'}
        a = recent[-1]
        return {
            'explanation': explain_anomaly(a),
            'anomaly': {'ue_id': a.ue_id, 'sinr_db': a.sinr_db}
        }

    def what_if(self, question: str) -> str:
        from llm_explainer import what_if_query
        return what_if_query(question, self.store)
'''


"""
ran_twin.py
Wraps the simulator + data_store + optimizer into a clean API
that the unified dashboard and cross-layer bridge can consume.
"""

import threading
import time
import logging
from typing import List, Dict, Any, Optional
from dataclasses import asdict

logger = logging.getLogger(__name__)


class RANTwin:

    def __init__(self, num_ues: int = 30, seed: int = 42,
                 optimizer_mode: str = "rule"):
        from simulator import NetworkSimulator
        from data_store import DataStore
        from optimizer import NetworkOptimizer

        self.sim       = NetworkSimulator(num_ues=num_ues, seed=seed)
        self.store     = DataStore()
        self.optimizer = NetworkOptimizer(mode=optimizer_mode)
        self.running   = False
        self._thread   = None
        self.tick_count = 0

        self._handoff_events: List[Dict] = []
        self._handoff_lock = threading.Lock()


    def trigger_load_reduction(self, bs_id: str):
        """
        Reduce load on a congested base station by moving some UEs away.
        This is a SIMPLE but REAL control action.
        """
        logger.info(f"[RAN CONTROL] Load reduction triggered for {bs_id}")
        try:
            # Get latest UE records
            records = self.store.latest_records()
            # Find UEs connected to this BS
            affected_ues = [r for r in records if r.bs_id == bs_id]
            if not affected_ues:
                logger.info(f"[RAN CONTROL] No UEs on {bs_id}")
                return
            # Move 30% of UEs to a different BS
            num_to_move = max(1, int(0.3 * len(affected_ues)))
            for r in affected_ues[:num_to_move]:
                # Pick a different BS randomly
                new_bs = self.sim.get_random_bs(exclude=bs_id)
                if new_bs:
                    self.sim.force_handoff(r.ue_id, new_bs)
                    logger.info(f"[RAN CONTROL] UE {r.ue_id} moved {bs_id} → {new_bs}")

        except Exception as e:
            logger.error(f"[RAN CONTROL ERROR] {e}")













    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("RAN twin started")

    def stop(self):
        self.running = False
        logger.info("RAN twin stopped")

    def _loop(self):
        prev_assignments: Dict[str, str] = {}

        while self.running:
            try:
                records = self.sim.tick()
                self.store.ingest(records)
                self.optimizer.step(self.store, self.sim)
                self.tick_count += 1

                current = {r.ue_id: r.bs_id for r in records}

                for ue_id, new_bs in current.items():
                    old_bs = prev_assignments.get(ue_id)
                    if old_bs and old_bs != new_bs:
                        event = {
                            'ue_id': ue_id,
                            'from_bs': old_bs,
                            'to_bs': new_bs,
                            'timestamp': time.time(),
                            'type': 'handoff'
                        }
                        with self._handoff_lock:
                            self._handoff_events.append(event)
                            self._handoff_events = self._handoff_events[-50:]
                        logger.info(f"HANDOFF: {ue_id} {old_bs} → {new_bs}")

                prev_assignments = current

            except Exception as e:
                logger.error(f"RAN twin loop error: {e}")

            time.sleep(1.0)

    # =========================
    # 🔥 NEW: TRANSPORT → RAN HOOK
    # =========================
    def handle_transport_event(self, event: Dict):
        """
        Called by cross-layer bridge when congestion detected.
        """
        try:
            if event.get("action") == "load_reduce_suggested":
                bs_id = event.get("base_station")
                if bs_id:
                    logger.info(f"[RAN CONTROL] Triggered for {bs_id}")
                    self._rebalance_from_bs(bs_id)
        except Exception as e:
            logger.error(f"RAN control error: {e}")

    # =========================
    # 🔥 NEW: ACTUAL CONTROL LOGIC
    # =========================
    def _rebalance_from_bs(self, congested_bs: str):
        """
        Move UEs away from congested BS (REAL EFFECT)
        """
        if not hasattr(self.sim, "ue_assignments"):
            logger.warning("Simulator missing ue_assignments → cannot rebalance")
            return

        assignments = self.sim.ue_assignments

        # Find UEs on congested BS
        affected = [ue for ue, bs in assignments.items() if bs == congested_bs]

        if not affected:
            return

        # Find alternative BS
        all_bs = list(set(assignments.values()))
        target_bs = next((b for b in all_bs if b != congested_bs), None)

        if not target_bs:
            return

        num_to_move = max(1, len(affected) // 2)

        logger.info(f"[RAN CONTROL] Moving {num_to_move} UEs {congested_bs} → {target_bs}")

        for ue in affected[:num_to_move]:
            old_bs = assignments[ue]
            assignments[ue] = target_bs  # 🔥 THIS IS THE REAL CHANGE

            # Log handoff so transport reacts
            event = {
                'ue_id': ue,
                'from_bs': old_bs,
                'to_bs': target_bs,
                'timestamp': time.time(),
                'type': 'handoff'
            }

            with self._handoff_lock:
                self._handoff_events.append(event)
                self._handoff_events = self._handoff_events[-50:]

            logger.info(f"[RAN CONTROL] UE {ue} moved {old_bs} → {target_bs}")

    # =========================

    def get_status(self) -> Dict[str, Any]:
        summary = self.store.kpi_summary()
        summary['active_issues'] = self.store.active_anomaly_count()
        summary['tick_count'] = self.tick_count
        return summary

    def get_records(self) -> List[Dict]:
        return [r.to_dict() for r in self.store.latest_records()]

    def get_anomalies(self, n: int = 15) -> List[Dict]:
        return [asdict(a) for a in self.store.recent_anomalies(n)]

    def get_bs_summary(self) -> List[Dict]:
        return self.store.per_bs_summary()

    def get_actions(self, n: int = 15) -> List[Dict]:
        return [a.to_dict() for a in self.optimizer.recent_actions(n)]

    def get_handoff_events(self, n: int = 10) -> List[Dict]:
        with self._handoff_lock:
            return self._handoff_events[-n:]

    def drain_handoff_events(self) -> List[Dict]:
        with self._handoff_lock:
            events = list(self._handoff_events)
            self._handoff_events.clear()
        return events

    def set_optimizer_mode(self, mode: str):
        self.optimizer.set_mode(mode)

    def get_optimizer_info(self) -> Dict:
        return {
            'mode': self.optimizer.mode,
            'ml_stats': self.optimizer.ml_stats()
        }

    def fail_bs(self, bs_id: str):
        self.sim.fail_bs(bs_id)

    def restore_bs(self, bs_id: str):
        self.sim.restore_bs(bs_id)

    def explain_latest(self) -> Dict:
        from llm_explainer import explain_anomaly
        recent = self.store.recent_anomalies(1)
        if not recent:
            return {'explanation': 'No anomalies detected yet.'}
        a = recent[-1]
        return {
            'explanation': explain_anomaly(a),
            'anomaly': {'ue_id': a.ue_id, 'sinr_db': a.sinr_db}
        }

    def what_if(self, question: str) -> str:
        from llm_explainer import what_if_query
        return what_if_query(question, self.store)