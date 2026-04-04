#!/usr/bin/env python3
"""
tests/test_cn_algorithms.py
────────────────────────────
Unit tests for all CN algorithm implementations.
Run: python3 -m pytest tests/ -v
"""

import sys
import os
import math
import struct
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# ── Path-loss tests ────────────────────────────────────────────────────────
from ran_layer.ran_simulator import (
    friis_path_loss_db, log_distance_path_loss_db,
    cost231_hata_db, doppler_shift_hz,
    RANSimulator,
)

def test_friis_known_value():
    """
    Friis at 1m, 2.4GHz:
      PL = 20*log10(4*pi*1*2.4e9 / 3e8) = 20*log10(100.53) ≈ 40.05 dB
    """
    pl = friis_path_loss_db(1.0, 2.4e9)
    assert 39.0 < pl < 41.0, f"Friis@1m,2.4GHz = {pl:.2f} (expected ~40dB)"


def test_friis_doubles_with_distance():
    """Friis: doubling distance adds ~6dB (20*log10(2) = 6.02dB)"""
    pl1 = friis_path_loss_db(100, 2.4e9)
    pl2 = friis_path_loss_db(200, 2.4e9)
    delta = pl2 - pl1
    assert 5.9 < delta < 6.2, f"Expected ~6dB, got {delta:.2f}"


def test_log_distance_exponent_effect():
    """Higher path-loss exponent → more loss at same distance."""
    from ran_layer.ran_simulator import log_distance_path_loss_db as ldpl
    pl_n2 = ldpl(500, 2.4e9, n=2.0)
    pl_n4 = ldpl(500, 2.4e9, n=4.0)
    assert pl_n4 > pl_n2, "Higher n must give more path loss"


def test_friis_increases_with_frequency():
    """Higher frequency → more free-space loss."""
    pl_24 = friis_path_loss_db(100, 2.4e9)
    pl_56 = friis_path_loss_db(100, 5.6e9)
    assert pl_56 > pl_24


def test_cost231_reasonable_range():
    """COST-231 at 1km, 1800MHz should be roughly 120–150 dB."""
    pl = cost231_hata_db(1000, 1800)
    assert 110 < pl < 160, f"COST-231 out of range: {pl:.1f}"


def test_doppler_zero_at_perpendicular():
    """Doppler = 0 when moving perpendicular to BS direction (cos π/2 = 0)."""
    fd = doppler_shift_hz(10.0, 2.4e9, angle_rad=math.pi/2)
    assert abs(fd) < 1e-6


def test_doppler_max_toward():
    """Doppler is maximum when heading straight toward BS (angle=0)."""
    fd_toward  = doppler_shift_hz(10, 2.4e9, 0)
    fd_away    = doppler_shift_hz(10, 2.4e9, math.pi)
    fd_side    = doppler_shift_hz(10, 2.4e9, math.pi/2)
    assert abs(fd_toward) > abs(fd_side)
    assert fd_toward > 0 and fd_away < 0   # blue shift vs red shift


# ── SINR and Shannon tests ─────────────────────────────────────────────────
def test_sinr_serving_best():
    """SINR is always highest for the closest BS."""
    sim = RANSimulator()
    sim.ue_pos = [10, 10]   # very close to BS1 at (0,0)
    sim._shadow = {bs: 0.0 for bs in sim.bs_config}
    all_rss = {bs: sim.compute_rss_dbm(bs) for bs in sim.bs_config}
    sinr_bs1 = sim.compute_sinr_db("BS1", all_rss)
    sinr_bs2 = sim.compute_sinr_db("BS2", all_rss)
    assert sinr_bs1 > sinr_bs2, "BS1 should have best SINR when UE is near BS1"


def test_shannon_increases_with_sinr():
    """More SINR → more Shannon capacity."""
    sim = RANSimulator()
    c1  = sim.shannon_capacity_mbps(5,  20e6)
    c2  = sim.shannon_capacity_mbps(15, 20e6)
    assert c2 > c1


def test_shannon_at_0db_sinr():
    """SINR = 0 dB (linear=1) → C = B*log2(2) = B bps."""
    sim = RANSimulator()
    bw  = 20e6
    c   = sim.shannon_capacity_mbps(0, bw)
    expected_mbps = bw / 1e6   # B * 1 bit/Hz
    assert abs(c - expected_mbps) < 0.01, f"Got {c:.2f}, expected {expected_mbps:.2f}"


def test_rss_decreases_with_distance():
    """RSS must decrease as UE moves away from BS."""
    sim = RANSimulator()
    sim._shadow = {bs: 0.0 for bs in sim.bs_config}
    sim.ue_pos = [50, 0]
    rss_near = sim.compute_rss_dbm("BS1")
    sim.ue_pos = [300, 0]
    rss_far  = sim.compute_rss_dbm("BS1")
    assert rss_far < rss_near


# ── Dijkstra tests ─────────────────────────────────────────────────────────
from controller.dijkstra_controller import dijkstra, reconstruct_path, LSDB

def test_dijkstra_simple():
    graph = {
        "A": {"B": 1, "C": 4},
        "B": {"A": 1, "C": 2, "D": 5},
        "C": {"A": 4, "B": 2, "D": 1},
        "D": {"B": 5, "C": 1},
    }
    dist, prev = dijkstra(graph, "A")
    assert dist["D"] == 4   # A→B→C→D = 1+2+1 = 4
    path = reconstruct_path(prev, "A", "D")
    assert path == ["A", "B", "C", "D"]


def test_dijkstra_direct_vs_alternate():
    """Direct path vs alternate with lower weight should pick alternate."""
    graph = {
        "src": {"direct": 100, "via": 1},
        "direct": {"src": 100, "dst": 1},
        "via":    {"src": 1,   "dst": 2},
        "dst":    {"direct": 1,"via": 2},
    }
    dist, prev = dijkstra(graph, "src")
    path = reconstruct_path(prev, "src", "dst")
    assert path == ["src", "via", "dst"]   # 1+2=3 < 100+1=101


def test_dijkstra_congestion_avoidance():
    """Congested link (cost+9999) must be avoided if alternate exists."""
    lsdb = LSDB()
    lsdb.update_utilisation("s1", "s2", util_bps=9_800_000, congested=True)
    graph = lsdb.get_graph()
    dist, prev = dijkstra(graph, "h1")
    path = reconstruct_path(prev, "h1", "h2")
    # s1-s2 is primary; with congestion it should route via s5
    assert "s2" not in path[1:-1] or "s5" in path, \
        f"Expected reroute via s5, got {'→'.join(path)}"


def test_dijkstra_unreachable():
    """Isolated node should give inf distance."""
    graph = {
        "A": {"B": 1},
        "B": {"A": 1},
        "X": {},
    }
    dist, prev = dijkstra(graph, "A")
    assert dist.get("X", math.inf) == math.inf


def test_lsdb_composite_cost():
    """Composite cost must increase when link is congested."""
    lsdb  = LSDB()
    link  = lsdb._db[("s1", "s2")]
    cost_ok = link.composite_cost()
    link.congested = True
    cost_cong = link.composite_cost()
    assert cost_cong > cost_ok + 100


def test_lsdb_down_link_infinite():
    """Down link must have infinite cost."""
    lsdb = LSDB()
    lsdb.mark_link_down("s1", "s2")
    assert math.isinf(lsdb._db[("s1", "s2")].composite_cost())


# ── Bridge protocol tests ─────────────────────────────────────────────────
from bridge.ran_transport_bridge import (
    encode_msg, decode_msg, encode_ack, is_ack, HEADER_SZ,
)
from config import MSG_HANDOVER_COMPLETE, MSG_BS_FAILURE

def test_encode_decode_roundtrip():
    payload = {"new_bs": "BS2", "connected_switch": "s3", "sinr": 12.5}
    raw = encode_msg(MSG_HANDOVER_COMPLETE, 7, payload)
    op, seq, dec = decode_msg(raw)
    assert op  == MSG_HANDOVER_COMPLETE
    assert seq == 7
    assert dec == payload


def test_wire_header_size():
    """Header must be exactly 5 bytes (1+2+2)."""
    assert HEADER_SZ == 5


def test_ack_format():
    ack = encode_ack(42)
    assert is_ack(ack)
    assert struct.unpack_from("!H", ack, 1)[0] == 42


def test_ack_wrong_data():
    assert not is_ack(b"\x00\x00\x00")
    assert not is_ack(b"\xFE\x00\x2A")


def test_empty_payload():
    raw = encode_msg(0x01, 0, {})
    op, seq, payload = decode_msg(raw)
    assert payload == {}


def test_large_payload():
    big = {"data": "x" * 1000, "num": 3.14159, "list": list(range(50))}
    raw = encode_msg(MSG_BS_FAILURE, 999, big)
    op, seq, dec = decode_msg(raw)
    assert dec["num"] == 3.14159
    assert len(dec["list"]) == 50


# ── Run all tests ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import traceback

    tests = [
        test_friis_known_value,
        test_friis_doubles_with_distance,
        test_log_distance_exponent_effect,
        test_friis_increases_with_frequency,
        test_cost231_reasonable_range,
        test_doppler_zero_at_perpendicular,
        test_doppler_max_toward,
        test_sinr_serving_best,
        test_shannon_increases_with_sinr,
        test_shannon_at_0db_sinr,
        test_rss_decreases_with_distance,
        test_dijkstra_simple,
        test_dijkstra_direct_vs_alternate,
        test_dijkstra_congestion_avoidance,
        test_dijkstra_unreachable,
        test_lsdb_composite_cost,
        test_lsdb_down_link_infinite,
        test_encode_decode_roundtrip,
        test_wire_header_size,
        test_ack_format,
        test_ack_wrong_data,
        test_empty_payload,
        test_large_payload,
    ]

    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  \033[32m✓\033[0m  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  \033[31m✗\033[0m  {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
