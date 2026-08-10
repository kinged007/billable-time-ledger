"""Sessionization: group ledger events into work sessions by idle gap.

Sessions are delimited by explicit session_start / session_end events
written by the daemon (idle-gap detection). This module provides the
read-side reconstruction used by reports and exports.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def iter_events(log_dir: str) -> Iterator[dict]:
    """Yield all events from all daily JSONL files, oldest first."""
    d = Path(log_dir)
    if not d.exists():
        return
    for p in sorted(d.glob("*.jsonl")):
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def load_events(log_dir: str) -> list[dict]:
    return list(iter_events(log_dir))


def load_events_multi(log_dirs: list[str]) -> list[dict]:
    """Merge + time-sort events from several watch log dirs."""
    events: list[dict] = []
    for d in log_dirs:
        events.extend(load_events(d))
    events.sort(key=lambda e: e.get("ts", ""))
    return events


def build_sessions_multi(log_dirs: list[str], idle_gap_min: int | None = None,
                         now: datetime | None = None,
                         non_billable: set[str] | None = None) -> list[dict]:
    """Build sessions per watch dir, then merge sorted by start time."""
    sessions: list[dict] = []
    for d in log_dirs:
        sessions.extend(
            build_sessions(d, idle_gap_min=idle_gap_min, now=now,
                           non_billable=non_billable)
        )
    sessions.sort(key=lambda s: s["start"])
    return sessions


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def build_sessions(log_dir: str, idle_gap_min: int | None = None,
                   now: datetime | None = None,
                   non_billable: set[str] | None = None) -> list[dict]:
    """Reconstruct sessions from session_start/session_end markers.

    Session markers carry the authoritative boundaries; if a session_end
    is missing (still active or crash), the session is closed at the last
    event time. duration_min = span start→end (includes trailing idle gap
    for idle-closed sessions); active_min = first→last event time (the
    actual work window).
    """
    events = load_events(log_dir)
    sessions: list[dict] = []
    current: dict | None = None
    by_id: dict[int, dict] = {}
    now = now or datetime.now(timezone.utc)
    non_billable = non_billable or set()

    for ev in events:
        etype = ev.get("type", "file")
        ts = _parse_ts(ev.get("ts", ""))
        if etype == "session_start":
            current = {
                "id": ev.get("session"),
                "start": ev.get("ts"),
                "end": None,
                "reason": ev.get("reason", "activity"),
                "project": ev.get("project", ""),
                "files": set(),
                "events": 0,
                "actors": {},
                "_nb_events": 0,
                "notes": [],
                "commits": [],
                "_first": ts,
                "_last": ts,
                "_end_ts": None,
                "_open": True,
            }
            by_id[current["id"]] = current
            sessions.append(current)
        elif etype == "session_end" and current is not None and ev.get("session") == current["id"]:
            current["end"] = ev.get("ts")
            current["_end_ts"] = _parse_ts(ev.get("ts", ""))
            current["_open"] = False
            current["reason_end"] = ev.get("reason")
        elif etype == "note_start" and current is not None:
            current["notes"].append({"ts": ev.get("ts"), "text": ev.get("text")})
            current["_last"] = max(current["_last"], ts)
        elif etype == "git_commit" and current is not None:
            current["commits"].append(ev)
            current["_last"] = max(current["_last"], ts)
        elif etype == "file" and current is not None:
            current["events"] += 1
            p = ev.get("path", "")
            if p:
                current["files"].add(p)
            act = ev.get("actor", "unknown")
            current["actors"][act] = current["actors"].get(act, 0) + 1
            if act in non_billable:
                current["_nb_events"] += 1
            current["_last"] = max(current["_last"], ts)

    # Close dangling sessions at last event ts
    for s in sessions:
        if s["_end_ts"] is None:
            s["end"] = s["_last"].isoformat()
            s["_end_ts"] = s["_last"]
            s["reason_end"] = "active" if s["_open"] else "crash"
        s["files"] = sorted(s["files"])
        s["duration_min"] = round((s["_end_ts"] - s["_first"]).total_seconds() / 60, 1)
        s["active_min"] = round((s["_last"] - s["_first"]).total_seconds() / 60, 1)
        # billable = session span, unless EVERY file event came from a
        # non-billable actor (e.g. a cron-only block) — a cron write
        # inside a real work session doesn't zero it. Sessions with no
        # file events (pure notes) stay billable.
        s["billable"] = s["_nb_events"] < s["events"]
        s.pop("_nb_events", None)
        s["non_billable_actors"] = sorted(
            a for a in s["actors"] if a in non_billable
        )
        s["open"] = s["_open"]
        s.pop("_first", None)
        s.pop("_last", None)
        s.pop("_end_ts", None)
        s.pop("_open", None)
    return sessions


def total_active_min(sessions: list[dict], billable_only: bool = True) -> float:
    """Billable minutes = session span (start→end), the full session block.

    A session spans from first activity to when it closed (idle gap,
    manual stop, or daemon stop). All time inside the block counts —
    thinking, planning, discussion and gaps between file writes —
    provided activity kept the session alive within the idle gap.
    """
    return round(
        sum(
            s.get("duration_min", 0)
            for s in sessions
            if not billable_only or s.get("billable", True)
        ),
        1,
    )
