"""Append-only JSONL ledger writer, one file per watch per UTC day."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Dict


class Ledger:
    """Thread-safe JSONL appender with daily rotation.

    Files: <log_dir>/<watch>/YYYY-MM-DD.jsonl
    """

    def __init__(self, log_dir: str):
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._fh: Dict[str, IO[str]] = {}   # day -> open file handle
        self._lock = threading.Lock()
        self._count = 0

    def _day(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _path_for(self, day: str) -> Path:
        return self._dir / f"{day}.jsonl"

    def _open(self, day: str):
        if day not in self._fh:
            self._fh[day] = open(self._path_for(day), "a", encoding="utf-8")
        return self._fh[day]

    def append(self, **event: object) -> None:
        """Append one event (already complete dict)."""
        event.setdefault("ts", datetime.now(timezone.utc).isoformat())
        day = self._day()
        with self._lock:
            fh = self._open(day)
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            self._count += 1

    def rotate(self) -> None:
        """Close handles for any non-current day (call periodically)."""
        today = self._day()
        with self._lock:
            for day in list(self._fh):
                if day != today:
                    self._fh[day].close()
                    del self._fh[day]

    @property
    def count(self) -> int:
        return self._count

    def close(self) -> None:
        with self._lock:
            for fh in self._fh.values():
                fh.close()
            self._fh.clear()
