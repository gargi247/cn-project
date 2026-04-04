# Digital Twin Network — CN Mini Project

A multi-layer network digital twin implementing real CN algorithms.
**No simulation library dependency** — all algorithms from scratch.

---

## CN Concepts Implemented

### 1. Path-Loss Models (`ran_layer/ran_simulator.py`)

Three standard wireless path-loss models:

**Friis free-space:**
```
PL(dB) = 20·log₁₀(4·π·d·f / c)
```
Valid for line-of-sight, far field. Exponent = 2.

**Log-distance (urban):**
```
PL(d) = PL(d₀) + 10·n·log₁₀(d/d₀)   n = 3.5 (urban)
```
Reference distance d₀ = 1m. Models obstructions and reflections.

**COST-231 Hata (macro-cell):**
```
PL = A + B·log₁₀(d_km) - C
A = 46.3 + 33.9·log₁₀(f) - 13.82·log₁₀(h_BS) - a(h_m)
```
3GPP-standard empirical model for 1500–2000 MHz band.

---

### 2. RSS, SINR, and Shannon Capacity

**Received Signal Strength:**
```
RSS = P_tx + G_tx + G_rx - PL(d) - shadow_fading
```

**SINR (Signal-to-Interference-plus-Noise Ratio):**
```
SINR = S / (I + N)
  S = signal from serving BS (mW)
  I = sum of interference from all other active BSs
  N = thermal noise = kTB·NF = -174 + 10·log₁₀(BW) + NF  dBm
```

**Shannon–Hartley theorem:**
```
C = B · log₂(1 + SINR_linear)   [bps]
```
20 MHz channel @ SINR = 20 dB → C ≈ 133 Mbps (theoretical max).

---

### 3. Handover Management (3GPP A3 Event)

Handover is triggered when:
```
RSS(neighbour) - RSS(serving) > hysteresis   (3 dB)
```
AND this condition holds for **time-to-trigger (TTT = 40ms)**.

The TTT prevents ping-pong handovers (oscillating between two BSs).

---

### 4. Doppler Shift

For a UE moving at velocity `v` toward a BS at frequency `f`:
```
f_d = (v/c) · f · cos(θ)
```
At 2.4 GHz, pedestrian speed (5 m/s):
```
f_d = (5/3×10⁸) × 2.4×10⁹ × cos(0) ≈ 40 Hz
```

---

### 5. Dijkstra with Composite Metric (`controller/dijkstra_controller.py`)

Standard Dijkstra's algorithm on a weighted directed graph.

**Composite cost per link:**
```
cost = delay_ms + α·(100/bw_mbps) + β·congestion_penalty + loss_pct·10
```
- `α = 0.3` — BW normalised to 100 Mbps baseline
- `β = 1.0`
- `congestion_penalty = 9999` — effectively removes congested links

**Link-State Database (LSDB)** updated by:
- `/proc/net/dev` polling (real Mininet mode)
- RTT probing via ICMP
- Cross-layer signals from RAN

---

### 6. Cross-Layer Design (`bridge/ran_transport_bridge.py`)

The bridge implements a **UDP-based control protocol** between OSI layers.

**Wire format:**
```
┌─────────┬──────────┬─────────────┬────────────────┐
│ opcode  │ seq_no   │ payload_len │ payload (JSON) │
│ 1 byte  │ 2 bytes  │ 2 bytes     │ variable       │
└─────────┴──────────┴─────────────┴────────────────┘
```

**Stop-and-wait ARQ:**
- Sender retransmits after 500ms timeout (up to 3×)
- Receiver sends ACK: `[0xFF][seq:2B]`
- Sequence numbers prevent duplicate processing

**Cross-layer messages:**
| Opcode | Direction | Meaning |
|--------|-----------|---------|
| 0x01 | RAN → Transport | Wireless congestion report |
| 0x02 | Transport → RAN | Request handover |
| 0x03 | RAN → Transport | Handover complete, use new switch |
| 0x04 | RAN → Transport | SINR low, request Dijkstra reroute |
| 0x05 | Transport → RAN | Path metrics (RTT, loss) |
| 0x06 | RAN → Transport | BS failure — immediate reroute |

---

### 7. Mininet Topology (`mininet_layer/topology.py`)

```
h1 ──(100M,1ms)── s1 ──(10M,10ms)── s2 ──(10M,10ms)── s3 ──(100M,1ms)── h3
                   │                  │                  │
              (5M,20ms)          (5M,15ms)          (5M,20ms)
                   │                  │                  │
                   s5 ───────────────────────────────────┘
                   │
              (100M,1ms)
                   │
                  h4
```

TCLink parameters emulate real WAN conditions:
- Bandwidth: `tc tbf rate Xmbit`
- Delay:     `tc netem delay Xms`
- Loss:      `tc netem loss X%`
- Queue:     `tc tbf burst Xkbit latency Xms`

Congestion injection uses `tc netem + tbf` stacked qdiscs.

---

## Directory Structure

```
cn-digital-twin/
├── src/
│   ├── config.py                    ← All CN parameters
│   ├── main.py                      ← Orchestrator
│   ├── mininet_layer/
│   │   └── topology.py              ← Mininet topo + TCLink + congestion injection
│   ├── ran_layer/
│   │   └── ran_simulator.py         ← Path loss, SINR, Shannon, handover
│   ├── controller/
│   │   └── dijkstra_controller.py   ← LSDB, Dijkstra, flow install, ARQ
│   ├── bridge/
│   │   └── ran_transport_bridge.py  ← UDP cross-layer protocol
│   └── dashboard/
│       └── server.py                ← Metrics viewer
├── scripts/
│   └── demo.py                      ← Standalone CN algorithm demo
├── tests/
│   └── test_cn_algorithms.py        ← 23 unit tests
├── requirements.txt
├── setup.sh
└── README.md
```

---

## Running

### Demo (no Mininet, no root needed)
```bash
python3 scripts/demo.py
```
Shows all algorithms with real computed values.

### Unit tests
```bash
python3 -m pytest tests/ -v
# or
python3 tests/test_cn_algorithms.py
```

### Full system (simulation, no Mininet)
```bash
python3 src/main.py
```
Interactive commands: `congest s1-s2`, `toggle BS1`, `move 400 200`, `status`

### With real Mininet (requires root + Ubuntu)
```bash
sudo bash setup.sh         # once
sudo python3 src/main.py --mininet
```

### curl API
```bash
curl http://localhost:5000/api/state
curl http://localhost:5000/api/metrics/ran
curl http://localhost:5000/api/events
```

---

## What to Show in Your Demo

1. **Run `demo.py`** — shows all calculations with real numbers
2. **Run tests** — proves algorithms are correct
3. **Run `main.py`** — interactive: inject congestion, watch Dijkstra reroute,
   toggle BS and watch RAN→Transport cross-layer signal trigger path recompute
4. **Show logs/** — CSV files with timestamped RSS/SINR/capacity metrics
5. **Explain the protocol** — 5-byte header, ARQ, sequence numbers

---

## Key Files to Explain to Examiner

| File | CN Concept |
|------|-----------|
| `ran_simulator.py` lines 50–120 | Friis, log-distance, COST-231 path loss |
| `ran_simulator.py` lines 160–190 | SINR with interference sum |
| `ran_simulator.py` lines 195–205 | Shannon–Hartley theorem |
| `ran_simulator.py` lines 220–260 | 3GPP A3 handover with TTT |
| `dijkstra_controller.py` lines 50–100 | Composite cost, LSDB |
| `dijkstra_controller.py` lines 105–135 | Dijkstra implementation |
| `ran_transport_bridge.py` lines 45–75 | Wire protocol encoding |
| `ran_transport_bridge.py` lines 80–130 | Stop-and-wait ARQ |
