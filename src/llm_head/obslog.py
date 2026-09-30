"""Structured JSON event log in Olla's format.

Existing dashboards parse Olla's log file line by line, so events keep Olla's message
names ("Access log", "Request dispatching", "Request completed", "Request failed",
"Endpoint status changed: <name>") and field names. New events are additive.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime
from typing import Any, TextIO

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}


class EventLog:
    def __init__(self, path: str | None, level: str = "info", max_size_mb: int = 10, max_backups: int = 7):
        self.path = path
        self.min_level = _LEVELS[level]
        self.max_bytes = max_size_mb * 1024 * 1024
        self.max_backups = max_backups
        self._lock = threading.Lock()
        self._fh: TextIO | None = None
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")

    def event(self, level: str, msg: str, **fields: Any) -> None:
        if _LEVELS[level] < self.min_level:
            return
        rec = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "level": level.upper(), "msg": msg}
        rec.update(fields)
        line = json.dumps(rec, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            if self._fh is None:
                sys.stderr.write(line)
                return
            self._fh.write(line)
            self._fh.flush()
            if self._fh.tell() >= self.max_bytes:
                self._rotate()

    def debug(self, msg: str, **f: Any) -> None:
        self.event("debug", msg, **f)

    def info(self, msg: str, **f: Any) -> None:
        self.event("info", msg, **f)

    def warn(self, msg: str, **f: Any) -> None:
        self.event("warn", msg, **f)

    def error(self, msg: str, **f: Any) -> None:
        self.event("error", msg, **f)

    def _rotate(self) -> None:
        """Compress to olla-<UTC timestamp>.log.gz beside the live file, as Olla does."""
        assert self.path and self._fh
        self._fh.close()
        d = os.path.dirname(self.path) or "."
        stem = os.path.splitext(os.path.basename(self.path))[0]
        now = time.time()
        stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime(now)) + f".{int(now * 1000) % 1000:03d}"
        target = os.path.join(d, f"{stem}-{stamp}.log.gz")
        with open(self.path, "rb") as src, gzip.open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.chmod(target, 0o600)
        self._fh = open(self.path, "w", encoding="utf-8")
        backups = sorted(f for f in os.listdir(d) if f.startswith(f"{stem}-") and f.endswith(".log.gz"))
        for old in backups[: max(0, len(backups) - self.max_backups)]:
            os.remove(os.path.join(d, old))

    def close(self) -> None:
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None
