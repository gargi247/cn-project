"""
data_store.py
In-memory time-series store + statistical anomaly detector.
No database required — stores the last N ticks in a deque.

Anomaly detection: z-score on SINR and RSRP per UE over a rolling window.
"""
'''
from collections import deque, defaultdict
from dataclasses import dataclass
from typing import List, Dict, Any
import statistics

from simulator import Record

# ── Configuration ─────────────────────────────────────────────────────────────
MAX_TICKS      = 200      # how many ticks to keep in memory
WINDOW         = 20       # rolling window for z-score baseline (longer = more stable)
Z_THRESHOLD    = 3.0      # standard deviations — raised to reduce false positives
SINR_HARD_MIN  = -15.0    # always flag below this regardless of z-score
RSRP_HARD_MIN  = -115.0   # always flag below this
WARMUP_TICKS   = 15       # don't flag anomalies until we have a stable baseline


@dataclass
class Anomaly:
    timestamp: float
    ue_id: str
    bs_id: str
    sinr_db: float
    rsrp_dbm: float
    throughput_mbps: float
    latency_ms: float
    reason: str            # human-readable cause


class DataStore:
    """
    Central store for the DTN twin.
    Call .ingest(records) every tick.
    Call .get_anomalies() for the latest flagged events.
    """

    def __init__(self):
        self.ticks: deque = deque(maxlen=MAX_TICKS)
        self._ue_sinr_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=WINDOW))
        self._ue_rsrp_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=WINDOW))
        self.anomalies: deque = deque(maxlen=500)
        self._tick_count: int = 0

    # ── Ingest ────────────────────────────────────────────────────────────────

    def ingest(self, records: List[Record]) -> List[Anomaly]:
        """Store records and run anomaly detection. Returns new anomalies."""
        self.ticks.append(records)
        self._tick_count += 1
        new_anomalies = []

        for r in records:
            # Update rolling history first so baseline builds up
            self._ue_sinr_history[r.ue_id].append(r.sinr_db)
            self._ue_rsrp_history[r.ue_id].append(r.rsrp_dbm)

            # Only detect anomalies after warmup period
            if self._tick_count < WARMUP_TICKS:
                continue

            anomaly = self._check(r)
            if anomaly:
                r.is_anomaly = True
                self.anomalies.append(anomaly)
                new_anomalies.append(anomaly)

        return new_anomalies

        from typing import Optional

        def _check(self, r: Record) -> Optional[Anomaly]:
            reasons = []

        # Hard thresholds
        if r.sinr_db < SINR_HARD_MIN:
            reasons.append(f"SINR critically low ({r.sinr_db:.1f} dB)")
        if r.rsrp_dbm < RSRP_HARD_MIN:
            reasons.append(f"RSRP critically low ({r.rsrp_dbm:.1f} dBm)")

        # Z-score check on SINR
        sinr_hist = list(self._ue_sinr_history[r.ue_id])
        if len(sinr_hist) >= 4:
            mean = statistics.mean(sinr_hist)
            std  = statistics.stdev(sinr_hist) or 1.0
            z    = (r.sinr_db - mean) / std
            if z < -Z_THRESHOLD:
                reasons.append(f"SINR dropped {abs(z):.1f}σ below baseline ({mean:.1f} dB avg)")

        # Z-score check on RSRP
        rsrp_hist = list(self._ue_rsrp_history[r.ue_id])
        if len(rsrp_hist) >= 4:
            mean = statistics.mean(rsrp_hist)
            std  = statistics.stdev(rsrp_hist) or 1.0
            z    = (r.rsrp_dbm - mean) / std
            if z < -Z_THRESHOLD:
                reasons.append(f"RSRP dropped {abs(z):.1f}σ below baseline ({mean:.1f} dBm avg)")

        if not reasons:
            return None

        return Anomaly(
            timestamp=r.timestamp,
            ue_id=r.ue_id,
            bs_id=r.bs_id,
            sinr_db=r.sinr_db,
            rsrp_dbm=r.rsrp_dbm,
            throughput_mbps=r.throughput_mbps,
            latency_ms=r.latency_ms,
            reason="; ".join(reasons),
        )

    # ── Query helpers ─────────────────────────────────────────────────────────

    def latest_records(self) -> List[Record]:
        """Most recent tick's records."""
        return list(self.ticks[-1]) if self.ticks else []

    def recent_anomalies(self, n: int = 20) -> List[Anomaly]:
        return list(self.anomalies)[-n:]

    def kpi_summary(self) -> Dict[str, Any]:
        """Aggregate KPIs across the latest tick for the dashboard."""
        records = self.latest_records()
        if not records:
            return {}

        sinrs  = [r.sinr_db for r in records]
        rsrps  = [r.rsrp_dbm for r in records]
        tputs  = [r.throughput_mbps for r in records]
        lats   = [r.latency_ms for r in records]
        n_bad  = sum(1 for r in records if r.sinr_db < 0)

        return {
            "num_ues":           len(records),
            "avg_sinr_db":       round(statistics.mean(sinrs), 1),
            "avg_rsrp_dbm":      round(statistics.mean(rsrps), 1),
            "avg_throughput_mbps": round(statistics.mean(tputs), 1),
            "avg_latency_ms":    round(statistics.mean(lats), 1),
            "ues_below_0db_sinr": n_bad,
            "total_anomalies":   len(self.anomalies),
        }

    def per_bs_summary(self) -> List[Dict]:
        """Per-BS aggregated KPIs for the latest tick."""
        records = self.latest_records()
        bs_map: Dict[str, list] = defaultdict(list)
        for r in records:
            bs_map[r.bs_id].append(r)

        result = []
        for bs_id, recs in sorted(bs_map.items()):
            result.append({
                "bs_id":      bs_id,
                "cell_type":  recs[0].cell_type,
                "freq_ghz":   recs[0].freq_ghz,
                "num_ues":    len(recs),
                "avg_sinr":   round(statistics.mean(r.sinr_db for r in recs), 1),
                "avg_tput":   round(statistics.mean(r.throughput_mbps for r in recs), 1),
            })
        return result

    def sinr_history(self, ue_id: str) -> List[float]:
        return list(self._ue_sinr_history[ue_id])

    def active_anomaly_count(self, last_ticks: int = 10) -> int:
        """
        Count anomalies only in recent ticks (NOT cumulative).
        This shows real-time network health.
        """
        if not self.ticks:
            return 0

        recent_ticks = list(self.ticks)[-last_ticks:]
        count = 0

        for tick in recent_ticks:
            for r in tick:
                if r.is_anomaly:
                    count += 1

        return count
'''

"""
data_store.py
In-memory time-series store + statistical anomaly detector.
"""

from collections import deque, defaultdict
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
import statistics

from simulator import Record

# ── Configuration ─────────────────────────────────────────────────────────────
MAX_TICKS      = 200
WINDOW         = 20
Z_THRESHOLD    = 3.0
SINR_HARD_MIN  = -15.0
RSRP_HARD_MIN  = -115.0
WARMUP_TICKS   = 15


@dataclass
class Anomaly:
    timestamp: float
    ue_id: str
    bs_id: str
    sinr_db: float
    rsrp_dbm: float
    throughput_mbps: float
    latency_ms: float
    reason: str


class DataStore:

    def __init__(self):
        self.ticks: deque = deque(maxlen=MAX_TICKS)
        self._ue_sinr_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=WINDOW))
        self._ue_rsrp_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=WINDOW))
        self.anomalies: deque = deque(maxlen=500)
        self._tick_count: int = 0

    # ── Ingest ────────────────────────────────────────────────────────────────

    def ingest(self, records: List[Record]) -> List[Anomaly]:
        self.ticks.append(records)
        self._tick_count += 1
        new_anomalies = []

        for r in records:
            # Build history
            self._ue_sinr_history[r.ue_id].append(r.sinr_db)
            self._ue_rsrp_history[r.ue_id].append(r.rsrp_dbm)

            # Skip warmup
            if self._tick_count < WARMUP_TICKS:
                continue

            anomaly = self._check(r)
            if anomaly:
                r.is_anomaly = True
                self.anomalies.append(anomaly)
                new_anomalies.append(anomaly)

        return new_anomalies

    # ── FIXED: Properly defined method ─────────────────────────────────────────

    def _check(self, r: Record) -> Optional[Anomaly]:
        reasons = []

        # Hard thresholds
        if r.sinr_db < SINR_HARD_MIN:
            reasons.append(f"SINR critically low ({r.sinr_db:.1f} dB)")

        if r.rsrp_dbm < RSRP_HARD_MIN:
            reasons.append(f"RSRP critically low ({r.rsrp_dbm:.1f} dBm)")

        # Z-score SINR
        sinr_hist = list(self._ue_sinr_history[r.ue_id])
        if len(sinr_hist) >= 4:
            mean = statistics.mean(sinr_hist)
            std  = statistics.stdev(sinr_hist) or 1.0
            z    = (r.sinr_db - mean) / std

            if z < -Z_THRESHOLD:
                reasons.append(
                    f"SINR dropped {abs(z):.1f}σ below baseline ({mean:.1f} dB avg)"
                )

        # Z-score RSRP
        rsrp_hist = list(self._ue_rsrp_history[r.ue_id])
        if len(rsrp_hist) >= 4:
            mean = statistics.mean(rsrp_hist)
            std  = statistics.stdev(rsrp_hist) or 1.0
            z    = (r.rsrp_dbm - mean) / std

            if z < -Z_THRESHOLD:
                reasons.append(
                    f"RSRP dropped {abs(z):.1f}σ below baseline ({mean:.1f} dBm avg)"
                )

        if not reasons:
            return None

        return Anomaly(
            timestamp=r.timestamp,
            ue_id=r.ue_id,
            bs_id=r.bs_id,
            sinr_db=r.sinr_db,
            rsrp_dbm=r.rsrp_dbm,
            throughput_mbps=r.throughput_mbps,
            latency_ms=r.latency_ms,
            reason="; ".join(reasons),
        )

    # ── Query helpers ─────────────────────────────────────────────────────────

    def latest_records(self) -> List[Record]:
        return list(self.ticks[-1]) if self.ticks else []

    def recent_anomalies(self, n: int = 20) -> List[Anomaly]:
        return list(self.anomalies)[-n:]

    def kpi_summary(self) -> Dict[str, Any]:
        records = self.latest_records()
        if not records:
            return {}

        sinrs  = [r.sinr_db for r in records]
        rsrps  = [r.rsrp_dbm for r in records]
        tputs  = [r.throughput_mbps for r in records]
        lats   = [r.latency_ms for r in records]
        n_bad  = sum(1 for r in records if r.sinr_db < 0)

        return {
            "num_ues": len(records),
            "avg_sinr_db": round(statistics.mean(sinrs), 1),
            "avg_rsrp_dbm": round(statistics.mean(rsrps), 1),
            "avg_throughput_mbps": round(statistics.mean(tputs), 1),
            "avg_latency_ms": round(statistics.mean(lats), 1),
            "ues_below_0db_sinr": n_bad,
            "total_anomalies": len(self.anomalies),
        }

    def per_bs_summary(self) -> List[Dict]:
        records = self.latest_records()
        bs_map: Dict[str, list] = defaultdict(list)

        for r in records:
            bs_map[r.bs_id].append(r)

        result = []
        for bs_id, recs in sorted(bs_map.items()):
            result.append({
                "bs_id": bs_id,
                "cell_type": recs[0].cell_type,
                "freq_ghz": recs[0].freq_ghz,
                "num_ues": len(recs),
                "avg_sinr": round(statistics.mean(r.sinr_db for r in recs), 1),
                "avg_tput": round(statistics.mean(r.throughput_mbps for r in recs), 1),
            })

        return result

    def sinr_history(self, ue_id: str) -> List[float]:
        return list(self._ue_sinr_history[ue_id])

    def active_anomaly_count(self, last_ticks: int = 10) -> int:
        if not self.ticks:
            return 0

        recent_ticks = list(self.ticks)[-last_ticks:]
        count = 0

        for tick in recent_ticks:
            for r in tick:
                if r.is_anomaly:
                    count += 1

        return count