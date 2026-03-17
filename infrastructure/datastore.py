"""
PolyEdge v4 — DataStore
========================
Simple JSON-based persistence for v4 state.
Extracted from main_v4.py.
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional


class DataStore:
    """Simple JSON-based persistence for v4 state."""

    def __init__(self, config):
        self.data_dir = config.DATA_DIR
        os.makedirs(self.data_dir, exist_ok=True)
        self.logger = logging.getLogger("polyedge.store")

    def save(self, key: str, data: dict):
        path = os.path.join(self.data_dir, f"{key}.json")
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            self.logger.error(f"Save error ({key}): {e}")

    def load(self, key: str) -> Optional[dict]:
        path = os.path.join(self.data_dir, f"{key}.json")
        try:
            if os.path.exists(path):
                with open(path) as f:
                    return json.load(f)
        except Exception as e:
            self.logger.error(f"Load error ({key}): {e}")
        return None

    def append_log(self, key: str, entry: dict):
        """Append a timestamped entry to a JSONL log file."""
        path = os.path.join(self.data_dir, f"{key}.jsonl")
        try:
            entry["_ts"] = datetime.now(timezone.utc).isoformat()
            with open(path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            self.logger.error(f"Append log error ({key}): {e}")

    def load_log(self, key: str, last_n: int = 100) -> list:
        """Load last N entries from a JSONL log file."""
        path = os.path.join(self.data_dir, f"{key}.jsonl")
        if not os.path.exists(path):
            return []
        try:
            with open(path) as f:
                lines = f.readlines()
            return [json.loads(line) for line in lines[-last_n:]]
        except Exception as e:
            self.logger.error(f"Load log error ({key}): {e}")
            return []
