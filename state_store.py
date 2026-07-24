"""Simple JSON file persistence, ported from state.js — writes debounced so
a burst of ticks doesn't hammer disk.
"""

import json
import os
import threading

DATA_DIR = os.path.join(os.getcwd(), "data")
DATA_FILE = os.path.join(DATA_DIR, "state.json")

_save_timer: threading.Timer | None = None
_save_lock = threading.Lock()


def load_persisted() -> dict | None:
    if not os.path.exists(DATA_FILE):
        return None
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Could not read saved state, starting fresh: {e}")
        return None


def _write(data: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Failed to save state: {e}")


def save_persisted(data: dict) -> None:
    global _save_timer
    with _save_lock:
        if _save_timer is not None:
            _save_timer.cancel()
        _save_timer = threading.Timer(0.5, _write, args=(data,))
        _save_timer.daemon = True
        _save_timer.start()
