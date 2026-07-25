#!/bin/bash
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 was not found. Install it with your distro's package manager (e.g. 'sudo apt install python3 python3-venv'), then run this again."
  read -p "Press Enter to close..."
  exit 1
fi

if [ ! -f "venv/deps_installed.txt" ]; then
  echo "First run — creating a virtual environment and installing dependencies..."
  if [ ! -d "venv" ]; then
    python3 -m venv venv || { echo "Failed to create the virtual environment."; read -p "Press Enter to close..."; exit 1; }
  fi
  ./venv/bin/pip install -r requirements.txt || { echo "Failed to install dependencies — see the error above."; read -p "Press Enter to close..."; exit 1; }
  echo "installed" > venv/deps_installed.txt
fi

echo "This window auto-restarts Signal Desk if it ever crashes. To fully stop"
echo "it, close this window — closing just the child process is not enough,"
echo "it will relaunch itself."
echo ""

while true; do
  ./venv/bin/python server.py
  echo ""
  echo "Signal Desk stopped (exit code $?) — restarting in 5 seconds."
  echo "Close this window now if you meant to stop it for good."
  sleep 5
done
