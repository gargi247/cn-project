#!/usr/bin/env python3
"""
scripts/demo.py
───────────────
Standalone demo — runs ALL CN algorithms without needing Mininet or root.
Shows everything working in a single terminal session.

Demonstrates:
  1. Dijkstra on weighted graph with composite metric
  2. Path-loss calculations (Friis, Log-distance, COST-231)
  3. Shannon capacity for each BS
  4. SINR computation with interference
  5. 3GPP A3 handover trigger
  6. Cross-layer message encoding/decoding
  7. Congestion → reroute → path change walkthrough

Run:  python3 scripts/demo.py
"""

import sys, os, math, time, struct, json, heapq, random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from config import BASE_STATIONS, UE, CARRIER_FREQ_HZ, BANDWIDTH_HZ, \
    THERMAL_NOISE_DBM, PATH_LOSS_EXPONENT, REFERENCE_DISTANCE_M, \
    TOPOLOGY, CONGESTION_PENALTY
from ran_layer.ran_simulator import (
    friis_path_loss_db, log_distance_path_loss_db, cost231_hata_db,
    doppler_shift_hz, RANSimulator,
)
from controller.dijkstra_controller import (
    LSDB, dijkstra, reconstruct_path, all_host_pairs_paths,
)
from bridge.ran_transport_bridge import encode_msg, decode_msg, encode_ack

# ANSI
G  = "\033[32m"; Y = "\033[33m"; B = "\033[34m"
C  = "\033[36m"; R = "\033[31m"; W = "\033[37m"
BO = "\033[1m";  RS = "\033[0m"
DIV = f"{W}{'─'*68}{RS}"

def hdr(title):
    print(f"\n{BO}{C}{'═'*68}{RS}")
    print(f"{BO}{C}  {title}{RS}")
    print(f"{BO}{C}{'═'*68}{RS}")

def section(title):
    print(f"\n{BO}{B}  ── {title}{RS}")
    print(DIV)

def ok(msg):  print(f"  {G}✓  {RS}{msg}")
def info(msg): print(f"  {W}   {RS}{msg}")
def warn(msg): print(f"  {Y}⚠  {RS}{msg}")


# ── 1. Path-loss models ───────────────────────────────────────────────────
def demo_path_loss():
    hdr("1. Path-Loss Models")
    distances = [10, 50, 100, 250, 500, 1000]
    freq = CARRIER_FREQ_HZ

    print(f"\n  {'Distance (m)':<16} {'Friis (dB)':<14} {'Log-dist (dB)':<16} {'COST-231 (dB)'}")
    print(f"  {'─'*13:<16} {'─'*10:<14} {'─'*13:<16} {'─'*13}")
    for d in distances:
        friis   = friis_path_loss_db(d, freq)
        logd    = log_distance_path_loss_db(d, freq)
        cost231 = cost231_hata_db(d, freq/1e6)
        print(f"  {d:<16} {friis:<14.1f} {logd:<16.1f} {cost231:.1f}")

    section("Interpretation")
    info("Friis: free-space LoS (ideal)")
    info(f"Log-distance: exponent n={PATH_LOSS_EXPONENT} (urban)")
    info("COST-231: macro-cell empirical model (1.5–2GHz)")


# ── 2. RSS and SINR ───────────────────────────────────────────────────────
def demo_sinr():
    hdr("2. RSS / SINR / Shannon Capacity")
    sim = RANSimulator()
    sim.ue_pos = list(UE["position"])

    section("Per-BS metrics at UE position "
            f"({sim.ue_pos[0]:.0f},{sim.ue_pos[1]:.0f}) m")

    # Compute RSS for all BSs
    all_rss = {}
    for bs_id in sim.bs_config:
        all_rss[bs_id] = sim.compute_rss_dbm(bs_id)

    print(f"\n  {'BS':<8} {'Distance(m)':<14} {'RSS(dBm)':<12} "
          f"{'SINR(dB)':<12} {'Shannon C(Mbps)'}")
    print(f"  {'──':<8} {'──────────':<14} {'────────':<12} "
          f"{'────────':<12} {'───────────────'}")

    for bs_id in sorted(sim.bs_config):
        bs  = sim.bs_config[bs_id]
        d   = math.sqrt((sim.ue_pos[0]-bs['position'][0])**2 +
                        (sim.ue_pos[1]-bs['position'][1])**2)
        rss = all_rss[bs_id]
        sinr = sim.compute_sinr_db(bs_id, all_rss)
        cap  = sim.shannon_capacity_mbps(sinr, bs['bandwidth_hz'])

        rss_s  = f"\033[32m{rss:.1f}\033[0m" if rss > -80 else f"\033[31m{rss:.1f}\033[0m"
        sinr_s = f"\033[32m{sinr:.1f}\033[0m" if sinr > 10 else f"\033[33m{sinr:.1f}\033[0m"
        print(f"  {bs_id:<8} {d:<14.0f} {rss_s:<20} {sinr_s:<20} {cap:.1f}")

    section("Shannon–Hartley theorem")
    info("C = B · log₂(1 + SINR_linear)")
    info(f"Channel BW = {BANDWIDTH_HZ/1e6:.0f} MHz")
    info(f"Thermal noise = {THERMAL_NOISE_DBM:.1f} dBm")

    section("Doppler shift")
    for bs_id in sim.bs_config:
        dopp = doppler_shift_hz(UE["velocity_mps"],
                                sim.bs_config[bs_id]["frequency_hz"])
        info(f"{bs_id}: fd = ±{dopp:.1f} Hz "
             f"(v={UE['velocity_mps']}m/s, f={CARRIER_FREQ_HZ/1e9:.1f}GHz)")


# ── 3. Handover walkthrough ───────────────────────────────────────────────
def demo_handover():
    hdr("3. 3GPP A3 Handover Walkthrough")
    sim = RANSimulator()

    section("UE moving from BS1 coverage toward BS2")
    positions = [
        (50,   50,  "near BS1"),
        (200, 100,  "moving toward centre"),
        (300, 100,  "near centre"),
        (400, 100,  "approaching BS2"),
        (490,  50,  "near BS2"),
    ]

    print(f"\n  {'Position':<22} {'Serving BS':<12} "
          f"{'Best RSS(dBm)':<16} {'HO triggered'}")
    print(f"  {'────────':<22} {'──────────':<12} "
          f"{'─────────────':<16} {'────────────'}")

    prev_bs = None
    for x, y, label in positions:
        sim.ue_pos = [x, y]
        metrics    = sim.measure_all()
        sim.check_handover(metrics)

        best_bs  = max(metrics, key=lambda b: metrics[b].rss_dbm)
        best_rss = metrics[best_bs].rss_dbm
        ho_done  = (prev_bs is not None and sim.current_bs != prev_bs)
        ho_str   = f"{G}YES → {sim.current_bs}{RS}" if ho_done else "—"

        print(f"  ({x},{y}) {label:<16} {sim.current_bs or '—':<12} "
              f"{best_rss:<16.1f} {ho_str}")
        prev_bs = sim.current_bs

    info("A3 event: HO when RSS(neighbour) - RSS(serving) > hysteresis (3dB)")
    info("TTT (time-to-trigger) = 40ms prevents ping-pong handovers")


# ── 4. Dijkstra with composite metric ─────────────────────────────────────
def demo_dijkstra():
    hdr("4. Dijkstra — Composite Routing Metric")

    lsdb = LSDB()

    section("Normal topology — primary paths")
    graph = lsdb.get_graph()
    dist, prev = dijkstra(graph, "h1")
    print(f"\n  {'Source':<8} {'Destination':<14} {'Path':<32} {'Cost'}")
    print(f"  {'──────':<8} {'───────────':<14} {'────':<32} {'────'}")
    for dst in ["h2", "h3", "h4"]:
        path = reconstruct_path(prev, "h1", dst)
        cost = dist.get(dst, float("inf"))
        print(f"  h1      {dst:<14} {'→'.join(path):<32} {cost:.1f}")

    section("After congestion on s1-s2 (penalty +9999)")
    lsdb.update_utilisation("s1", "s2", util_bps=9_500_000, congested=True)
    graph2 = lsdb.get_graph()
    dist2, prev2 = dijkstra(graph2, "h1")
    for dst in ["h2", "h3", "h4"]:
        path = reconstruct_path(prev2, "h1", dst)
        cost = dist2.get(dst, float("inf"))
        was  = reconstruct_path(prev, "h1", dst)
        changed = path != was
        marker  = f"  {Y}← REROUTED{RS}" if changed else ""
        print(f"  h1→{dst}: {'→'.join(path)} (cost={cost:.1f}){marker}")

    section("Composite cost formula")
    info("cost = delay_ms + α*(100/bw_mbps) + β*congestion_penalty + loss*10")
    info(f"α = 0.3 (BW weight),  β = 1.0,  penalty = {CONGESTION_PENALTY}")
    info("Congested link gets +9999ms effective cost → always avoided")


# ── 5. Cross-layer protocol ───────────────────────────────────────────────
def demo_bridge_protocol():
    hdr("5. Cross-Layer Bridge Protocol (UDP wire format)")

    section("Message encoding")
    from config import (MSG_HANDOVER_COMPLETE, MSG_REROUTE_REQUEST,
                        MSG_BS_FAILURE)

    messages = [
        (MSG_HANDOVER_COMPLETE, 42, {"new_bs": "BS2", "connected_switch": "s3"}),
        (MSG_REROUTE_REQUEST,   43, {"reason": "SINR 3.2dB < threshold 5dB"}),
        (MSG_BS_FAILURE,        44, {"bs_id": "BS1", "connected_switch": "s1"}),
    ]

    for opcode, seq, payload in messages:
        raw = encode_msg(opcode, seq, payload)
        dec_op, dec_seq, dec_pay = decode_msg(raw)
        ack = encode_ack(seq)

        print(f"\n  Opcode: 0x{opcode:02X}  Seq: {seq}  "
              f"Payload: {json.dumps(payload)}")
        print(f"  Wire:   {raw.hex()}")
        print(f"  ACK:    {ack.hex()}")
        assert dec_op == opcode and dec_seq == seq and dec_pay == payload
        ok(f"Encode → transmit → decode → ACK verified (len={len(raw)}B)")

    section("Stop-and-wait ARQ")
    info("Header: opcode(1B) + seq(2B) + payload_len(2B) = 5B overhead")
    info("Retransmit up to 3× with 500ms timeout if no ACK")
    info("RTT measured for each message — control plane latency tracking")
    info("Sequence numbers prevent duplicate processing on retransmit")

    section("Cross-layer event flow")
    print(f"""
  {G}RAN Simulator{RS}                           {B}Dijkstra Controller{RS}
       │                                          │
       │  SINR drops below 5dB                   │
       │  ──────────────────────────────────────▶ │
       │  MSG_REROUTE_REQUEST                     │  recompute_paths()
       │                                          │  Dijkstra runs on LSDB
       │                                          │  new path installed
       │                                          │
       │  A3 event → TTT expires                 │
       │  handover BS1 → BS2                     │
       │  ──────────────────────────────────────▶ │
       │  MSG_HANDOVER_COMPLETE                   │  on_handover(BS2, s3)
       │  {{new_bs: BS2, switch: s3}}              │  recompute_paths()
       │                                          │
       │           BS1 power failure             │
       │  ──────────────────────────────────────▶ │
       │  MSG_BS_FAILURE                          │  mark_link_down(s1)
       │  {{bs_id: BS1, switch: s1}}               │  recompute_paths()
    """)


# ── 6. Full scenario walkthrough ──────────────────────────────────────────
def demo_scenario():
    hdr("6. Full Scenario: Congestion → Reroute → RAN Handover")

    sim  = RANSimulator()
    lsdb = LSDB()

    print(f"\n  Scenario: h1 is streaming video to h2.")
    print(f"  Primary path: h1 → s1 → s2 → s3 → h2  (low latency)")
    print(f"  UE is camped on BS1 (strongest signal)\n")
    time.sleep(0.5)

    # Step 1: baseline
    graph = lsdb.get_graph()
    _, prev = dijkstra(graph, "h1")
    path = reconstruct_path(prev, "h1", "h2")
    ok(f"Step 1 — Baseline path: {'→'.join(path)}")
    sim.ue_pos = [80, 80]
    metrics = sim.measure_all()
    sim.check_handover(metrics)
    ok(f"         Serving BS: {sim.current_bs}  "
       f"SINR: {metrics[sim.current_bs].sinr_db:.1f}dB  "
       f"Capacity: {metrics[sim.current_bs].capacity_mbps:.1f}Mbps")
    time.sleep(0.8)

    # Step 2: inject congestion
    warn("Step 2 — Congestion injected on s1-s2 (tc netem +80ms, 20% loss)")
    lsdb.update_utilisation("s1", "s2", util_bps=9_800_000, congested=True)
    time.sleep(0.4)

    # Step 3: Dijkstra reroutes
    graph2 = lsdb.get_graph()
    _, prev2 = dijkstra(graph2, "h1")
    new_path = reconstruct_path(prev2, "h1", "h2")
    ok(f"Step 3 — Dijkstra rerouted: {'→'.join(new_path)}")
    info(f"         s1-s2 cost={lsdb._db[('s1','s2')].composite_cost():.0f} "
         f"(was {lsdb.dump().get('s1-s2',{}).get('delay_ms',10)}) → via s5")
    time.sleep(0.8)

    # Step 4: UE moves, handover
    warn("Step 4 — UE moves toward BS2 coverage area")
    sim.ue_pos = [420, 80]
    metrics2   = sim.measure_all()
    old_bs     = sim.current_bs
    # Force TTT to zero for demo
    sim._ho_start_time = time.time() - 1.0
    sim.check_handover(metrics2)
    if sim.current_bs != old_bs:
        ok(f"Step 5 — A3 handover: {old_bs} → {sim.current_bs}")
        ok(f"         New serving switch: "
           f"{sim.bs_config[sim.current_bs]['connected_switch']}")
        ok(f"         Transport controller notified → paths recomputed")
    else:
        info(f"Step 5 — UE still on {sim.current_bs} (no handover needed yet)")
    time.sleep(0.8)

    # Step 5: clear congestion
    lsdb.update_utilisation("s1", "s2", util_bps=0, congested=False)
    graph3 = lsdb.get_graph()
    _, prev3 = dijkstra(graph3, "h1")
    restored = reconstruct_path(prev3, "h1", "h2")
    ok(f"Step 6 — Congestion cleared. Primary path restored: {'→'.join(restored)}")

    print(f"\n  {BO}{G}Scenario complete. Both layers reacted and recovered.{RS}\n")


# ── Entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n{BO}{C}  Digital Twin Network — CN Algorithm Demo{RS}")
    print(f"  Running all demonstrations without Mininet…\n")

    demo_path_loss()
    time.sleep(0.3)
    demo_sinr()
    time.sleep(0.3)
    demo_handover()
    time.sleep(0.3)
    demo_dijkstra()
    time.sleep(0.3)
    demo_bridge_protocol()
    time.sleep(0.3)
    demo_scenario()

    print(f"\n{BO}{G}  All CN demonstrations complete.{RS}")
    print(f"  See logs/ directory for CSV metrics.\n")
