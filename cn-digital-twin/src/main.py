#!/usr/bin/env python3
"""
main.py — Digital Twin Network Orchestrator
────────────────────────────────────────────
Starts all components in the correct order and provides a live
terminal dashboard showing CN metrics.

Components started:
  1. Cross-layer bridge (UDP relay)
  2. RAN simulator (wireless channel model)
  3. Dijkstra controller (SDN routing)
  4. Congestion injector (interactive)
  5. Dashboard server (web view — optional)

Run:
  Without Mininet (simulation mode):  python3 main.py
  With real Mininet (as root):        sudo python3 main.py --mininet
"""

import sys
import os
import time
import threading
import argparse
import json
import signal
import logging
logging.getLogger("BRIDGE").setLevel(logging.WARNING)
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(__file__))  # already there
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # ADD THIS
from config import LOG_DIR, METRICS_FILE
from bridge.ran_transport_bridge import CrossLayerBridge
from ran_layer.ran_simulator     import RANSimulator
from controller.dijkstra_controller import DijkstraController

os.makedirs(LOG_DIR, exist_ok=True)

# ANSI colours
R  = "\033[31m"
G  = "\033[32m"
Y  = "\033[33m"
B  = "\033[34m"
M  = "\033[35m"
C  = "\033[36m"
W  = "\033[37m"
BO = "\033[1m"
RS = "\033[0m"

_last_cmd_result = {"text": "", "ts": 0}
DIVIDER = f"{W}{'─'*72}{RS}"


def clear():
    os.system("clear" if os.name != "nt" else "cls")


def fmt_dbm(v):
    if v > -70:   return f"{G}{v:.1f}{RS}"
    if v > -85:   return f"{Y}{v:.1f}{RS}"
    return f"{R}{v:.1f}{RS}"

def fmt_sinr(v):
    if v > 10:  return f"{G}{v:.1f}{RS}"
    if v > 5:   return f"{Y}{v:.1f}{RS}"
    return f"{R}{v:.1f}{RS}"

def fmt_ms(v):
    if v < 50:   return f"{G}{v:.0f}{RS}"
    if v < 100:  return f"{Y}{v:.0f}{RS}"
    return f"{R}{v:.0f}{RS}"

def fmt_bool(v, true_str="YES", false_str="no"):
    return f"{G}{true_str}{RS}" if v else f"{W}{false_str}{RS}"


# ── Terminal dashboard ─────────────────────────────────────────────────────
def terminal_dashboard(ran_sim, controller, bridge, stop_event):
    """Runs in a thread, redraws terminal every 2s."""
    while not stop_event.is_set():
        time.sleep(5)
        try:
            ran_state  = ran_sim.get_state()
            ctrl_state = controller.status()
            br_state   = bridge.get_state()

            clear()
            print(f"\n{BO}{C}  ╔══════════════════════════════════════════╗{RS}")
            print(f"{BO}{C}  ║   Digital Twin Network — Live Dashboard  ║{RS}")
            print(f"{BO}{C}  ╚══════════════════════════════════════════╝{RS}")
            print(f"  {W}Time: {time.strftime('%H:%M:%S')}   "
                  f"UE position: ({ran_state['ue_position'][0]:.0f}, "
                  f"{ran_state['ue_position'][1]:.0f}) m{RS}\n")

            # ── RAN Layer ────────────────────────────────────────────────
            print(f"{BO}  ── RAN Layer (Wireless){RS}")
            print(DIVIDER)
            print(f"  {'BS':<8} {'RSS (dBm)':<14} {'SINR (dB)':<14} "
                  f"{'Capacity (Mbps)':<18} {'Doppler (Hz)':<14} {'Serving'}")
            print(f"  {'──':<8} {'─────────':<14} {'─────────':<14} "
                  f"{'───────────────':<18} {'────────────':<14} {'───────'}")
            for bs_id, m in ran_state.get("metrics", {}).items():
                is_serving = (bs_id == ran_state["current_bs"])
                active     = ran_state["bs_states"].get(bs_id, {}).get("active", False)
                state_str  = f"{BO}{G}◀ SERVING{RS}" if is_serving else (
                             f"{W}standby{RS}" if active else f"{R}OFFLINE{RS}")
                print(f"  {bs_id:<8} "
                      f"{fmt_dbm(m['rss_dbm']):<22} "
                      f"{fmt_sinr(m['sinr_db']):<22} "
                      f"{m['capacity_mbps']:<18.1f} "
                      f"{m['doppler_hz']:<14.1f} "
                      f"{state_str}")

            # ── Transport Layer ──────────────────────────────────────────
            print(f"\n{BO}  ── Transport Layer (Dijkstra / SDN){RS}")
            print(DIVIDER)
            paths = ctrl_state.get("paths", {})
            lsdb  = ctrl_state.get("lsdb", {})

            print(f"  {BO}Computed paths:{RS}")
            for pair, path in list(paths.items())[:6]:
                path_str = "→".join(path) if path else "unreachable"
                print(f"    {pair:<14} {B}{path_str}{RS}")

            print(f"\n  {BO}Link-state DB (top links):{RS}")
            print(f"  {'Link':<12} {'BW(Mbps)':<10} {'Delay(ms)':<11} "
                  f"{'Cost':<8} {'Congested'}")
            print(f"  {'────':<12} {'────────':<10} {'─────────':<11} "
                  f"{'────':<8} {'─────────'}")
            shown = 0
            for link, rec in lsdb.items():
                if not link.startswith("s") or shown > 7:
                    continue
                cong_str = f"{R}YES{RS}" if rec["congested"] else f"{G}no{RS}"
                print(f"  {link:<12} {rec['bw_mbps']:<10} {rec['delay_ms']:<11} "
                      f"{rec['cost']:<8.1f} {cong_str}")
                shown += 1

            # ── Cross-layer Events ───────────────────────────────────────
            print(f"\n{BO}  ── Cross-Layer Bridge Events (last 5){RS}")
            print(DIVIDER)
            events = br_state.get("events", [])[-5:]
            if not events:
                print(f"  {W}(no events yet){RS}")
            for ev in reversed(events):
                direction = ev["direction"]
                arrow     = f"{M}←→{RS}"
                colour    = G if "COMPLETE" in ev["opcode"] else (
                            R if "FAILURE" in ev["opcode"] else Y)
                print(f"  [{ev['ts']}] {direction:<22} "
                      f"{colour}{ev['opcode']}{RS}")
                      
            # show last command result for 30 seconds
            age = time.time() - _last_cmd_result["ts"]
            if _last_cmd_result["text"] and age < 30:
                print(f"\n  {BO}Last command ({int(age)}s ago):{RS}")
                print(f"  {_last_cmd_result['text']}")

            print(f"\n  {W}Commands: congest s1-s2 | clear | toggle BS1 | "
                  f"move | status | quit{RS}")

        except Exception as e:
            print(f"\n[Dashboard error: {e}]")


# ── Interactive command loop ───────────────────────────────────────────────
def set_result(msg):
    _last_cmd_result["text"] = msg
    _last_cmd_result["ts"]   = time.time()
    print(msg)   # still prints immediately too
    
def command_loop(ran_sim, controller, bridge, stop_event):
    print(f"\n{G}System ready.{RS} Type 'help' for commands.\n")
    while not stop_event.is_set():
        try:
            raw = input()
            cmd = raw.strip().lower()
        except (EOFError, KeyboardInterrupt):
            stop_event.set()
            break

        if cmd == "help":
            print("  congest <s1-s2>   — inject congestion on link")
            print("  clear             — clear all congestion")
            print("  toggle <BS1/2/3>  — toggle base station on/off")
            print("  move <x> <y>      — move UE to position")
            print("  status            — full JSON status dump")
            print("  quit              — exit")

        elif cmd.startswith("congest "):
            link = cmd.split()[1]
            a, b = link.split("-")
            controller.update_link(a, b, util_bps=9_500_000, congested=True)
            set_result(f"{Y}→ Congestion injected on {link}{RS}")


        elif cmd == "clear":
                    from config import TOPOLOGY
                    for src, dst, *_ in TOPOLOGY["links"]:
                        if src.startswith("s") and dst.startswith("s"):
                            controller.update_link(src, dst, util_bps=0, congested=False)
                    set_result(f"{G}→ All congestion cleared{RS}")


        elif cmd.startswith("toggle "):
            bs_id = cmd.split()[1].upper()
            ran_sim.toggle_bs(bs_id)
            set_result(f"{Y}→ Toggled {bs_id}{RS}")

        elif cmd.startswith("move "):
            parts = cmd.split()
            if len(parts) == 3:
                ran_sim.ue_pos = [float(parts[1]), float(parts[2])]
                set_result(f"{G}→ UE moved to ({parts[1]}, {parts[2]}){RS}")

        elif cmd == "status":
            set_result(json.dumps(controller.status(), indent=2))
            set_result(json.dumps(ran_sim.get_state(), indent=2))

        elif cmd in ("quit", "exit", "q"):
            stop_event.set()

        else:
            print(f"Unknown command: {cmd}  (type 'help')")


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Digital Twin Network")
    parser.add_argument("--mininet", action="store_true",
                        help="Launch real Mininet topology (requires root)")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Disable web dashboard")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Don't exec real ovs-ofctl commands")
    args = parser.parse_args()

    stop_event = threading.Event()

    def handle_sig(sig, frame):
        print(f"\n{Y}Shutting down…{RS}")
        stop_event.set()

    signal.signal(signal.SIGINT,  handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    print(f"\n{BO}{C}  Digital Twin Network — CN Mini Project{RS}")
    print(f"  Starting components…\n")

    # 1. Cross-layer bridge
    bridge = CrossLayerBridge()
    bridge.start()
    print(f"  {G}✓{RS} Cross-layer bridge started")

    # 2. RAN simulator
    ran_sim = RANSimulator()
    ran_thread = threading.Thread(
        target=ran_sim.run, kwargs={"interval_s": 1.0}, daemon=True)
    ran_thread.start()
    print(f"  {G}✓{RS} RAN simulator started")

    # 3. Dijkstra controller
    controller = DijkstraController(dry_run=args.dry_run)
    controller.start()
    print(f"  {G}✓{RS} Dijkstra controller started")

    # 4. Optional Mininet
    if args.mininet:
        if os.geteuid() != 0:
            print(f"  {R}✗ --mininet requires root{RS}")
        else:
            from mininet_layer.topology import run as mn_run
            mn_thread = threading.Thread(target=mn_run, daemon=True)
            mn_thread.start()
            print(f"  {G}✓{RS} Mininet topology started")

    # 5. Optional web dashboard
    if not args.no_dashboard:
        try:
            from dashboard.server import DashboardServer
            dashboard = DashboardServer(ran_sim, controller, bridge)
            dash_thread = threading.Thread(
                target=dashboard.run, daemon=True)
            dash_thread.start()
            print(f"  {G}✓{RS} Dashboard at http://localhost:5000")
        except Exception as e:
            print(f"  {Y}⚠{RS}  Dashboard unavailable: {e}")

    print(f"\n  {G}All components running.{RS}")
    time.sleep(1.5)

    # Terminal dashboard in background
    dash_t = threading.Thread(
        target=terminal_dashboard,
        args=(ran_sim, controller, bridge, stop_event),
        daemon=True)
    dash_t.start()

    # Command loop (foreground)
    command_loop(ran_sim, controller, bridge, stop_event)

    print(f"\n{Y}Stopping all components…{RS}")
    ran_sim.stop()
    bridge.stop()
    print(f"{G}Done.{RS}")


if __name__ == "__main__":
    main()
