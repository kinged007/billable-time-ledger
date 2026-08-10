"""Backend abstraction: emits file events per watch root.

Linux:    custom inotify backend with selective directory registration
          (one fd, only non-noise dirs get watches — stays under the
          kernel inotify limit even when watching ~/)
macOS:    watchdog FSEvents (single stream, recursive is cheap)
"""

from __future__ import annotations

import os
import platform
import threading
from typing import Callable

# callback(event_type, path, is_dir)
# event_type in {"created", "modified", "deleted", "moved"}
EventCallback = Callable[[str, str, bool], None]


def create_backend(watch_root: str, excludes: set[str], config_root: str,
                   on_event: EventCallback) -> "Backend":
    system = platform.system()
    if system == "Darwin":
        from btl.backends.fsevents_darwin import FSEventsBackend
        return FSEventsBackend(watch_root, excludes, config_root, on_event)
    from btl.backends.inotify_linux import InotifyBackend
    return InotifyBackend(watch_root, excludes, config_root, on_event)


class Backend:
    def start(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError
