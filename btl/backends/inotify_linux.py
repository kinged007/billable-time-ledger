"""Linux inotify backend with selective directory registration.

Strategy: one inotify fd; register watches only for directories that
pass the exclude filter. On directory creation/move-in, register the new
dir if it passes. For .git dirs, register only the root + refs/ + logs/
(commit signals) — never objects/. This keeps the watch count far below
fs.inotify.max_user_watches even when the watch root is ~/.

Events are mapped from raw inotify masks:
  - IN_CLOSE_WRITE  -> "modified"  (file written and closed)
  - IN_CREATE       -> "created"   (file or dir)
  - IN_DELETE       -> "deleted"
  - IN_MOVED_FROM/TO-> "moved"
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import select
import struct
import threading
from pathlib import Path

from btl.backends import Backend, EventCallback

IN_CLOSE_WRITE = 0x00000008
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_ISDIR = 0x40000000
IN_ONLYDIR = 0x01000000
IN_IGNORED = 0x00008000
IN_Q_OVERFLOW = 0x00004000

WATCH_MASK = (
    IN_CLOSE_WRITE | IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO
)

_EVENT_STRUCT = "iIII"  # wd, mask, cookie, len
_EVENT_SIZE = struct.calcsize(_EVENT_STRUCT)

# subdirs of .git we register (commit signals), everything else skipped
GIT_WATCH_DIRS = {"", "refs", "logs"}


class InotifyBackend(Backend):
    def __init__(self, watch_root: str, excludes: set[str], config_root: str,
                 on_event: EventCallback):
        self.root = os.path.normpath(watch_root)
        self.excludes = excludes
        self.config_root = os.path.normpath(config_root)
        self.on_event = on_event
        self._fd = -1
        self._wd_to_path: dict[int, str] = {}
        self._path_to_wd: dict[str, int] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        libc_name = ctypes.util.find_library("c")
        if not libc_name:
            raise RuntimeError("libc not found")
        self._libc = ctypes.CDLL(libc_name, use_errno=True)
        self._libc.inotify_init1.argtypes = [ctypes.c_int]
        self._libc.inotify_init1.restype = ctypes.c_int
        self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._libc.inotify_add_watch.restype = ctypes.c_int

    # -- registration ------------------------------------------------

    def _is_noise(self, path: str) -> bool:
        """True if the dir should not be watched at all."""
        if path == self.config_root or path.startswith(self.config_root + os.sep):
            return True
        rel = os.path.relpath(path, self.root)
        parts = rel.split(os.sep)
        for p in parts:
            if p in self.excludes:
                return True
        return False

    def _should_register_git(self, path: str, git_root: str) -> bool:
        rel = os.path.relpath(path, git_root)
        return rel in GIT_WATCH_DIRS or rel.startswith("refs") or rel.startswith("logs")

    def _register(self, path: str) -> None:
        path = os.path.normpath(path)
        if path in self._path_to_wd:
            return
        # special handling for .git dirs: register root + refs/ + logs/
        # (+ one level of subdirs), never objects/ — commit signals only.
        # Must run BEFORE the noise check: ".git" is in the default excludes
        # (we don't want .git file events, only commit signals).
        base = os.path.basename(path)
        if base == ".git":
            self._add_watch(path)
            for sub in ("refs", "logs"):
                subp = os.path.join(path, sub)
                self._add_watch(subp)
                if os.path.isdir(subp):
                    for child in os.listdir(subp):
                        c = os.path.join(subp, child)
                        if os.path.isdir(c):
                            self._add_watch(c)
            return
        if self._is_noise(path):
            return
        self._add_watch(path)

    def _add_watch(self, path: str) -> None:
        if not os.path.isdir(path) or path in self._path_to_wd:
            return
        try:
            wd = self._libc.inotify_add_watch(
                self._fd, path.encode("utf-8"), WATCH_MASK | IN_ONLYDIR
            )
        except OSError:
            return
        if wd < 0:
            return
        self._wd_to_path[wd] = path
        self._path_to_wd[path] = wd

    def _initial_scan(self) -> None:
        for dirpath, dirnames, _filenames in os.walk(self.root):
            # keep .git dirs so commit signals are watched; prune other noise
            dirnames[:] = [
                d for d in dirnames
                if d == ".git" or not self._is_noise(os.path.join(dirpath, d))
            ]
            self._register(dirpath)

    # -- event loop ---------------------------------------------------

    def _read_events(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                data = os.read(self._fd, 65536)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                if e.errno == errno.EINTR:
                    continue
                break
            if not data:
                break
            buf += data
            while len(buf) >= _EVENT_SIZE:
                wd, mask, _cookie, name_len = struct.unpack(
                    _EVENT_STRUCT, buf[:_EVENT_SIZE]
                )
                name = buf[_EVENT_SIZE:_EVENT_SIZE + name_len].split(b"\x00", 1)[0].decode("utf-8", "replace")
                buf = buf[_EVENT_SIZE + name_len:]
                self._handle_event(wd, mask, name)

    def _handle_event(self, wd: int, mask: int, name: str) -> None:
        if mask & IN_Q_OVERFLOW:
            return
        path = self._wd_to_path.get(wd)
        if not path:
            return
        full = os.path.join(path, name) if name else path
        is_dir = bool(mask & IN_ISDIR)

        if mask & IN_IGNORED:
            self._wd_to_path.pop(wd, None)
            return

        # register new dirs
        if is_dir and (mask & (IN_CREATE | IN_MOVED_TO)):
            self._register(full)

        if mask & IN_MOVED_FROM:
            self.on_event("moved", full, is_dir)
        elif mask & IN_MOVED_TO:
            self.on_event("moved", full, is_dir)
        elif mask & IN_CREATE:
            self.on_event("created", full, is_dir)
        elif mask & IN_DELETE:
            self.on_event("deleted", full, is_dir)
        elif mask & IN_CLOSE_WRITE:
            self.on_event("modified", full, is_dir)

    # -- lifecycle ----------------------------------------------------

    def start(self) -> None:
        self._fd = self._libc.inotify_init1(0x80000 | 0x00000800)  # CLOEXEC|NONBLOCK
        if self._fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self._initial_scan()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="btl-inotify")
        self._thread.start()

    def _loop(self) -> None:
        poll = select.poll()
        poll.register(self._fd, select.POLLIN)
        while not self._stop.is_set():
            try:
                events = poll.poll(1000)
            except OSError:
                continue
            if events:
                self._read_events()
        try:
            os.close(self._fd)
        except OSError:
            pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
