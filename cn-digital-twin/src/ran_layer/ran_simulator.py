#!/usr/bin/env python3
"""
ran_layer/ran_simulator.py
───────────────────────────
Real RAN (Radio Access Network) simulator with:

  1. Path loss models:
       - Friis free-space
       - Log-distance (urban exponent)
       - COST-231 Hata (suburban/urban macro)
  2. Received signal strength (RSS) calculation
  3. SINR (Signal-to-Interference-plus-Noise Ratio)
  4. Shannon capacity theorem: C = B * log2(1 + SINR)
  5. 3GPP A3-event handover (hysteresis + time-to-trigger)
  6. Doppler shift for mobile UE
  7. Log-normal shadowing (random slow fading)
  8. Periodic state broadcast to cross-layer bridge (UDP)

CN Concepts:
  - Wireless channel modelling
  - Shannon–Hartley theorem
  - SINR and interference
  - Handover / mobility management
  - Cross-layer design (RAN state → routing decisions)
"""

import math
import time
import random
import socket
import struct
import json
import threading
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import (
    BASE_STATIONS, UE, CARRIER_FREQ_HZ, TRANSMIT_POWER_DBM,
    NOISE_FIGURE_DB, BANDWIDTH_HZ, THERMAL_NOISE_DBM,
    PATH_LOSS_MODEL, PATH_LOSS_EXPONENT,
    REFERENCE_DISTANCE_M, SHADOW_FADING_STD_DB,
    HANDOVER_HYSTERESIS_DB, HANDOVER_TTT_S, SINR_TARGET_DB,
    BRIDGE_HOST, BRIDGE_PORT,
    MSG_HANDOVER_COMPLETE, MSG_REROUTE_REQUEST,
    MSG_BS_FAILURE, MSG_LINK_STATE_UPDATE,
    LOG_DIR, METRICS_FILE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RAN] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)
os.makedirs(LOG_DIR, exist_ok=True)

SPEED_OF_LIGHT = 3e8   # m/s


# ── Path loss models ───────────────────────────────────────────────────────
def friis_path_loss_db(distance_m, freq_hz):
    """
    Friis free-space path loss (dB):
      PL = 20*log10(4*pi*d*f / c)
    Valid for line-of-sight, far field (d > lambda).
    """
    if distance_m < 1:
        distance_m = 1.0
    wavelength = SPEED_OF_LIGHT / freq_hz
    pl = 20 * math.log10((4 * math.pi * distance_m) / wavelength)
    return pl


def log_distance_path_loss_db(distance_m, freq_hz, n=PATH_LOSS_EXPONENT,
                               d0=REFERENCE_DISTANCE_M):
    """
    Log-distance path loss model:
      PL(d) = PL(d0) + 10*n*log10(d/d0)
    where PL(d0) is free-space loss at reference distance d0.
    n: path-loss exponent (2=free space, 3-4=urban, 4-6=indoor)
    """
    if distance_m < d0:
        distance_m = d0
    pl_d0 = friis_path_loss_db(d0, freq_hz)
    pl    = pl_d0 + 10 * n * math.log10(distance_m / d0)
    return pl


def cost231_hata_db(distance_m, freq_mhz, h_bs_m=30.0, h_ue_m=1.5,
                    urban=True):
    """
    COST-231 Hata model for urban/suburban macro cells.
    Valid: 1500–2000 MHz, d: 1–20 km.
      PL = A + B*log10(d_km) - E
    """
    d_km  = max(distance_m / 1000.0, 0.001)
    f_mhz = freq_mhz

    # Mobile antenna height correction
    a_hm  = (1.1 * math.log10(f_mhz) - 0.7) * h_ue_m - (1.56 * math.log10(f_mhz) - 0.8)
    A     = 46.3 + 33.9 * math.log10(f_mhz) - 13.82 * math.log10(h_bs_m) - a_hm
    B     = 44.9 - 6.55 * math.log10(h_bs_m)
    C     = 0.0 if urban else 3.0   # suburban correction
    pl    = A + B * math.log10(d_km) - C
    return pl


def compute_path_loss(distance_m, freq_hz, model="log_distance"):
    if model == "friis":
        return friis_path_loss_db(distance_m, freq_hz)
    elif model == "cost231":
        return cost231_hata_db(distance_m, freq_hz / 1e6)
    else:
        return log_distance_path_loss_db(distance_m, freq_hz)


# ── Doppler shift ──────────────────────────────────────────────────────────
def doppler_shift_hz(velocity_mps, freq_hz, angle_rad=0.0):
    """
    Doppler shift: fd = (v/c) * f * cos(theta)
    theta: angle between velocity vector and BS direction.
    For worst-case (heading straight toward/away): theta=0.
    """
    return (velocity_mps / SPEED_OF_LIGHT) * freq_hz * math.cos(angle_rad)


# ── RAN metrics per BS ─────────────────────────────────────────────────────
class BSMetrics:
    """All RF metrics for one UE–BS pair."""

    def __init__(self, bs_id, rss_dbm, sinr_db, capacity_mbps,
                 shadow_db, doppler_hz):
        self.bs_id        = bs_id
        self.rss_dbm      = rss_dbm
        self.sinr_db      = sinr_db
        self.capacity_mbps = capacity_mbps
        self.shadow_db    = shadow_db
        self.doppler_hz   = doppler_hz
        self.timestamp    = time.time()

    def __repr__(self):
        return (f"BS={self.bs_id} RSS={self.rss_dbm:.1f}dBm "
                f"SINR={self.sinr_db:.1f}dB C={self.capacity_mbps:.1f}Mbps")


# ── RAN Simulator ─────────────────────────────────────────────────────────
class RANSimulator:
    """
    Simulates a UE moving through a multi-BS RAN.
    Computes per-BS RSS/SINR/capacity, manages handovers,
    and sends cross-layer signals to the transport controller.
    """

    def __init__(self):
        self.bs_config    = BASE_STATIONS
        self.ue_pos       = list(UE["position"])   # [x, y]
        self.ue_vel       = UE["velocity_mps"]
        self.ue_direction = 0.0   # radians

        self.current_bs   = None  # currently serving BS id
        self.bs_states    = {}    # bs_id → {"active": bool, ...}
        for bs_id in self.bs_config:
            self.bs_states[bs_id] = {"active": True}

        # Handover TTT tracking (3GPP A3 event)
        self._ho_candidate  = None
        self._ho_start_time = None

        # Shadow fading per BS (slow-varying, updated every ~5s)
        self._shadow = {bs: 0.0 for bs in self.bs_config}
        self._shadow_ts = 0.0

        # Cross-layer socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Metrics log
        self._metrics_log = os.path.join(LOG_DIR, "ran_metrics.csv")
        with open(self._metrics_log, "w") as f:
            f.write("ts,bs,rss_dbm,sinr_db,capacity_mbps,serving\n")

        self._running = False

    # ── RF calculations ────────────────────────────────────────────────────
    def _distance(self, pos, bs_pos):
        return math.sqrt((pos[0]-bs_pos[0])**2 + (pos[1]-bs_pos[1])**2)

    def _update_shadow_fading(self):
        """Update log-normal shadow fading every 5 seconds (slow fading)."""
        now = time.time()
        if now - self._shadow_ts > 5.0:
            for bs in self.bs_config:
                self._shadow[bs] = random.gauss(0, SHADOW_FADING_STD_DB)
            self._shadow_ts = now

    def compute_rss_dbm(self, bs_id):
        """
        Received Signal Strength (dBm):
          RSS = P_tx + G_tx + G_rx - PL - shadow_fading
        """
        bs   = self.bs_config[bs_id]
        dist = self._distance(self.ue_pos, bs["position"])
        pl   = compute_path_loss(dist, bs["frequency_hz"], PATH_LOSS_MODEL)
        rss  = (bs["tx_power_dbm"]
                + bs["antenna_gain_dbi"]
                + UE["rx_antenna_gain_dbi"]
                - pl
                - self._shadow[bs_id])
        return rss

    def compute_sinr_db(self, serving_bs_id, all_rss):
        """
        SINR = S / (I + N)
        S: signal from serving BS
        I: interference = sum of RSS from all other ACTIVE BS
        N: thermal noise
        All in linear scale (mW), then convert to dB.
        """
        def dbm_to_mw(dbm):
            return 10 ** (dbm / 10.0)

        signal    = dbm_to_mw(all_rss[serving_bs_id])
        noise_mw  = dbm_to_mw(THERMAL_NOISE_DBM)
        interfere = sum(
            dbm_to_mw(rss)
            for bs_id, rss in all_rss.items()
            if bs_id != serving_bs_id and self.bs_states[bs_id]["active"]
        )
        sinr_linear = signal / (interfere + noise_mw)
        return 10 * math.log10(max(sinr_linear, 1e-10))

    def shannon_capacity_mbps(self, sinr_db, bandwidth_hz):
        """
        Shannon–Hartley theorem:
          C = B * log2(1 + SINR_linear)   [bits/sec]
        """
        sinr_linear = 10 ** (sinr_db / 10.0)
        capacity_bps = bandwidth_hz * math.log2(1 + sinr_linear)
        return capacity_bps / 1e6

    def _compute_doppler(self, bs_id):
        bs_pos = self.bs_config[bs_id]["position"]
        dx = bs_pos[0] - self.ue_pos[0]
        dy = bs_pos[1] - self.ue_pos[1]
        angle_to_bs = math.atan2(dy, dx)
        rel_angle   = angle_to_bs - self.ue_direction
        return doppler_shift_hz(self.ue_vel,
                                self.bs_config[bs_id]["frequency_hz"],
                                rel_angle)

    # ── All-BS snapshot ───────────────────────────────────────────────────
    def measure_all(self):
        self._update_shadow_fading()
        all_rss = {}
        for bs_id in self.bs_config:
            if self.bs_states[bs_id]["active"]:
                all_rss[bs_id] = self.compute_rss_dbm(bs_id)

        metrics = {}
        for bs_id, rss in all_rss.items():
            sinr   = self.compute_sinr_db(bs_id, all_rss)
            cap    = self.shannon_capacity_mbps(
                         sinr, self.bs_config[bs_id]["bandwidth_hz"])
            dopp   = self._compute_doppler(bs_id)
            shadow = self._shadow[bs_id]
            metrics[bs_id] = BSMetrics(bs_id, rss, sinr, cap, shadow, dopp)

        return metrics

    # ── Handover (3GPP A3 event) ──────────────────────────────────────────
    def check_handover(self, metrics):
        """
        A3 event: trigger handover when
          RSS(neighbour) - RSS(serving) > hysteresis_db
        AND the condition holds for time-to-trigger (TTT).
        """
        if self.current_bs is None:
            # Initial attachment: pick strongest
            if metrics:
                self.current_bs = max(metrics, key=lambda b: metrics[b].rss_dbm)
                log.info(f"Initial attachment → {self.current_bs}")
                self._send_handover_complete(self.current_bs)
            return

        if self.current_bs not in metrics:
            # Serving BS went down
            candidates = list(metrics.keys())
            if candidates:
                new_bs = max(candidates, key=lambda b: metrics[b].rss_dbm)
                log.warning(f"Serving BS {self.current_bs} lost — emergency HO → {new_bs}")
                self._do_handover(new_bs)
            return

        serving_rss = metrics[self.current_bs].rss_dbm
        best_other  = None
        best_rss    = -math.inf

        for bs_id, m in metrics.items():
            if bs_id != self.current_bs and m.rss_dbm > best_rss:
                best_rss   = m.rss_dbm
                best_other = bs_id

        if best_other is None:
            return

        a3_condition = (best_rss - serving_rss) > HANDOVER_HYSTERESIS_DB

        if a3_condition:
            if self._ho_candidate != best_other:
                self._ho_candidate  = best_other
                self._ho_start_time = time.time()
                log.debug(f"A3 event: HO candidate {best_other} "
                          f"(margin {best_rss-serving_rss:.1f} dB) — TTT started")
            elif time.time() - self._ho_start_time >= HANDOVER_TTT_S:
                log.info(f"TTT expired — executing handover {self.current_bs}→{best_other}")
                self._do_handover(best_other)
        else:
            self._ho_candidate  = None
            self._ho_start_time = None

    def _do_handover(self, new_bs_id):
        old_bs = self.current_bs
        self.current_bs    = new_bs_id
        self._ho_candidate = None
        log.info(f"Handover complete: {old_bs} → {new_bs_id}")
        self._send_handover_complete(new_bs_id)

    # ── Cross-layer signaling ─────────────────────────────────────────────
    def _send_msg(self, opcode, payload_dict):
        payload = json.dumps(payload_dict).encode()
        header  = struct.pack("!BH", opcode, len(payload))
        try:
            self._sock.sendto(header + payload, (BRIDGE_HOST, BRIDGE_PORT + 1))
        except Exception as e:
            log.error(f"Bridge send error: {e}")

    def _send_handover_complete(self, bs_id):
        sw = self.bs_config[bs_id]["connected_switch"]
        self._send_msg(MSG_HANDOVER_COMPLETE, {
            "new_bs":           bs_id,
            "connected_switch": sw,
        })

    def _send_reroute_request(self, reason):
        self._send_msg(MSG_REROUTE_REQUEST, {"reason": reason})

    def _send_bs_failure(self, bs_id):
        sw = self.bs_config[bs_id]["connected_switch"]
        self._send_msg(MSG_BS_FAILURE, {
            "bs_id":            bs_id,
            "connected_switch": sw,
        })

    # ── BS toggle (for dashboard control) ────────────────────────────────
    def toggle_bs(self, bs_id):
        if bs_id not in self.bs_states:
            log.error(f"Unknown BS: {bs_id}")
            return
        was = self.bs_states[bs_id]["active"]
        self.bs_states[bs_id]["active"] = not was
        if was:
            log.warning(f"BS {bs_id} OFFLINE")
            if self.current_bs == bs_id:
                self._send_bs_failure(bs_id)
            else:
                self._send_reroute_request(f"BS {bs_id} offline")
        else:
            log.info(f"BS {bs_id} ONLINE")

    # ── UE mobility ──────────────────────────────────────────────────────
    def move_ue(self, dt_s):
        """Random-waypoint-like: move UE, bounce off boundary."""
        self.ue_pos[0] += self.ue_vel * math.cos(self.ue_direction) * dt_s
        self.ue_pos[1] += self.ue_vel * math.sin(self.ue_direction) * dt_s
        # Boundary: 0–600m square
        if not (0 <= self.ue_pos[0] <= 600):
            self.ue_direction = math.pi - self.ue_direction
            self.ue_pos[0] = max(0, min(600, self.ue_pos[0]))
        if not (0 <= self.ue_pos[1] <= 600):
            self.ue_direction = -self.ue_direction
            self.ue_pos[1] = max(0, min(600, self.ue_pos[1]))
        # Randomly change direction slightly
        self.ue_direction += random.gauss(0, 0.1)

    # ── Metrics logging ───────────────────────────────────────────────────
    def _log_metrics(self, metrics):
        ts = time.strftime("%H:%M:%S")
        with open(self._metrics_log, "a") as f:
            for bs_id, m in metrics.items():
                serving = 1 if bs_id == self.current_bs else 0
                f.write(f"{ts},{bs_id},{m.rss_dbm:.2f},{m.sinr_db:.2f},"
                        f"{m.capacity_mbps:.2f},{serving}\n")

    # ── State snapshot (for dashboard / bridge) ───────────────────────────
    def get_state(self):
        metrics = self.measure_all()
        return {
            "ue_position":  self.ue_pos,
            "current_bs":   self.current_bs,
            "bs_states":    self.bs_states,
            "metrics": {
                bs_id: {
                    "rss_dbm":      round(m.rss_dbm, 2),
                    "sinr_db":      round(m.sinr_db, 2),
                    "capacity_mbps": round(m.capacity_mbps, 2),
                    "doppler_hz":   round(m.doppler_hz, 2),
                }
                for bs_id, m in metrics.items()
            },
        }

    # ── Main loop ─────────────────────────────────────────────────────────
    def run(self, interval_s=1.0):
        self._running = True
        log.info("RAN simulator running")
        log.info(f"  Path-loss model : {PATH_LOSS_MODEL}")
        log.info(f"  Carrier freq    : {CARRIER_FREQ_HZ/1e9:.1f} GHz")
        log.info(f"  Channel BW      : {BANDWIDTH_HZ/1e6:.0f} MHz")
        log.info(f"  Thermal noise   : {THERMAL_NOISE_DBM:.1f} dBm")
        log.info(f"  HO hysteresis   : {HANDOVER_HYSTERESIS_DB} dB")

        t_last_sinr_warn = 0.0

        while self._running:
            t0 = time.time()

            self.move_ue(interval_s)
            metrics = self.measure_all()
            self.check_handover(metrics)
            self._log_metrics(metrics)

            # Log current serving BS stats
            if self.current_bs and self.current_bs in metrics:
                m = metrics[self.current_bs]
                log.info(
                    f"UE@({self.ue_pos[0]:.0f},{self.ue_pos[1]:.0f})m "
                    f"| Serving:{self.current_bs} "
                    f"| RSS:{m.rss_dbm:.1f}dBm "
                    f"| SINR:{m.sinr_db:.1f}dB "
                    f"| C:{m.capacity_mbps:.1f}Mbps"
                )
                # Alert transport if SINR too low
                if m.sinr_db < SINR_TARGET_DB:
                    now = time.time()
                    if now - t_last_sinr_warn > 10.0:
                        log.warning(f"SINR {m.sinr_db:.1f}dB < target "
                                    f"{SINR_TARGET_DB}dB — requesting reroute")
                        self._send_reroute_request(
                            f"low SINR {m.sinr_db:.1f}dB on {self.current_bs}")
                        t_last_sinr_warn = now

            # Maintain timing
            elapsed = time.time() - t0
            sleep   = max(0, interval_s - elapsed)
            time.sleep(sleep)

    def stop(self):
        self._running = False
        self._sock.close()


# ── Entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    sim = RANSimulator()

    def input_loop(sim):
        print("\nRAN Simulator interactive mode")
        print("Commands: state | toggle BS1 | toggle BS2 | toggle BS3 | quit\n")
        while True:
            try:
                cmd = input("ran> ").strip()
                if cmd == "state":
                    import pprint
                    pprint.pprint(sim.get_state())
                elif cmd.startswith("toggle "):
                    bs_id = cmd.split()[1].upper()
                    sim.toggle_bs(bs_id)
                elif cmd == "quit":
                    sim.stop()
                    break
            except (EOFError, KeyboardInterrupt):
                sim.stop()
                break

    t = threading.Thread(target=input_loop, args=(sim,), daemon=True)
    t.start()
    sim.run(interval_s=1.0)
