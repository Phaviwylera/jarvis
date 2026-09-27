#!/usr/bin/env bash
# ============================================
#  J.A.R.V.I.S. installer for macOS / Linux
# ============================================
set -e
cd "$(dirname "$0")"

echo ""
echo " =========================================="
echo "  J.A.R.V.I.S.  -  macOS / Linux Setup"
echo " =========================================="
echo ""

# Pick python3
if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] python3 not found. Install it first:"
    echo "        macOS : brew install python3"
    echo "        Linux : sudo apt install python3 python3-pip"
    exit 1
fi
echo "[1/3] Python found: $(python3 --version)"

echo ""
echo "[2/3] Installing system audio libraries (may ask for sudo password)..."
if [[ "$OSTYPE" == "darwin"* ]]; then
    if command -v brew >/dev/null 2>&1; then
        brew install portaudio espeak 2>/dev/null || echo "  (brew step skipped/failed - continuing)"
    else
        echo "  Homebrew not found - skipping (voice input may need: brew install portaudio)"
    fi
else
    if command -v apt >/dev/null 2>&1; then
        sudo apt update -y && sudo apt install -y portaudio19-dev espeak ffmpeg \
            python3-pyaudio scrot gnome-screenshot 2>/dev/null \
            || echo "  (apt step had issues - continuing anyway)"
    else
        echo "  Non-Debian distro detected."
        echo "  Please install: portaudio-devel, espeak (names vary by distro)."
    fi
fi

echo ""
echo "[3/3] Installing Python packages..."
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt || {
    echo "[WARN] Full install failed. JARVIS still works in text mode:"
    echo "       python3 jarvis.py --text"
}

echo ""
echo " Setup complete! 🎉"
echo "  Start JARVIS : ./start_jarvis.sh   (or: python3 jarvis.py)"
echo "  Text-only    : python3 jarvis.py --text"
echo ""
