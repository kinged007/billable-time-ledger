"""Sessionization: group ledger events into work sessions by idle gap.

Sessions are delimited by explicit session_start / session_end events
written by the daemon (idle-gap detection). This module provides the
read-side reconstruction used by reports and exports.
"""

from __future__ import annotations

import json
import os
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
    return _build_sessions_events(
        load_events(log_dir), idle_gap_min=idle_gap_min, now=now,
        non_billable=non_billable,
    )


def _build_sessions_events(events: list[dict], idle_gap_min: int | None = None,
                           now: datetime | None = None,
                           non_billable: set[str] | None = None) -> list[dict]:
    sessions: list[dict] = []
    current: dict | None = None
    by_id: dict[object, dict] = {}
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
                "watch": ev.get("watch", ""),
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
            current["_allocated_duration_min"] = ev.get("allocated_duration_min")
            current["_open"] = ev.get("open", False)
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
        if s.get("_allocated_duration_min") is not None:
            s["duration_min"] = s["_allocated_duration_min"]
        s["active_min"] = round((s["_last"] - s["_first"]).total_seconds() / 60, 1)
        if s.get("_allocated_duration_min") is not None:
            s["active_min"] = min(s["active_min"], s["duration_min"])
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
        s.pop("_allocated_duration_min", None)
        s.pop("_open", None)
    return sessions


def _absolute_event_path(event: dict, source: dict) -> str | None:
    if event.get("type") == "file":
        value = event.get("path", "")
    elif event.get("type") == "git_commit":
        value = event.get("repo", "")
    else:
        return None
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = Path(source["root"]) / path
    return os.path.normpath(str(path))


def _watch_owns(watch: dict, path: str) -> bool:
    for root in watch["roots"]:
        try:
            if os.path.commonpath((path, root)) == root:
                return True
        except ValueError:
            continue
    return False


def _event_owner(event: dict, source: dict, watches: list[dict]) -> dict:
    path = _absolute_event_path(event, source)
    if path is None:
        return source
    matches = [watch for watch in watches if _watch_owns(watch, path)]
    if not matches:
        return source
    return max(
        matches,
        key=lambda watch: (max(len(root) for root in watch["roots"]), watch["name"]),
    )


def _event_fingerprint(event: dict, source: dict) -> tuple | None:
    path = _absolute_event_path(event, source)
    if path is None:
        return None
    if event.get("type") == "file":
        return (
            "file", path, event.get("event", ""),
            bool(event.get("is_dir")), event.get("actor", "unknown"),
        )
    if event.get("type") == "git_commit" and event.get("hash"):
        return ("git", path, event["hash"])
    return None


def _source_priority(record: dict, watches: list[dict]) -> tuple:
    owner = _event_owner(record["event"], record["source"], watches)
    return (
        -max(len(root) for root in owner["roots"]),
        -max(len(root) for root in record["source"]["roots"]),
        record["timestamp"],
        record["source"]["name"],
    )


def _duplicate_event_ids(events_by_watch: dict[str, list[dict]],
                         watches: list[dict]) -> set[int]:
    groups: dict[tuple, list[dict]] = {}
    for source in watches:
        for event in events_by_watch[source["name"]]:
            fingerprint = _event_fingerprint(event, source)
            if fingerprint:
                groups.setdefault(fingerprint, []).append({
                    "event": event,
                    "source": source,
                    "timestamp": _parse_ts(event.get("ts", "")),
                })

    duplicates: set[int] = set()
    for fingerprint, records in groups.items():
        records.sort(key=lambda record: record["timestamp"])
        if fingerprint[0] == "git":
            canonical = min(records, key=lambda record: _source_priority(record, watches))
            duplicates.update(id(record["event"]) for record in records
                              if record is not canonical)
            continue

        clusters: list[list[dict]] = []
        for record in records:
            if clusters and (record["timestamp"] - clusters[-1][-1]["timestamp"]).total_seconds() > 1:
                clusters.append([record])
            elif clusters:
                clusters[-1].append(record)
            else:
                clusters.append([record])
        for cluster in clusters:
            by_source: dict[str, list[dict]] = {}
            for record in cluster:
                by_source.setdefault(record["source"]["name"], []).append(record)
            canonical_source = min(
                by_source,
                key=lambda name: _source_priority(by_source[name][0], watches),
            )
            canonical = by_source[canonical_source]
            # ponytail: one-second callback matching; add persisted event IDs if
            # overlapping backends can timestamp the same event farther apart.
            for name, source_records in by_source.items():
                if name == canonical_source:
                    continue
                used: set[int] = set()
                for record in source_records:
                    candidates = [
                        (abs((item["timestamp"] - record["timestamp"]).total_seconds()), index)
                        for index, item in enumerate(canonical)
                        if index not in used
                    ]
                    if not candidates:
                        continue
                    delta, index = min(candidates)
                    if delta <= 1:
                        used.add(index)
                        duplicates.add(id(record["event"]))
    return duplicates


def _map_owned_event(event: dict, source: dict, owner: dict) -> dict:
    row = dict(event)
    row["watch"] = owner["name"]
    row["project"] = owner["project"]
    if owner["name"] != source["name"] and event.get("type") == "file":
        path = _absolute_event_path(event, source)
        row["path"] = os.path.relpath(path, owner["root"])
    return row


def _session_groups(events: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[dict] | None = None
    for event in events:
        if event.get("type") == "session_start":
            if current:
                groups.append(current)
            current = [event]
        elif current is not None:
            current.append(event)
            if (event.get("type") == "session_end"
                    and event.get("session") == current[0].get("session")):
                groups.append(current)
                current = None
    if current:
        groups.append(current)
    return groups


def _source_intervals(events_by_watch: dict[str, list[dict]], watches: list[dict],
                      duplicates: set[int]) -> list[dict]:
    intervals: list[dict] = []
    activity_types = {"file", "git_commit", "note_start", "note_stop"}
    for source in watches:
        for group in _session_groups(events_by_watch[source["name"]]):
            start_event = group[0]
            end_event = group[-1]
            start = _parse_ts(start_event.get("ts", ""))
            end = _parse_ts(end_event.get("ts", ""))
            rows = []
            for event in group:
                if event.get("type") not in activity_types or id(event) in duplicates:
                    continue
                owner = _event_owner(event, source, watches)
                rows.append(_map_owned_event(event, source, owner))
            if rows and end >= start:
                intervals.append({
                    "start": start,
                    "end": end,
                    "open": end_event.get("type") != "session_end",
                    "start_event": start_event,
                    "end_event": end_event,
                    "events": rows,
                })
    return intervals


def _cluster_intervals(intervals: list[dict]) -> list[dict]:
    clusters: list[dict] = []
    for interval in sorted(intervals, key=lambda item: (item["start"], item["end"])):
        if clusters and interval["start"] <= clusters[-1]["end"]:
            clusters[-1]["end"] = max(clusters[-1]["end"], interval["end"])
            clusters[-1]["intervals"].append(interval)
        else:
            clusters.append({
                "start": interval["start"],
                "end": interval["end"],
                "intervals": [interval],
            })
    return clusters


def _owned_segments(cluster: dict) -> list[dict]:
    rows = sorted(
        (row for interval in cluster["intervals"] for row in interval["events"]),
        key=lambda row: row.get("ts", ""),
    )
    by_owner: dict[str, dict] = {}
    for row in rows:
        segment = by_owner.setdefault(row["watch"], {
            "owner": row["watch"],
            "project": row["project"],
            "start": cluster["start"],
            "end": cluster["end"],
            "events": [],
            "minutes": set(),
        })
        segment["events"].append(row)
        segment["minutes"].add(row.get("ts", "")[:16])

    segments = list(by_owner.values())
    open_rows = [
        row for interval in cluster["intervals"] if interval["open"]
        for row in interval["events"]
    ]
    open_owner = max(open_rows, key=lambda row: row.get("ts", ""))["watch"] if open_rows else None
    if len(segments) == 1:
        segments[0]["open"] = segments[0]["owner"] == open_owner
        return segments
    total_minutes = (cluster["end"] - cluster["start"]).total_seconds() / 60
    total_weight = sum(max(1, len(segment["minutes"])) for segment in segments)
    allocated = 0.0
    for index, segment in enumerate(segments):
        owner_rows = segment["events"]
        segment["start"] = _parse_ts(owner_rows[0].get("ts", ""))
        segment["end"] = _parse_ts(owner_rows[-1].get("ts", ""))
        if index == len(segments) - 1:
            share = round(total_minutes - allocated, 1)
        else:
            share = round(total_minutes * max(1, len(segment["minutes"])) / total_weight, 1)
            allocated += share
        segment["allocated_duration_min"] = share
        segment["open"] = segment["owner"] == open_owner
    return segments


def load_owned_view(watches: list[dict], selected: str | None = None,
                    non_billable: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Load a non-destructive view owned by the most specific watch root."""
    events_by_watch = {
        watch["name"]: load_events(watch["log_dir"]) for watch in watches
    }
    duplicates = _duplicate_event_ids(events_by_watch, watches)
    clusters = _cluster_intervals(
        _source_intervals(events_by_watch, watches, duplicates)
    )
    owned_events: list[dict] = []
    for cluster in clusters:
        intervals = cluster["intervals"]
        first_interval = min(intervals, key=lambda interval: interval["start"])
        last_interval = max(intervals, key=lambda interval: interval["end"])
        for segment in _owned_segments(cluster):
            owner = segment["owner"]
            start_ts = segment["start"].isoformat()
            session_id = f"{owner}@{start_ts}"
            end_ts = segment["end"].isoformat()
            owned_events.append({
                "ts": start_ts,
                "type": "session_start",
                "session": session_id,
                "reason": first_interval["start_event"].get("reason", "activity"),
                "project": segment["project"],
                "watch": owner,
            })
            for row in segment["events"]:
                row["session"] = session_id
                owned_events.append(row)
            end_event = {
                "ts": end_ts,
                "type": "session_end",
                "session": session_id,
                "reason": last_interval["end_event"].get("reason", "ownership_split"),
                "project": segment["project"],
                "watch": owner,
                "open": segment.get("open", False),
            }
            if segment.get("allocated_duration_min") is not None:
                end_event["allocated_duration_min"] = segment["allocated_duration_min"]
            owned_events.append(end_event)

    owned_events.sort(key=lambda event: event.get("ts", ""))
    if selected:
        owned_events = [
            event for event in owned_events
            if event.get("watch") == selected
        ]
    sessions = _build_sessions_events(owned_events, non_billable=non_billable)
    return owned_events, sessions


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
