"""macOS FSEvents backend via watchdog (single stream, recursive is cheap)."""

from __future__ import annotations

import os

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from btl.backends import Backend, EventCallback

KEEP = {"created", "modified", "deleted", "moved"}


class _Handler(FileSystemEventHandler):
    def __init__(self, on_event: EventCallback, excludes: set[str], config_root: str):
        self.on_event = on_event
        self.excludes = excludes
        self.config_root = os.path.normpath(config_root)

    def on_any_event(self, event):
        if event.event_type not in KEEP:
            return
        path = os.path.normpath(getattr(event, "src_path", "") or "")
        if not path:
            return
        if path == self.config_root or path.startswith(self.config_root + os.sep):
            return
        parts = path.split(os.sep)
        for ex in self.excludes:
            if ex in parts:
                return
        self.on_event(event.event_type, path, event.is_directory)


class FSEventsBackend(Backend):
    def __init__(self, watch_root: str, excludes: set[str], config_root: str,
                 on_event: EventCallback):
        self.watch_root = watch_root
        self.excludes = excludes
        self.config_root = config_root
        self.on_event = on_event
        self.observer = Observer()

    def start(self) -> None:
        handler = _Handler(self.on_event, self.excludes, self.config_root)
        self.observer.schedule(handler, self.watch_root, recursive=True)
        self.observer.start()

    def stop(self) -> None:
        self.observer.stop()
        self.observer.join(timeout=5)
