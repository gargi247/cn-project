"""
simulator.py
Generates fake-but-realistic 6G network telemetry.
No Kafka, no external dependencies — pure Python.
"""

import math, random, time
from dataclasses import dataclass, asdict
from typing import List

# ── Base stations (fixed positions) ──────────────────────────────────────────
BASE_STATIONS = [
    {"id": "BS_MAC_0", "x": 0,    "y": 0,    "type": "macro", "freq": 3.5,  "power": 46, "h": 25},
    {"id": "BS_MAC_1", "x": 800,  "y": 0,    "type": "macro", "freq": 3.5,  "power": 46, "h": 25},
    {"id": "BS_MAC_2", "x": -800, "y": 0,    "type": "macro", "freq": 3.5,  "power": 46, "h": 25},
    {"id": "BS_MAC_3", "x": 400,  "y": 700,  "type": "macro", "freq": 3.5,  "power": 46, "h": 25},
    {"id": "BS_MAC_4", "x": -400, "y": -700, "type": "macro", "freq": 3.5,  "power": 46, "h": 25},
    {"id": "BS_MIC_0", "x": 200,  "y": 200,  "type": "micro", "freq": 28.0, "power": 33, "h": 10},
    {"id": "BS_MIC_1", "x": -300, "y": 400,  "type": "micro", "freq": 28.0, "power": 33, "h": 10},
    {"id": "BS_MIC_2", "x": 500,  "y": -300, "type": "micro", "freq": 28.0, "power": 33, "h": 10},
]

@dataclass
class Record:
    timestamp: float
    ue_id: str
    bs_id: str
    cell_type: str
    freq_ghz: float
    distance_m: float
    rsrp_dbm: float
    sinr_db: float
    throughput_mbps: float
    latency_ms: float
    is_anomaly: bool = False   # labelled by anomaly detector later

    def to_dict(self):
        return asdict(self)


def _path_loss(d_m: float, freq_ghz: float, cell_type: str) -> float:
    d_m = max(d_m, 10.0)
    if cell_type == "macro":
        return 28.0 + 22.0 * math.log10(d_m) + 20.0 * math.log10(freq_ghz)
    else:
        return 32.4 + 21.0 * math.log10(d_m) + 20.0 * math.log10(freq_ghz)


def _throughput(sinr_db: float, bw_mhz: float) -> float:
    sinr = min(10 ** (sinr_db / 10), 10 ** 3)
    return round(0.7 * bw_mhz * math.log2(1 + sinr), 2)


def _latency(sinr_db: float, rng: random.Random) -> float:
    if sinr_db > 15:   base = rng.uniform(1, 4)
    elif sinr_db > 0:  base = rng.uniform(4, 20)
    else:              base = rng.uniform(20, 100)
    return round(base + abs(rng.gauss(0, base * 0.1)), 2)


class NetworkSimulator:
    """
    Maintains UE positions, moves them each tick, returns telemetry records.
    Optionally injects fault events (BS failure, interference spike) so the
    anomaly detector has something real to catch.
    """

    def __init__(self, num_ues: int = 30, seed: int = 42):
        self.rng = random.Random(seed)
        self.ues = [
            {
                "id": f"UE_{i:03d}",
                "x": self.rng.uniform(-900, 900),
                "y": self.rng.uniform(-900, 900),
                "vx": self.rng.uniform(-5, 5),
                "vy": self.rng.uniform(-5, 5),
            }
            for i in range(num_ues)
        ]
        self.failed_bs: set = set()          # BS IDs currently "down"
        self.interference_ues: set = set()   # UE IDs under extra interference
        self.ue_bs_assignment: dict = {}   # ue_id → current bs_id (sticky)

    # ── Fault injection (called externally for what-if demos) ──

    def fail_bs(self, bs_id: str):
        self.failed_bs.add(bs_id)

    def restore_bs(self, bs_id: str):
        self.failed_bs.discard(bs_id)

    def inject_interference(self, ue_id: str):
        self.interference_ues.add(ue_id)

    def clear_interference(self, ue_id: str):
        self.interference_ues.discard(ue_id)

    # ── Tick ──────────────────────────────────────────────────────────────────

    def tick(self) -> List[Record]:
        self._move_ues()
        records = []
        ts = time.time()

        for ue in self.ues:
            bs = self._best_bs(ue)
            if bs is None:
                continue

            d2d = math.sqrt((ue["x"] - bs["x"]) ** 2 + (ue["y"] - bs["y"]) ** 2)
            d3d = math.sqrt(d2d ** 2 + (bs["h"] - 1.5) ** 2)

            pl = _path_loss(d3d, bs["freq"], bs["type"])
            shadowing = self.rng.gauss(0, 5 if bs["type"] == "macro" else 7)

            rsrp = bs["power"] - pl - shadowing

            # Inter-cell interference: only same-frequency BSs interfere
            interference_linear = 0.0
            for ibs in BASE_STATIONS:
                if ibs["id"] == bs["id"] or ibs["id"] in self.failed_bs:
                    continue
                # Different frequency bands don't interfere
                if abs(ibs["freq"] - bs["freq"]) > 1.0:
                    continue
                id2 = math.sqrt((ue["x"] - ibs["x"]) ** 2 + (ue["y"] - ibs["y"]) ** 2)
                id3 = math.sqrt(id2 ** 2 + (ibs["h"] - 1.5) ** 2)
                ipl = _path_loss(id3, ibs["freq"], ibs["type"])
                irx = ibs["power"] - ipl
                interference_linear += 10 ** (irx / 10)

            interference_dbm = 10 * math.log10(interference_linear) if interference_linear > 0 else -130.0

            # Extra penalty for injected interference UEs
            if ue["id"] in self.interference_ues:
                interference_dbm += 15

            noise_dbm = -104  # dBm (20 MHz NR thermal noise)
            # SINR in dB
            signal_lin = 10 ** (rsrp / 10)
            noise_lin  = 10 ** (noise_dbm / 10)
            interf_lin = 10 ** (interference_dbm / 10)
            sinr = 10 * math.log10(signal_lin / (noise_lin + interf_lin))

            bw = 20.0 if bs["type"] == "macro" else 100.0

            records.append(Record(
                timestamp=ts,
                ue_id=ue["id"],
                bs_id=bs["id"],
                cell_type=bs["type"],
                freq_ghz=bs["freq"],
                distance_m=round(d2d, 1),
                rsrp_dbm=round(rsrp, 2),
                sinr_db=round(sinr, 2),
                throughput_mbps=_throughput(sinr, bw),
                latency_ms=_latency(sinr, self.rng),
            ))

        return records

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _move_ues(self):
        for ue in self.ues:
            ue["x"] = max(-950, min(950, ue["x"] + ue["vx"] + self.rng.gauss(0, 1)))
            ue["y"] = max(-950, min(950, ue["y"] + ue["vy"] + self.rng.gauss(0, 1)))
            ue["vx"] += self.rng.gauss(0, 0.3)
            ue["vy"] += self.rng.gauss(0, 0.3)
            ue["vx"] = max(-15, min(15, ue["vx"]))
            ue["vy"] = max(-15, min(15, ue["vy"]))

    def _best_bs(self, ue: dict):
        active = [bs for bs in BASE_STATIONS if bs["id"] not in self.failed_bs]
        if not active:
            return None
        nearest = min(active, key=lambda bs: math.sqrt(
            (ue["x"] - bs["x"])**2 + (ue["y"] - bs["y"])**2
        ))
        current_id = self.ue_bs_assignment.get(ue["id"])
        if current_id and current_id != nearest["id"]:
            current_bs = next((b for b in active if b["id"] == current_id), None)
            if current_bs:
                d_current = math.sqrt((ue["x"]-current_bs["x"])**2 + (ue["y"]-current_bs["y"])**2)
                d_nearest = math.sqrt((ue["x"]-nearest["x"])**2 + (ue["y"]-nearest["y"])**2)
                if d_current / d_nearest < 1.15:   # must be 15% better to trigger handoff
                    return current_bs
        self.ue_bs_assignment[ue["id"]] = nearest["id"]
        return nearest
    

    def get_random_bs(self, exclude=None):
        active = [bs["id"] for bs in BASE_STATIONS if bs["id"] not in self.failed_bs]
        if exclude in active:
            active.remove(exclude)
        return self.rng.choice(active) if active else None

    def force_handoff(self, ue_id: str, new_bs_id: str):
        """
        Teleport a UE to be closest to new_bs_id by overriding its position.
        Since _best_bs() always picks nearest BS, we move the UE near the target BS.
        """
        target = next((bs for bs in BASE_STATIONS if bs["id"] == new_bs_id), None)
        if target is None:
            return
        for ue in self.ues:
            if ue["id"] == ue_id:
            # Place UE 50m from the target BS so it stays connected to it
                ue["x"] = target["x"] + self.rng.uniform(-50, 50)
                ue["y"] = target["y"] + self.rng.uniform(-50, 50)
                ue["vx"] = self.rng.uniform(-2, 2)   # slow it down too
                ue["vy"] = self.rng.uniform(-2, 2)
                break
