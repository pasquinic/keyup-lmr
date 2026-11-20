"""Persistent edge state: credentials, WireGuard key, cached central config, buffered call logs."""
from __future__ import annotations

import json
import os
import tempfile
import threading


class State:
    def __init__(self, state_dir: str):
        self.path = os.path.join(state_dir, "state.json")
        self.lock = threading.Lock()
        self.data: dict = {}
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.data = json.load(f)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, **kw) -> None:
        with self.lock:
            self.data.update(kw)
            d = os.path.dirname(self.path)
            fd, tmp = tempfile.mkstemp(dir=d)
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    @property
    def enrolled(self) -> bool:
        return bool(self.data.get("device_id") and self.data.get("device_secret"))
