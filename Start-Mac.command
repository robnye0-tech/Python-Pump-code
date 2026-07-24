#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d "venv" ]; then
  echo "First run — creating a virtual environment and installing dependencies..."
  python3 -m venv venv
  ./venv/bin/pip install -r requirements.txt
fi
./venv/bin/python server.py
