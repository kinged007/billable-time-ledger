"""Backend abstraction: emits file events per watch root.

Linux:    fanotify (root, process-attributed) when available, else the
          custom inotify backend with selective directory registration
          (one fd, only non-noise dirs get watches — stays under the
          kernel inotify limit even when watching ~/)
macOS:    watchdog FSEvents (single stream, recursive is cheap)
"""

from __future__ import annotations

import os
import platform
from typing import Callable

# callback(event_type, path, is_dir, pid)
# event_type in {"created", "modified", "deleted", "moved"}
# pid is None for backends that cannot attribute (inotify, FSEvents)
EventCallback = Callable[[str, str, bool, int | None], None]


def create_backend(watch_root: str, excludes: set[str], config_root: str,
                   on_event: EventCallback,
                   process_tracking: bool = True) -> "Backend":
    system = platform.system()
    if system == "Darwin":
        from btl.backends.fsevents_darwin import FSEventsBackend
        return FSEventsBackend(watch_root, excludes, config_root, on_event)
    if system == "Linux" and process_tracking and os.geteuid() == 0:
        try:
            from btl.backends.fanotify_linux import FanotifyBackend
            return FanotifyBackend(watch_root, excludes, config_root, on_event)
        except Exception:
            pass  # fall back to inotify (actor stays "unknown")
    from btl.backends.inotify_linux import InotifyBackend
    return InotifyBackend(watch_root, excludes, config_root, on_event)


class Backend:
    def start(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    @property
    def kind(self) -> str:  # pragma: no cover - abstract
        return "unknown"
