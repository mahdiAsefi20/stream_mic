#!/usr/bin/env bash
# Build/test helper. The real .exe must be built ON Windows (build_windows.bat),
# but this lets you sanity-check the GUI on Linux/macOS.
set -e
cd "$(dirname "$0")"
python3 -m py_compile nevisar_mic_gui.py && echo "[OK] syntax OK"
if command -v python3 >/dev/null && python3 -c "import tkinter" 2>/dev/null; then
  echo "[*] Launching GUI (close window to finish)..."
  python3 nevisar_mic_gui.py
else
  echo "[!] tkinter not available here; test on Windows with: python nevisar_mic_gui.py"
fi
