"""btl daemon: runs one watchdog observer per configured watch.

Responsibilities per watch:
  - filter file events (WRITE/CREATE/DELETE/MOVED), exclude noise
  - auto-exclude the btl config root itself (avoids self-logging loop)
  - write events to the watch's JSONL ledger
  - sessionization: session_start on first event after idle gap,
    session_end when idle gap elapses (timer thread)
  - git detection: writes under .git/ trigger a debounced commit capture
  - notes: served over the IPC socket, scoped to the active session

Actor attribution: the watchdog inotify/FSEvents backends do not expose
the writing PID, so actor defaults to "unknown" (fail-open, per PRD
FR-19). A fanotify/fatrace backend (root) would populate proc/actor —
future enhancement.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from btl import config as cfg
from btl.backends import create_backend
from btl.ledger import Ledger
from btl.procattr import ActorClassifier, ProcessInspector

# File events we keep (backend emits only created/modified/deleted/moved;
# reads are never reported — FR-3 naturally satisfied)
KEEP_EVENTS = {"created", "modified", "deleted", "moved"}

SESSION_IDLE_REASON = "idle_gap"
SESSION_NOTE_REASON = "note"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WatchState:
    """Per-watch runtime state (session, note, timers)."""

    def __init__(self, w: dict, ledger: Ledger):
        self.w = w
        self.ledger = ledger
        self.root = w["root"]
        self.idle_gap = float(w["idle_gap_min"]) * 60.0
        self.last_activity: float = 0.0
        self.session_id: int | None = None
        self.session_start: float = 0.0
        self.note_text: str | None = None
        self.note_ts: str | None = None
        self.repos: set[str] = set()          # repo roots seen recently
        self.repo_lock = threading.Lock()
        self._seq = 0
        self.backend_kind: str = "unknown"

    def new_session_id(self) -> int:
        # epoch-seconds: unique across daemon restarts (sessions are
        # separated by at least the idle gap in normal use)
        return int(time.time())

    @property
    def active(self) -> bool:
        return self.session_id is not None


class BtlEventHandler:
    """Receives backend events for one watch; writes to the ledger."""

    def __init__(self, state: WatchState, global_cfg: dict, excludes: set[str],
                 config_root: str, process_tracking: bool = True):
        self.state = state
        self.global_cfg = global_cfg
        self.excludes = excludes
        self.config_root = config_root
        self.exclude_actors = set(
            global_cfg.get("actors", {}).get("exclude", []) or []
        )
        if process_tracking:
            actors = global_cfg.get("actors", {})
            self.inspector = ProcessInspector()
            self.classifier = ActorClassifier(
                agents=actors.get("agents", []),
                humans=actors.get("humans", []),
                system=actors.get("system", []),
            )
        else:
            self.inspector = None
            self.classifier = None

    def on_event(self, event_type: str, path: str, is_dir: bool,
                 pid: int | None = None) -> None:
        if event_type not in KEEP_EVENTS:
            return
        if not path:
            return
        path = os.path.normpath(path)
        git = self._git_signal(path)
        if self._skip(path) and not git:
            return
        self.state.last_activity = time.time()
        if git:
            # commit signal: queue repo for capture, no file event logged
            self._queue_git_repo(path)
            return
        actor, proc, exe, unit = self._attribution(pid)
        if actor in self.exclude_actors:
            return
        self._ensure_session(reason="activity")
        rel = self._rel(path)
        self.state.ledger.append(
            type="file",
            event=event_type.upper(),
            path=rel,
            is_dir=is_dir,
            actor=actor,
            proc=proc,
            exe=exe,
            unit=unit,
            watch=self.state.w["name"],
            project=self.state.w["project"],
        )

    def _attribution(self, pid: int | None) -> tuple[str, str, str, str]:
        """Map a backend PID to (actor, proc, exe, unit). Fail-open."""
        if pid is None or self.inspector is None or self.classifier is None:
            return "unknown", "", "", ""
        info = self.inspector.inspect(pid)
        if info is None:
            return "unknown", "", "", ""
        actor = self.classifier.classify(info)
        return (actor, info.comm,
                os.path.basename(info.exe) if info.exe else "",
                info.unit)

    # -- helpers ------------------------------------------------------

    def _skip(self, path: str) -> bool:
        if path.startswith(self.config_root + os.sep):
            return True
        parts = path.split(os.sep)
        for ex in self.excludes:
            if ex in parts:
                return True
        return False

    def _rel(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.state.root)
        except ValueError:
            return path

    def _ensure_session(self, reason: str):
        st = self.state
        if not st.active:
            st.session_start = time.time()
            st.session_id = st.new_session_id()
            st.ledger.append(
                type="session_start",
                session=st.session_id,
                reason=reason,
                project=st.w["project"],
                watch=st.w["name"],
            )

    def _git_signal(self, path: str) -> bool:
        """True if path is a commit signal (under .git refs/HEAD/logs)."""
        if not self.state.w.get("git", True):
            return False
        p = Path(path)
        for parent in [p] + list(p.parents):
            git_dir = parent / ".git"
            if git_dir.exists():
                rel = os.path.relpath(path, str(parent))
                return (
                    rel.startswith(".git/refs/")
                    or rel.startswith(".git/logs/")
                    or rel in (".git/HEAD", ".git/packed-refs", ".git/index")
                )
        return False

    def _queue_git_repo(self, path: str):
        """Find repo root for a commit signal and queue it for capture."""
        p = Path(path)
        for parent in [p] + list(p.parents):
            if (parent / ".git").exists():
                with self.state.repo_lock:
                    self.state.repos.add(str(parent))
                return


class IdleTimer(threading.Thread):
    """Checks each watch every 2s; closes sessions/notes on idle gap."""

    def __init__(self, states: list[WatchState]):
        super().__init__(daemon=True, name="btl-idle-timer")
        self.states = states
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            for st in self.states:
                if st.active and (time.time() - st.last_activity) > st.idle_gap:
                    close_session(st, reason=SESSION_IDLE_REASON)
            time.sleep(2)


def close_session(state: WatchState, reason: str):
    """Public helper (used by CLI via IPC): close active session/note."""
    st = state
    if not st.active:
        return
    if st.note_text is not None:
        st.ledger.append(
            type="note_stop", session=st.session_id,
            reason="idle_gap" if reason == SESSION_IDLE_REASON else "manual",
            text=st.note_text, watch=st.w["name"],
        )
        st.note_text = None
    st.ledger.append(
        type="session_end", session=st.session_id, reason=reason,
        project=st.w["project"], watch=st.w["name"],
    )
    st.session_id = None


class Daemon:
    def __init__(self, global_cfg: dict | None = None):
        self.root = cfg.config_root()
        cfg.ensure_layout(self.root)
        self.global_cfg = global_cfg or cfg.load_global_config(self.root)
        self.watches = cfg.load_all_watches(self.root)
        if not self.watches:
            raise ValueError(
                "no watches configured — add one under "
                f"{self.root / 'watches' / '<name>' / 'watch.yaml'}"
            )
        self.states: list[WatchState] = []
        self.backends = []
        self.ledgers: list[Ledger] = []
        self.idle_timer: IdleTimer | None = None
        self._stop = threading.Event()

    # -- lifecycle ----------------------------------------------------

    def start(self) -> None:
        excludes = set(self.global_cfg.get("exclude", []))
        process_tracking = bool(self.global_cfg.get("process_tracking", True))
        for w in self.watches:
            ledger = Ledger(w["log_dir"])
            state = WatchState(w, ledger)
            handler = BtlEventHandler(state, self.global_cfg, excludes,
                                      str(self.root), process_tracking)
            backend = create_backend(
                w["root"], excludes, str(self.root), handler.on_event,
                process_tracking=process_tracking,
            )
            backend.start()
            state.backend_kind = backend.kind
            self.states.append(state)
            self.ledgers.append(ledger)
            self.backends.append(backend)
            state.last_activity = time.time()  # don't immediately idle
            print(f"watch '{w['name']}' → backend {backend.kind} "
                  f"(root {w['root']})")
        self.idle_timer = IdleTimer(self.states)
        self.idle_timer.start()

    def stop(self) -> None:
        self._stop.set()
        if self.idle_timer:
            self.idle_timer.stop()
        for b in self.backends:
            b.stop()
        for st in self.states:
            if st.active:
                close_session(st, reason="daemon_stop")
        for led in self.ledgers:
            led.close()

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                time.sleep(1)
        finally:
            self.stop()

    # -- commands (called from IPC) ----------------------------------

    def find_state(self, watch: str | None) -> WatchState:
        if watch:
            for st in self.states:
                if st.w["name"] == watch:
                    return st
            raise ValueError(f"unknown watch: {watch}")
        if len(self.states) == 1:
            return self.states[0]
        if not self.states:
            raise ValueError("no watches running")
        raise ValueError(
            "multiple watches running — specify one: "
            + ", ".join(st.w["name"] for st in self.states)
        )

    def cmd_status(self) -> dict:
        out = {
            "running": True,
            "config_root": str(self.root),
            "process_tracking": bool(self.global_cfg.get("process_tracking", True)),
            "watches": [],
        }
        for st in self.states:
            out["watches"].append({
                "name": st.w["name"],
                "root": st.root,
                "project": st.w["project"],
                "backend": st.backend_kind,
                "active_session": st.active,
                "note": st.note_text,
                "idle_gap_min": st.w["idle_gap_min"],
                "events_logged": st.ledger.count,
            })
        return out

    def cmd_restart(self) -> dict:
        """Restart the daemon (systemd unit) so new config takes effect."""
        if os.geteuid() == 0:
            unit = "btl-root.service"
            cmd = ["systemctl", "restart", unit]
        else:
            unit = "btl.service"
            cmd = ["systemctl", "--user", "restart", unit]
        try:
            subprocess.Popen(cmd)
            return {"ok": True, "restarting": True, "unit": unit}
        except OSError:
            return {"ok": False, "error": "systemctl not available"}

    def cmd_note(self, text: str, watch: str | None) -> dict:
        st = self.find_state(watch)
        if not st.active:
            st.last_activity = time.time()
            st.session_start = time.time()
            st.session_id = st.new_session_id()
            st.ledger.append(
                type="session_start", session=st.session_id,
                reason=SESSION_NOTE_REASON, project=st.w["project"],
                watch=st.w["name"],
            )
        st.note_text = text
        st.note_ts = _now_iso()
        st.last_activity = time.time()
        st.ledger.append(
            type="note_start", session=st.session_id, text=text,
            watch=st.w["name"],
        )
        return {"ok": True, "watch": st.w["name"], "session": st.session_id,
                "note": text}

    def cmd_note_stop(self, watch: str | None) -> dict:
        st = self.find_state(watch)
        if st.note_text is None:
            return {"ok": True, "note": None}
        st.ledger.append(
            type="note_stop", session=st.session_id, text=st.note_text,
            reason="manual", watch=st.w["name"],
        )
        st.note_text = None
        return {"ok": True, "watch": st.w["name"], "note_stopped": True}

    def cmd_session_stop(self, watch: str | None) -> dict:
        st = self.find_state(watch)
        close_session(st, reason="manual")
        return {"ok": True, "watch": st.w["name"]}


# -- git commit capture (debounced) -----------------------------------

def capture_commits(states: list[WatchState], interval: float = 2.0) -> None:
    """Background loop: for repos touched recently, record latest commit."""
    last_hash: dict[str, str] = {}
    while True:
        for st in states:
            with st.repo_lock:
                repos = list(st.repos)
                st.repos.clear()
            for repo in repos:
                try:
                    r = subprocess.run(
                        ["git", "-C", repo, "log", "-1",
                         "--format=%H\t%an\t%s"],
                        capture_output=True, text=True, timeout=10,
                    )
                    if r.returncode != 0 or not r.stdout.strip():
                        continue
                    parts = r.stdout.strip().split("\t", 2)
                    if len(parts) < 3:
                        continue
                    h, author, msg = parts[0], parts[1], parts[2]
                    if last_hash.get(repo) == h:
                        continue
                    last_hash[repo] = h
                    st.ledger.append(
                        type="git_commit", repo=repo,
                        hash=h, author=author, message=msg,
                        watch=st.w["name"], project=st.w["project"],
                    )
                except Exception:
                    continue
        time.sleep(interval)
