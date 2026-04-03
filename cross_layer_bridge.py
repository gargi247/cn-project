import threading
import time
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)

BS_TO_HOST = {
    'BS_MAC_0': 'h1',
    'BS_MAC_1': 'h2',
    'BS_MAC_2': 'h3',
    'BS_MAC_3': 'h4',
    'BS_MAC_4': 'h5',
    'BS_MIC_0': 'h3',
    'BS_MIC_1': 'h5',
    'BS_MIC_2': 'h6',
}

HOST_TO_BS = {v: k for k, v in BS_TO_HOST.items()}

BS_TO_SWITCH = {
    'BS_MAC_0': 's1', 'BS_MAC_1': 's1',
    'BS_MAC_2': 's2', 'BS_MAC_3': 's2', 'BS_MIC_0': 's2',
    'BS_MAC_4': 's3', 'BS_MIC_1': 's3', 'BS_MIC_2': 's3',
}


class CrossLayerBridge:

    def __init__(self, ran_twin, transport_db=None, transport_controller=None):
        self.ran = ran_twin
        self.db  = transport_db
        self.ctrl = transport_controller
        self.running = False
        self._thread = None

        self.events_log: List[Dict] = []

        self.stats = {
            'handoffs_processed': 0,
            'transport_reroutes_triggered': 0,
            'ran_load_reductions': 0,
        }

        # 🔥 NEW: cooldown to stop spam
        self._recent_actions = {}
        self.COOLDOWN = 10  # seconds

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Cross-layer bridge started")

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                self._process_ran_to_transport()
                self._process_transport_to_ran()
            except Exception as e:
                logger.error(f"Bridge error: {e}")
            time.sleep(2.0)

    # =========================
    # RAN → TRANSPORT
    # =========================
    def _process_ran_to_transport(self):
        handoffs = self.ran.drain_handoff_events()

        for event in handoffs:
            self.stats['handoffs_processed'] += 1

            from_bs = event['from_bs']
            to_bs   = event['to_bs']
            ue_id   = event['ue_id']

            from_switch = BS_TO_SWITCH.get(from_bs)
            to_switch   = BS_TO_SWITCH.get(to_bs)

            cross_layer_event = {
                'type': 'ran_handoff',
                'ue_id': ue_id,
                'from_bs': from_bs,
                'to_bs': to_bs,
                'from_switch': from_switch,
                'to_switch': to_switch,
                'transport_action': None,
                'timestamp': time.time()
            }

            if from_switch and to_switch and from_switch != to_switch:
                logger.info(
                    f"Cross-switch handoff: {ue_id} {from_bs}({from_switch}) "
                    f"→ {to_bs}({to_switch})"
                )

                src_host = BS_TO_HOST.get(from_bs)
                dst_host = BS_TO_HOST.get(to_bs)

                if src_host and dst_host and self.ctrl:
                    cross_layer_event['transport_action'] = (
                        f"{src_host}→{dst_host} reroute check"
                    )
                    self.stats['transport_reroutes_triggered'] += 1

            self.events_log.append(cross_layer_event)
            self.events_log = self.events_log[-100:]

    # =========================
    # 🔥 TRANSPORT → RAN (FIXED)
    # =========================
    def _process_transport_to_ran(self):
        if not self.ctrl:
            return

        active_congestion = self.ctrl.active_congestion
        if not active_congestion:
            return

        now = time.time()

        for link_id, event in active_congestion.items():

            # 🔥 cooldown check
            last = self._recent_actions.get(link_id, 0)
            if now - last < self.COOLDOWN:
                continue

            self._recent_actions[link_id] = now

            src_host = event.get('src')
            dst_host = event.get('dst')

            src_bs = HOST_TO_BS.get(src_host)

            if not src_bs:
                continue

            logger.info(
                f"[CROSS-LAYER] {link_id} → triggering RAN load reduction on {src_bs}"
            )

            # ✅ ACTUAL CONTROL (FIXED)
            self.ran.trigger_load_reduction(src_bs)

            self.stats['ran_load_reductions'] += 1

            cross_layer_event = {
                'type': 'transport_to_ran',
                'transport_link': link_id,
                'affected_bs': src_bs,
                'action': 'load_reduction_triggered',
                'timestamp': now
            }

            self.events_log.append(cross_layer_event)
            self.events_log = self.events_log[-100:]

    # =========================

    def get_events(self, n: int = 20) -> List[Dict]:
        return self.events_log[-n:]

    def get_stats(self) -> Dict:
        return self.stats

    def get_topology_mapping(self) -> Dict:
        return {
            'bs_to_host': BS_TO_HOST,
            'bs_to_switch': BS_TO_SWITCH,
        }