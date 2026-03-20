#!/bin/bash
# Install system dependencies for music_ripper
set -e

echo "Installing system packages..."
sudo apt-get install -y cdparanoia lame libdiscid0 eject libchromaprint-tools

echo "Installing Python dependencies..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
venv/bin/pip install musicbrainzngs discid pyudev mutagen requests

echo ""
echo "Done. Set your Minimax API key before running:"
echo "  export MINIMAX_API_KEY=your_key_here"
echo ""
echo "Run the ripper with:"
echo "  venv/bin/python main.py"
