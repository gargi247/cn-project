#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup.sh — Environment setup for Digital Twin Network
# Run ONCE as root: sudo bash setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

echo ""
echo "  Digital Twin Network — Setup"
echo "  ──────────────────────────────────────────────"

# ── System packages (Debian/Ubuntu) ──────────────────────────────────────────
echo ""
echo "  [1/4] Installing system packages…"
apt-get update -qq
apt-get install -y -qq \
    mininet \
    openvswitch-switch \
    openvswitch-testcontroller \
    iproute2 \
    net-tools \
    iperf3 \
    iputils-ping \
    tcpdump \
    python3-pip \
    python3-venv \
    python3-tk \
    2>&1 | grep -E "^(Get|Preparing|Unpacking|Setting)" || true

echo "  ✓ System packages installed"

# ── OVS ──────────────────────────────────────────────────────────────────────
echo ""
echo "  [2/4] Starting Open vSwitch…"
service openvswitch-switch start 2>/dev/null || true
ovs-vsctl --version | head -1
echo "  ✓ OVS ready"

# ── Python venv ───────────────────────────────────────────────────────────────
echo ""
echo "  [3/4] Creating Python virtual environment…"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

python3 -m venv venv
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "  ✓ Python environment ready"

# ── Mininet smoke test ────────────────────────────────────────────────────────
echo ""
echo "  [4/4] Mininet smoke test…"
python3 -c "from mininet.net import Mininet; print('  ✓ Mininet import OK')"

# ── Permissions ───────────────────────────────────────────────────────────────
echo ""
echo "  Fixing permissions…"
chmod +x scripts/*.py 2>/dev/null || true
mkdir -p logs

echo ""
echo "  ┌──────────────────────────────────────────┐"
echo "  │  Setup complete!                         │"
echo "  │                                          │"
echo "  │  Demo (no Mininet, no root):             │"
echo "  │    python3 scripts/demo.py               │"
echo "  │                                          │"
echo "  │  Tests:                                  │"
echo "  │    python3 -m pytest tests/ -v           │"
echo "  │                                          │"
echo "  │  Full system (requires root):            │"
echo "  │    sudo python3 src/main.py --mininet    │"
echo "  │                                          │"
echo "  │  Simulation only (no root needed):       │"
echo "  │    python3 src/main.py                   │"
echo "  └──────────────────────────────────────────┘"
echo ""
