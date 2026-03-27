#!/bin/bash
# Digital Twin Network - Setup and Quick Start Script

set -e  # Exit on error

echo "=================================================="
echo "Digital Twin Network - Phase 1 Setup"
echo "=================================================="
echo ""

# Check for Python
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is not installed"
    exit 1
fi

echo "✓ Python 3 found: $(python3 --version)"

# Check for pip
if ! command -v pip3 &> /dev/null; then
    echo "Error: pip3 is not installed"
    exit 1
fi

echo "✓ pip3 found"

# Install Python dependencies
echo ""
echo "Installing Python dependencies..."
pip3 install -r requirements.txt

# Initialize database
echo ""
echo "Initializing database..."
python3 data_layer/storage.py --init --db dtn_network.db

echo ""
echo "=================================================="
echo "Setup Complete!"
echo "=================================================="
echo ""
echo "Next Steps:"
echo ""
echo "1. Start the Mininet topology (requires sudo):"
echo "   sudo python3 physical_network/topology_builder.py"
echo ""
echo "2. In a new terminal, start data collection:"
echo "   python3 data_layer/collector.py"
echo ""
echo "3. In a new terminal, start the dashboard:"
echo "   python3 dashboard/app.py"
echo ""
echo "4. In a new terminal, start the sync engine:"
echo "   python3 twin_core/sync_engine.py"
echo ""
echo "5. Access the dashboard:"
echo "   http://localhost:5000"
echo ""
echo "=================================================="
echo ""
echo "For more information, see README.md"
echo ""
