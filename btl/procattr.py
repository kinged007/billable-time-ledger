"""Process attribution: map a PID (from fanotify events) to an actor identity.

At event time the fanotify backend hands us the writing PID. We read
/proc/<pid> — comm, exe, cmdline, ppid chain, cgroup (systemd unit) —
and classify the actor: a concrete process name (e.g. "hermes", "cron",
"code") or a category fallback ("human", "system", "other", "unknown").

Classification order (first match wins):
  1. cron/atd ancestor chain or systemd timer unit   -> "cron"
  2. system process names                             -> "system" (proc kept)
  3. agent patterns (comm OR cmdline match)           -> agent name (e.g. "hermes")
  4. human process names (comm match only)            -> "human" (proc kept)
  5. fallback                                         -> "other" / "unknown"

Fail-open: any /proc read error yields actor "unknown"; recording never
blocks (PRD FR-19).
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

MAX_ANCESTORS = 8
CACHE_TTL = 5.0
CRON_MARKERS = {"cron", "crond", "atd", "crontab"}


@dataclass
class ProcessInfo:
    pid: int
    comm: str = ""
    exe: str = ""
    cmdline: str = ""
    ppid: int = 0
    starttime: str = ""
    cgroup: str = ""
    unit: str = ""                 # last systemd cgroup component, e.g. hermes-gateway.service
    ancestors: list = field(default_factory=list)   # comms, parent first
    ok: bool = True


def _read_stat(pid: int) -> tuple[str, int, str] | None:
    """Return (comm, ppid, starttime) from /proc/<pid>/stat, or None.

    comm may contain spaces/parens — split on the last ')' of the name
    field, then parse the remainder (state, ppid, ..., starttime).
    """
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            raw = f.read()
        lparen = raw.index("(")
        rparen = raw.rfind(")")
        comm = raw[lparen + 1:rparen]
        rest = raw[rparen + 2:].split()
        # rest[0] = state, rest[1] = ppid, rest[19] = starttime (field 22)
        ppid = int(rest[1])
        starttime = rest[19]
        return comm, ppid, starttime
    except (OSError, ValueError, IndexError):
        return None


def _read_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _read_exe(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def _read_cgroup(pid: int) -> tuple[str, str]:
    """Return (cgroup_line, last_unit_component)."""
    try:
        with open(f"/proc/{pid}/cgroup", "r", encoding="utf-8") as f:
            line = f.read().strip()
        unit = line.rstrip("/").rsplit("/", 1)[-1]
        return line, unit
    except OSError:
        return "", ""


class ProcessInspector:
    """Reads /proc for a PID (with a short TTL cache per pid+starttime)."""

    def __init__(self, cache_ttl: float = CACHE_TTL):
        self._cache: dict[int, tuple[str, float, ProcessInfo]] = {}
        self._lock = threading.Lock()
        self._ttl = cache_ttl

    def inspect(self, pid: int) -> ProcessInfo | None:
        if pid <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(pid)
            if hit:
                starttime, exp, info = hit
                if now < exp and self._starttime_still(pid, starttime):
                    return info
        info = self._inspect_fresh(pid)
        if info is None:
            return None
        st = info.starttime
        with self._lock:
            self._cache[pid] = (st, now + self._ttl, info)
        return info

    def _starttime_still(self, pid: int, starttime: str) -> bool:
        st = _read_stat(pid)
        return bool(st and st[2] == starttime)

    def _inspect_fresh(self, pid: int) -> ProcessInfo | None:
        st = _read_stat(pid)
        if st is None:
            return None
        comm, ppid, starttime = st
        info = ProcessInfo(
            pid=pid, comm=comm, ppid=ppid, starttime=starttime,
            exe=_read_exe(pid), cmdline=_read_cmdline(pid),
        )
        info.cgroup, info.unit = _read_cgroup(pid)
        # walk ancestors (comms, parent first)
        seen: set[int] = set()
        cur = ppid
        for _ in range(MAX_ANCESTORS):
            if cur <= 1 or cur in seen:
                break
            seen.add(cur)
            ast = _read_stat(cur)
            if ast is None:
                break
            info.ancestors.append(ast[0])
            cur = ast[1]
        return info


def _is_cron(info: ProcessInfo) -> bool:
    if any(a in CRON_MARKERS for a in info.ancestors):
        return True
    if info.unit and (".timer" in info.unit or info.unit.startswith("systemd-tmpfiles")):
        return True
    if info.comm in CRON_MARKERS:
        return True
    return False


def _contains(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    return needle in haystack


class ActorClassifier:
    """Classifies ProcessInfo into an actor name using configured patterns.

    agents: match against comm + exe + cmdline (a process running an
            agent CLI, e.g. hermes under python, matches on cmdline)
    humans: match against comm only (bash, code, nvim, ...) — the
            interactive tools a person actually types in
    system: match against comm only (systemd units, package managers)
    """

    def __init__(self, agents: list[str], humans: list[str],
                 system: list[str] | None = None):
        self.agents = [a.lower() for a in agents if a]
        self.humans = [h.lower() for h in humans if h]
        self.system = [s.lower() for s in (system or []) if s]

    def classify(self, info: ProcessInfo) -> str:
        if not info.ok:
            return "unknown"
        if _is_cron(info):
            return "cron"
        comm = info.comm.lower()
        if self.system and comm in self.system:
            return "system"
        haystack = " ".join([info.comm, info.exe, info.cmdline]).lower()
        for agent in self.agents:
            if agent in haystack:
                return agent
        if self.humans and comm in self.humans:
            return "human"
        return "other"
