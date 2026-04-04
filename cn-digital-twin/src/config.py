"""
config.py — All CN parameters for the Digital Twin Network.
Centralised so every module reads from here.
"""

# ── Mininet topology ───────────────────────────────────────────────────────
# Link parameters passed directly to Mininet TCLink
TOPOLOGY = {
    "hosts": ["h1", "h2", "h3", "h4"],
    "switches": ["s1", "s2", "s3", "s4", "s5"],
    "links": [
        # (src, dst, bw_mbps, delay_ms, loss_pct, max_queue_pkts)
        ("h1", "s1",  100,  1,  0,   100),
        ("h2", "s2",  100,  1,  0,   100),
        ("h3", "s4",  100,  1,  0,   100),
        ("h4", "s5",  100,  1,  0,   100),
        ("s1", "s2",   10, 10,  0,    50),   # primary backbone
        ("s2", "s3",   10, 10,  0,    50),   # primary backbone
        ("s3", "s4",   10, 10,  0,    50),   # primary backbone
        ("s1", "s5",    5, 20,  0,    50),   # alternate path 1
        ("s5", "s3",    5, 20,  0,    50),   # alternate path 1
        ("s2", "s4",    5, 15,  0,    50),   # alternate path 2
    ],
}

# ── Dijkstra / routing ─────────────────────────────────────────────────────
CONGESTION_THRESHOLD_BPS   = 8_000_000     # 8 Mbps — link considered congested
CONGESTION_PENALTY         = 9999          # cost added to congested link in Dijkstra
PROBE_INTERVAL_S           = 2.0           # how often congestion monitor probes
RTT_CONGESTION_THRESHOLD_MS = 50.0         # RTT spike = congestion signal
LINK_COST_METRIC           = "delay"       # "delay" | "bw" | "composite"

# ── RAN / wireless ─────────────────────────────────────────────────────────
CARRIER_FREQ_HZ    = 2.4e9          # 2.4 GHz (WiFi / LTE band)
TRANSMIT_POWER_DBM = 23.0           # typical UE TX power
NOISE_FIGURE_DB    = 7.0            # receiver noise figure
BANDWIDTH_HZ       = 20e6           # 20 MHz channel (LTE standard)
THERMAL_NOISE_DBM  = -174 + 10 * __import__('math').log10(BANDWIDTH_HZ) + NOISE_FIGURE_DB

BASE_STATIONS = {
    "BS1": {
        "position": (0.0, 0.0),        # metres (x, y)
        "tx_power_dbm": 43.0,          # eNodeB TX power
        "antenna_gain_dbi": 15.0,
        "frequency_hz": 2.4e9,
        "bandwidth_hz": 20e6,
        "connected_switch": "s1",
    },
    "BS2": {
        "position": (500.0, 0.0),
        "tx_power_dbm": 43.0,
        "antenna_gain_dbi": 15.0,
        "frequency_hz": 2.4e9,
        "bandwidth_hz": 20e6,
        "connected_switch": "s3",
    },
    "BS3": {
        "position": (250.0, 433.0),    # equilateral triangle
        "tx_power_dbm": 40.0,
        "antenna_gain_dbi": 12.0,
        "frequency_hz": 2.4e9,
        "bandwidth_hz": 20e6,
        "connected_switch": "s2",
    },
}

UE = {
    "position": (250.0, 150.0),       # starts near centre
    "rx_antenna_gain_dbi": 0.0,       # omnidirectional UE
    "velocity_mps": 5.0,              # pedestrian speed for Doppler
}

# Path-loss model: "friis" | "log_distance" | "cost231"
PATH_LOSS_MODEL = "log_distance"
PATH_LOSS_EXPONENT = 3.5              # urban environment
REFERENCE_DISTANCE_M = 1.0
SHADOW_FADING_STD_DB = 8.0           # log-normal shadowing std dev

# Handover thresholds (3GPP A3 event)
HANDOVER_HYSTERESIS_DB = 3.0         # dB margin to prevent ping-pong
HANDOVER_TTT_S         = 0.04        # time-to-trigger (40 ms)
SINR_TARGET_DB         = 5.0         # minimum acceptable SINR

# ── Cross-layer bridge ─────────────────────────────────────────────────────
BRIDGE_HOST     = "127.0.0.1"
BRIDGE_PORT     = 9999
BRIDGE_PROTOCOL = "UDP"              # lightweight signaling

# Message types (1 byte opcode)
MSG_CONGESTION_REPORT   = 0x01  # RAN → Transport: tell transport about bad link
MSG_HANDOVER_REQUEST    = 0x02  # Transport → RAN: request handover
MSG_HANDOVER_COMPLETE   = 0x03  # RAN → Transport: handover done, use new switch
MSG_REROUTE_REQUEST     = 0x04  # RAN → Transport: trigger Dijkstra reroute
MSG_LINK_STATE_UPDATE   = 0x05  # Transport → RAN: current path metrics
MSG_BS_FAILURE          = 0x06  # RAN → Transport: BS went down

# ── Monitoring / logging ───────────────────────────────────────────────────
LOG_DIR         = "logs"
METRICS_FILE    = "logs/metrics.csv"
STATE_FILE      = "logs/state.json"
LOG_LEVEL       = "INFO"

# ── Dashboard (minimal, just reads log files) ──────────────────────────────
DASHBOARD_HOST  = "0.0.0.0"
DASHBOARD_PORT  = 5000
