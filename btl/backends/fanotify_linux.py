"""Linux fanotify backend (root) with process attribution.

Why: inotify gives no PID for the writing process. fanotify reports the
PID of the writing process in every event's metadata (unconditional on
modern kernels — the old FAN_REPORT_PID flag no longer exists), and with
FAN_REPORT_DFID_NAME (dir fid + name) we can reconstruct paths even for
deleted files (name comes from the parent dir record, no handle
resolution needed). Requires CAP_SYS_ADMIN — the daemon runs as root via
the btl-root systemd unit.

Strategy (mirrors inotify semantics but fs-wide + filtered):
  - one fanotify fd, FAN_MARK_FILESYSTEM per unique filesystem of the
    watch root (events for the whole fs, filtered to the root in
    userspace — keeps us far below any per-dir watch limit)
  - event mapping: CLOSE_WRITE -> modified, CREATE/DELETE/MOVED -> as-is
  - paths from the DFID_NAME record (dir handle + name) or, for file
    events, by resolving the FID file handle via open_by_handle_at
  - every delivered event carries the PID; the daemon attributes it

On any init failure (EPERM when not root) the constructor raises and
create_backend() falls back to inotify (actor stays "unknown").
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import select
import struct
import threading

from btl.backends import Backend, EventCallback

# -- flags from linux/fanotify.h (kernel >= 5.1) -----------------------
FAN_CLASS_NOTIF = 0x00000000
FAN_CLOEXEC = 0x00000001
FAN_NONBLOCK = 0x00000002
FAN_REPORT_FID = 0x00000200
FAN_REPORT_DIR_FID = 0x00000400
FAN_REPORT_NAME = 0x00000800
FAN_REPORT_DFID_NAME = FAN_REPORT_DIR_FID | FAN_REPORT_NAME

FAN_MARK_ADD = 0x00000001
FAN_MARK_FILESYSTEM = 0x00000100

FAN_CLOSE_WRITE = 0x00000008
FAN_MOVED_FROM = 0x00000040
FAN_MOVED_TO = 0x00000080
FAN_CREATE = 0x00000100
FAN_DELETE = 0x00000200
FAN_Q_OVERFLOW = 0x00004000
FAN_ONDIR = 0x40000000

EVENT_MASK = (
    FAN_CLOSE_WRITE | FAN_CREATE | FAN_DELETE | FAN_MOVED_FROM | FAN_MOVED_TO
)

AT_FDCWD = -100
O_PATH = os.O_PATH  # Python exposes it on Linux

# info record types (fanotify_event_info_type). NOTE: renumbered in
# newer kernels — DFID_NAME=2, DFID=3 (older kernels had them swapped).
INFO_FID = 1
INFO_DFID_NAME = 2
INFO_DFID = 3

# struct fanotify_event_metadata: u32 event_len, u8 vers, u8 reserved,
#   u16 metadata_len, u64 mask, s32 fd, s32 pid  (24 bytes, no padding)
_METADATA = struct.Struct("=IBBHqii")
_INFOHDR = struct.Struct("=BBH")


def _libc() -> ctypes.CDLL:
    name = ctypes.util.find_library("c")
    if not name:
        raise RuntimeError("libc not found")
    return ctypes.CDLL(name, use_errno=True)


class FanotifyBackend(Backend):
    def __init__(self, watch_root: str, excludes: set[str], config_root: str,
                 on_event: EventCallback):
        self.root = os.path.normpath(watch_root)
        self.excludes = excludes
        self.config_root = os.path.normpath(config_root)
        self.on_event = on_event
        self._fd = -1
        self._dev_fds: dict[int, int] = {}   # st_dev -> O_PATH fd on the root
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._libc = _libc()
        self._libc.fanotify_init.argtypes = [ctypes.c_uint, ctypes.c_uint]
        self._libc.fanotify_init.restype = ctypes.c_int
        self._libc.fanotify_mark.argtypes = [
            ctypes.c_int, ctypes.c_uint, ctypes.c_uint64, ctypes.c_int,
            ctypes.c_char_p,
        ]
        self._libc.fanotify_mark.restype = ctypes.c_int
        self._libc.open_by_handle_at.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        self._libc.open_by_handle_at.restype = ctypes.c_int
        self._init_and_mark()

    # -- setup ---------------------------------------------------------

    def _init_and_mark(self) -> None:
        flags = (
            FAN_CLASS_NOTIF | FAN_CLOEXEC | FAN_NONBLOCK
            | FAN_REPORT_FID | FAN_REPORT_DFID_NAME
        )
        fd = self._libc.fanotify_init(flags, os.O_RDONLY)
        if fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"fanotify_init failed: {errno.errorcode.get(err, err)}")
        self._fd = fd
        st = os.stat(self.root)
        # open_by_handle_at rejects O_PATH fds (EBADF) — it needs a real
        # readable mount fd (O_RDONLY|O_DIRECTORY).
        mfd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self._dev_fds[st.st_dev] = mfd
        ret = self._libc.fanotify_mark(
            fd, FAN_MARK_ADD | FAN_MARK_FILESYSTEM, EVENT_MASK,
            AT_FDCWD, self.root.encode("utf-8"),
        )
        if ret < 0:
            err = ctypes.get_errno()
            self._close_fds()
            raise OSError(err, f"fanotify_mark failed: {errno.errorcode.get(err, err)}")

    def _close_fds(self) -> None:
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1
        for fd in self._dev_fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self._dev_fds.clear()

    # -- event loop ----------------------------------------------------

    def _loop(self) -> None:
        poll = select.poll()
        poll.register(self._fd, select.POLLIN)
        buf = b""
        while not self._stop.is_set():
            try:
                events = poll.poll(1000)
            except OSError:
                continue
            if not events:
                continue
            try:
                data = os.read(self._fd, 131072)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                if e.errno == errno.EINTR:
                    continue
                break
            if not data:
                continue
            buf += data
            buf = self._drain(buf)
        self._close_fds()

    def _drain(self, buf: bytes) -> bytes:
        off = 0
        n = len(buf)
        while off + _METADATA.size <= n:
            event_len, _vers, _res, _mlen, mask, _fd, pid = _METADATA.unpack_from(buf, off)
            if event_len < _METADATA.size or off + event_len > n:
                break
            self._handle_event(buf, off + _METADATA.size, off + event_len,
                               mask, pid)
            off += event_len
        return buf[off:]

    def _handle_event(self, buf: bytes, start: int, end: int, mask: int,
                      pid: int) -> None:
        if mask & FAN_Q_OVERFLOW:
            return
        if mask & FAN_CREATE:
            etype = "created"
        elif mask & FAN_DELETE:
            etype = "deleted"
        elif mask & FAN_MOVED_FROM or mask & FAN_MOVED_TO:
            etype = "moved"
        elif mask & FAN_CLOSE_WRITE:
            etype = "modified"
        else:
            return
        is_dir = bool(mask & FAN_ONDIR)
        handle, name = self._parse_info(buf, start, end)
        path = None
        if handle is not None:
            resolved = self._resolve(handle)
            if name:
                if name == ".":
                    path = resolved
                elif resolved:
                    path = os.path.join(resolved, name)
            else:
                path = resolved
        if not path:
            return
        if not self._path_ok(path):
            return
        try:
            self.on_event(etype, os.path.normpath(path), is_dir, pid)
        except Exception:
            pass

    def _parse_info(self, buf: bytes, start: int, end: int):
        """Return (handle, name) from the info records.

        First record wins: dirent events carry DFID_NAME (dir handle +
        name); plain file events carry a single FID record (file handle).
        """
        pos = start
        dir_handle = None
        name = None
        fh = None
        while pos + 4 <= end:
            itype, _pad, ilen = _INFOHDR.unpack_from(buf, pos)
            if ilen < 4 or pos + ilen > end:
                break
            if itype in (INFO_FID, INFO_DFID, INFO_DFID_NAME):
                # layout: hdr(4) fsid(8) handle_bytes(4) handle_type(4) f_handle[hb] [name]
                try:
                    hb, ht = struct.unpack_from("=Ii", buf, pos + 12)
                except struct.error:
                    pos += ilen
                    continue
                if hb > 0 and pos + 20 + hb <= pos + ilen:
                    fbytes = bytes(buf[pos + 20: pos + 20 + hb])
                    # DFID_NAME carries the parent-dir handle + entry name;
                    # FID/DFID carry a bare handle. Some kernels report the
                    # name on type 2 (DFID_NAME), so treat any record with
                    # trailing bytes as dir+name.
                    if itype == INFO_DFID_NAME or (itype in (INFO_FID, INFO_DFID) and pos + 20 + hb < pos + ilen):
                        dir_handle = (hb, ht, fbytes)
                        raw = bytes(buf[pos + 20 + hb: pos + ilen]).rstrip(b"\x00")
                        name = raw.decode("utf-8", "replace") if raw else None
                    elif dir_handle is None and name is None:
                        fh = (hb, ht, fbytes)
            pos += ilen
        return (dir_handle if dir_handle is not None else fh), name

    def _resolve(self, handle) -> str | None:
        """Resolve a file handle to a path via open_by_handle_at."""
        hb, ht, fbytes = handle
        buf = ctypes.create_string_buffer(8 + hb)
        ctypes.memmove(buf, struct.pack("=Ii", hb, ht), 8)
        ctypes.memmove(ctypes.byref(buf, 8), fbytes, hb)
        for mfd in self._dev_fds.values():
            fd = self._libc.open_by_handle_at(mfd, buf, O_PATH)
            if fd < 0:
                continue
            try:
                path = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                path = None
            try:
                os.close(fd)
            except OSError:
                pass
            if path:
                return path
        return None

    def _path_ok(self, path: str) -> bool:
        path = os.path.normpath(path)
        if path != self.root and not path.startswith(self.root + os.sep):
            return False
        if path == self.config_root or path.startswith(self.config_root + os.sep):
            return False
        return True

    # -- lifecycle ----------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="btl-fanotify")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def kind(self) -> str:
        return "fanotify"
