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


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def build_sessions(log_dir: str, idle_gap_min: int | None = None) -> list[dict]:
    """Reconstruct sessions from session_start/session_end markers.

    Session markers carry the authoritative boundaries; if a session_end
    is missing (crash), the session is closed at the last event time.
    """
    events = load_events(log_dir)
    sessions: list[dict] = []
    current: dict | None = None
    by_id: dict[int, dict] = {}

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
                "notes": [],
                "commits": [],
            }
            by_id[current["id"]] = current
            sessions.append(current)
        elif etype == "session_end" and current is not None and ev.get("session") == current["id"]:
            current["end"] = ev.get("ts")
            current["reason_end"] = ev.get("reason")
        elif etype == "note_start" and current is not None:
            current["notes"].append({"ts": ev.get("ts"), "text": ev.get("text")})
        elif etype == "git_commit" and current is not None:
            current["commits"].append(ev)
        elif etype == "file" and current is not None:
            current["events"] += 1
            p = ev.get("path", "")
            if p:
                current["files"].add(p)
            act = ev.get("actor", "unknown")
            current["actors"][act] = current["actors"].get(act, 0) + 1

    # Close dangling sessions at last event ts
    for s in sessions:
        if s["end"] is None:
            s["end"] = s.get("_last_ts") or s["start"]
            s["reason_end"] = "crash"
        s["files"] = sorted(s["files"])
        s["duration_min"] = round((_parse_ts(s["end"]) - _parse_ts(s["start"])).total_seconds() / 60, 1)
        s.pop("_last_ts", None)
    return sessions


def total_active_min(sessions: list[dict]) -> float:
    return round(sum(s.get("duration_min", 0) for s in sessions), 1)
