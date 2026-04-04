#!/usr/bin/env python3
"""
bridge/ran_transport_bridge.py
───────────────────────────────
The Cross-Layer Bridge is the core CN concept of this project.

It implements a real UDP-based signaling protocol between the:
  - RAN layer  (ran_simulator.py)
  - Transport layer / SDN controller  (dijkstra_controller.py)

Protocol design:
  ┌──────────────────────────────────────────────────────┐
  │               Cross-Layer Signal Frame               │
  ├──────────┬──────────┬──────────────────────────────┐ │
  │ opcode   │ seq_no   │ payload_len │ payload (JSON) │ │
  │ (1 byte) │ (2 bytes)│ (2 bytes)  │ (variable)     │ │
  └──────────┴──────────┴────────────┴────────────────┘ │
  └──────────────────────────────────────────────────────┘

Message types and their cross-layer semantics:

  RAN → Transport:
    0x01 CONGESTION_REPORT  — wireless congestion on UE uplink
    0x02 HANDOVER_REQUEST   — UE about to change BS
    0x03 HANDOVER_COMPLETE  — HO done, UE now on new switch
    0x06 BS_FAILURE         — BS went down, reroute NOW

  Transport → RAN:
    0x04 REROUTE_REQUEST    — transport congested, request BS change
    0x05 LINK_STATE_UPDATE  — send path metrics to RAN for awareness

The bridge also maintains:
  - Sequence numbers for reliable delivery (simple stop-and-wait ARQ)
  - RTT measurement for the control plane itself
  - Message logging (every cross-layer event recorded)
  - A shared state dictionary read by the dashboard

CN concepts demonstrated:
  - UDP socket programming (raw, no library)
  - Stop-and-wait ARQ (reliability over UDP)
  - Cross-layer design (OSI layers sharing information)
  - Control plane vs data plane separation

Port layout:
  BRIDGE_PORT     (9999)  — RAN sends here; bridge receives from RAN
  BRIDGE_PORT+1  (10000)  — controller listens here; bridge forwards to it
  BRIDGE_PORT+2  (10001)  — bridge control/query port (dashboard)
"""

import socket
import struct
import json
import time
import threading
import logging
import os
import sys
import collections

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import (
    BRIDGE_HOST, BRIDGE_PORT,
    MSG_CONGESTION_REPORT, MSG_HANDOVER_REQUEST, MSG_HANDOVER_COMPLETE,
    MSG_REROUTE_REQUEST, MSG_LINK_STATE_UPDATE, MSG_BS_FAILURE,
    LOG_DIR,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BRIDGE] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)
os.makedirs(LOG_DIR, exist_ok=True)

OPCODE_NAMES = {
    MSG_CONGESTION_REPORT:  "CONGESTION_REPORT",
    MSG_HANDOVER_REQUEST:   "HANDOVER_REQUEST",
    MSG_HANDOVER_COMPLETE:  "HANDOVER_COMPLETE",
    MSG_REROUTE_REQUEST:    "REROUTE_REQUEST",
    MSG_LINK_STATE_UPDATE:  "LINK_STATE_UPDATE",
    MSG_BS_FAILURE:         "BS_FAILURE",
}

# ── Wire format ────────────────────────────────────────────────────────────
#   Full bridge frame: opcode:1B  seq:2B  payload_len:2B  payload:NB
HEADER_FMT = "!BHH"
HEADER_SZ  = struct.calcsize(HEADER_FMT)   # 5 bytes

#   RAN-side frame (from ran_simulator._send_msg): opcode:1B  payload_len:2B
RAN_HDR_FMT = "!BH"
RAN_HDR_SZ  = struct.calcsize(RAN_HDR_FMT)  # 3 bytes

ACK_FMT    = "!BH"   # 0xFF + seq
ACK_SZ     = struct.calcsize(ACK_FMT)


def encode_msg(opcode, seq, payload_dict):
    """Encode a full 5-byte bridge frame."""
    payload = json.dumps(payload_dict).encode("utf-8")
    header  = struct.pack(HEADER_FMT, opcode, seq, len(payload))
    return header + payload


def decode_msg(data):
    """Decode a full 5-byte bridge frame."""
    if len(data) < HEADER_SZ:
        raise ValueError("Packet too short")
    opcode, seq, plen = struct.unpack_from(HEADER_FMT, data)
    payload_bytes = data[HEADER_SZ: HEADER_SZ + plen]
    payload = json.loads(payload_bytes.decode("utf-8")) if payload_bytes else {}
    return opcode, seq, payload


def decode_ran_msg(data):
    """
    Decode a 3-byte RAN frame (from ran_simulator._send_msg).
    Returns (opcode, payload_dict).
    """
    if len(data) < RAN_HDR_SZ:
        raise ValueError("RAN packet too short")
    opcode, plen = struct.unpack_from(RAN_HDR_FMT, data)
    payload_bytes = data[RAN_HDR_SZ: RAN_HDR_SZ + plen]
    payload = json.loads(payload_bytes.decode("utf-8")) if payload_bytes else {}
    return opcode, payload


def encode_ack(seq):
    return struct.pack(ACK_FMT, 0xFF, seq)


def is_ack(data):
    return len(data) == ACK_SZ and data[0] == 0xFF


# ── Stop-and-wait ARQ sender ───────────────────────────────────────────────
class ARQSender:
    """
    Simple stop-and-wait ARQ over UDP.
    Retransmits up to MAX_RETRIES times with TIMEOUT_S timeout.
    Measures RTT for each successfully acknowledged message.
    """

    TIMEOUT_S   = 0.5
    MAX_RETRIES = 3

    def __init__(self, sock, dst_addr):
        self._sock     = sock
        self._dst      = dst_addr
        self._seq      = 0
        self._lock     = threading.Lock()
        self.rtt_ms_history = collections.deque(maxlen=50)

    def send(self, opcode, payload):
        with self._lock:
            seq  = self._seq & 0xFFFF
            pkt  = encode_msg(opcode, seq, payload)
            name = OPCODE_NAMES.get(opcode, f"0x{opcode:02X}")

            for attempt in range(1, self.MAX_RETRIES + 1):
                t_send = time.time()
                try:
                    self._sock.sendto(pkt, self._dst)
                except OSError as e:
                    log.error(f"Send error: {e}")
                    return False

                # Wait for ACK
                self._sock.settimeout(self.TIMEOUT_S)
                try:
                    ack_data, _ = self._sock.recvfrom(16)
                    if is_ack(ack_data):
                        ack_seq = struct.unpack_from("!H", ack_data, 1)[0]
                        if ack_seq == seq:
                            rtt = (time.time() - t_send) * 1000
                            self.rtt_ms_history.append(rtt)
                            log.debug(f"→ {name} seq={seq} ACKed "
                                      f"RTT={rtt:.1f}ms (attempt {attempt})")
                            self._seq += 1
                            return True
                except socket.timeout:
                    log.warning(f"→ {name} seq={seq} TIMEOUT "
                                f"(attempt {attempt}/{self.MAX_RETRIES})")

            log.error(f"→ {name} seq={seq} FAILED after {self.MAX_RETRIES} retries")
            self._seq += 1
            return False

    def avg_rtt_ms(self):
        if not self.rtt_ms_history:
            return 0.0
        return sum(self.rtt_ms_history) / len(self.rtt_ms_history)


# ── Bridge server (central relay + state store) ───────────────────────────
class CrossLayerBridge:
    """
    Central bridge process.

    Ports:
      BRIDGE_PORT     (9999)  — receives from RAN (3-byte header)
      BRIDGE_PORT+1  (10000)  — forwards to controller (5-byte header)
      BRIDGE_PORT+2  (10001)  — control/query port for dashboard
    """

    def __init__(self):
        self._state = {
            "ran": {},
            "transport": {},
            "events": [],         # last 100 cross-layer events
            "ctrl_rtt_ms": 0.0,
        }
        self._state_lock  = threading.Lock()
        self._msg_log     = os.path.join(LOG_DIR, "bridge_events.jsonl")
        self._running     = False

        # Socket that receives from RAN (binds to BRIDGE_PORT)
        self._sock_from_ran   = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Socket that sends to / receives from controller (binds to BRIDGE_PORT+1)
        self._sock_to_ctrl    = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Socket that receives from controller for RAN forwarding
        self._sock_from_ctrl  = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Control/query port
        self._sock_control    = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self._ctrl_addr = (BRIDGE_HOST, BRIDGE_PORT + 1)  # controller listens here
        self._ran_addr  = None   # set on first RAN packet

        # Sequence counter for messages forwarded to controller
        self._seq = 0
        self._seq_lock = threading.Lock()

    def _next_seq(self):
        with self._seq_lock:
            s = self._seq & 0xFFFF
            self._seq += 1
            return s

    def start(self):
        # Receive from RAN
        self._sock_from_ran.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock_from_ran.bind((BRIDGE_HOST, BRIDGE_PORT))

        # Receive from controller (ctrl → RAN direction)
        self._sock_from_ctrl.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock_from_ctrl.bind((BRIDGE_HOST, BRIDGE_PORT + 3))  # bridge ctrl-recv

        # Control/query port
        self._sock_control.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock_control.bind((BRIDGE_HOST, BRIDGE_PORT + 2))

        self._running = True
        threading.Thread(target=self._recv_from_ran,  daemon=True).start()
        threading.Thread(target=self._recv_from_ctrl, daemon=True).start()
        threading.Thread(target=self._handle_control, daemon=True).start()

        log.info("Cross-layer bridge started")
        log.info(f"  RAN→Bridge:        udp://{BRIDGE_HOST}:{BRIDGE_PORT}")
        log.info(f"  Bridge→Controller: udp://{BRIDGE_HOST}:{BRIDGE_PORT+1}")
        log.info(f"  Control port:      udp://{BRIDGE_HOST}:{BRIDGE_PORT+2}")

    # ── RAN → Controller relay ────────────────────────────────────────────
    def _recv_from_ran(self):
        """
        Receives 3-byte-header messages from RAN, records them,
        re-encodes with 5-byte bridge header, forwards to controller.
        """
        self._sock_from_ran.settimeout(1.0)
        while self._running:
            try:
                data, addr = self._sock_from_ran.recvfrom(4096)
                self._ran_addr = addr

                # Decode RAN's compact format (opcode + plen + payload)
                opcode, payload = decode_ran_msg(data)

                name = OPCODE_NAMES.get(opcode, f"0x{opcode:02X}")
                log.info(f"RAN→BRIDGE: {name} payload={payload}")

                self._record_event("RAN→Transport", opcode, payload)
                self._update_ran_state(opcode, payload)

                # Re-encode with full 5-byte bridge header and forward to controller
                seq = self._next_seq()
                fwd = encode_msg(opcode, seq, payload)
                self._sock_from_ran.sendto(fwd, self._ctrl_addr)
                log.info(f"BRIDGE→CTRL: {name} seq={seq} forwarded")

            except socket.timeout:
                continue
            except Exception as e:
                log.error(f"recv_from_ran: {e}")

    # ── Controller → RAN relay ────────────────────────────────────────────
    def _recv_from_ctrl(self):
        """
        Receives 5-byte-header messages from controller,
        strips to 3-byte format, forwards to RAN.
        """
        self._sock_from_ctrl.settimeout(1.0)
        while self._running:
            try:
                data, addr = self._sock_from_ctrl.recvfrom(4096)

                opcode, seq, payload = decode_msg(data)
                # ACK back to controller
                self._sock_from_ctrl.sendto(encode_ack(seq), addr)

                name = OPCODE_NAMES.get(opcode, f"0x{opcode:02X}")
                log.info(f"CTRL→BRIDGE: {name} seq={seq} payload={payload}")

                self._record_event("Transport→RAN", opcode, payload)
                self._update_transport_state(opcode, payload)

                # Forward to RAN using RAN's compact format
                if self._ran_addr:
                    ran_payload = json.dumps(payload).encode("utf-8")
                    ran_hdr     = struct.pack(RAN_HDR_FMT, opcode, len(ran_payload))
                    self._sock_from_ctrl.sendto(ran_hdr + ran_payload, self._ran_addr)

            except socket.timeout:
                continue
            except Exception as e:
                log.error(f"recv_from_ctrl: {e}")

    # ── Control port: dashboard queries state ─────────────────────────────
    def _handle_control(self):
        self._sock_control.settimeout(1.0)
        while self._running:
            try:
                data, addr = self._sock_control.recvfrom(256)
                cmd = data.decode().strip()
                if cmd == "state":
                    with self._state_lock:
                        resp = json.dumps(self._state).encode()
                    self._sock_control.sendto(resp, addr)
            except socket.timeout:
                continue
            except Exception as e:
                log.error(f"control port: {e}")

    # ── State updates ─────────────────────────────────────────────────────
    def _update_ran_state(self, opcode, payload):
        with self._state_lock:
            if opcode == MSG_HANDOVER_COMPLETE:
                self._state["ran"]["current_bs"]      = payload.get("new_bs")
                self._state["ran"]["connected_switch"] = payload.get("connected_switch")
            elif opcode == MSG_BS_FAILURE:
                self._state["ran"]["failed_bs"] = payload.get("bs_id")
            elif opcode == MSG_REROUTE_REQUEST:
                self._state["ran"]["last_reroute_reason"] = payload.get("reason")

    def _update_transport_state(self, opcode, payload):
        with self._state_lock:
            if opcode == MSG_LINK_STATE_UPDATE:
                self._state["transport"]["active_path"] = payload.get("path", [])
                self._state["transport"]["tx_ms"]       = payload.get("tx_ms", 0)
                self._state["transport"]["congested"]   = payload.get("congested", [])

    def _record_event(self, direction, opcode, payload):
        event = {
            "ts":        time.strftime("%H:%M:%S"),
            "direction": direction,
            "opcode":    OPCODE_NAMES.get(opcode, f"0x{opcode:02X}"),
            "payload":   payload,
        }
        with self._state_lock:
            self._state["events"].append(event)
            if len(self._state["events"]) > 100:
                self._state["events"].pop(0)
        with open(self._msg_log, "a") as f:
            f.write(json.dumps(event) + "\n")

    def get_state(self):
        with self._state_lock:
            return dict(self._state)

    def stop(self):
        self._running = False
        self._sock_from_ran.close()
        self._sock_from_ctrl.close()
        self._sock_control.close()


# ── Bridge query client (used by dashboard) ────────────────────────────────
def query_bridge_state():
    """Send 'state' to bridge control port, return dict."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.0)
    try:
        s.sendto(b"state", (BRIDGE_HOST, BRIDGE_PORT + 2))
        data, _ = s.recvfrom(65535)
        return json.loads(data.decode())
    except Exception as e:
        return {"error": str(e)}
    finally:
        s.close()


# ── Entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    bridge = CrossLayerBridge()
    bridge.start()
    log.info("Bridge running — Ctrl+C to stop")
    try:
        while True:
            time.sleep(5)
            state = bridge.get_state()
            log.info(f"Bridge state: RAN={state.get('ran')} "
                     f"TRANSPORT={state.get('transport')} "
                     f"events={len(state.get('events', []))}")
    except KeyboardInterrupt:
        bridge.stop()
        log.info("Bridge stopped")