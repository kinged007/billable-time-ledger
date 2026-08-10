"""Markdown reports (daily / monthly) and agent-readable exports."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone

from btl import sessionizer


def _day_of(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(
            timezone.utc
        ).strftime("%Y-%m-%d")
    except ValueError:
        return "????-??-??"


def _hm(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60}h{m % 60:02d}m"


def _billing_units(duration_min: float, increment_hours: float) -> float:
    """Billable units for a session/day (e.g. 4h half-days)."""
    if increment_hours <= 0:
        return 0.0
    return round(duration_min / (increment_hours * 60), 2)


def _actor_lines(actors: Counter, non_billable: set[str]) -> list[str]:
    if not actors:
        return ["_None._"]
    out = []
    for act, n in actors.most_common():
        nb = " (non-billable)" if act in non_billable else ""
        out.append(f"- {act}: {n} events{nb}")
    return out


def daily_report(log_dir: str, day: str, billing: dict | None = None,
                 watch: str = "", non_billable: set[str] | None = None) -> str:
    billing = billing or {}
    inc = float(billing.get("increment_hours", 4))
    non_billable = non_billable or set()
    events = [e for e in sessionizer.load_events(log_dir) if _day_of(e.get("ts", "")) == day]
    sessions = [s for s in sessionizer.build_sessions(
        log_dir, non_billable=non_billable) if _day_of(s["start"]) == day]
    files = Counter()
    commits = []
    notes = []
    actors = Counter()
    for e in events:
        if e.get("type") == "file":
            files[e.get("path", "")] += 1
            actors[e.get("actor", "unknown")] += 1
        elif e.get("type") == "git_commit":
            commits.append(e)
        elif e.get("type") == "note_start":
            notes.append(e)

    active = sessionizer.total_active_min(sessions)
    non_bill_min = sessionizer.total_active_min(
        sessions, billable_only=False) - active
    lines = [
        f"# btl daily report — {day}" + (f" · watch: {watch}" if watch else ""),
        "",
        f"- **Billable time** (session spans): {_hm(active)} ({len(sessions)} sessions)",
        f"- **Billable units** (increment {inc:g}h): {_billing_units(active, inc)}",
    ]
    if non_bill_min > 0:
        lines.append(f"- **Non-billable** (cron/system): {_hm(non_bill_min)}")
    lines += [
        f"- **Files touched**: {len(files)}",
        f"- **Commits**: {len(commits)}",
        f"- **Notes**: {len(notes)}",
        "",
        "## Sessions",
        "",
    ]
    if not sessions:
        lines.append("_No sessions._")
    for s in sessions:
        mark = " (open)" if s.get("open") else ""
        if not s.get("billable", True):
            mark += " (non-billable)"
        extra = ""
        if s.get("active_min") is not None:
            extra = f" / {_hm(s['active_min'])} writing"
        lines.append(
            f"- **#{s['id']}** {s['start'][11:16]}–{s['end'][11:16]} "
            f"({_hm(s['duration_min'])} span{extra})"
            f" · {s.get('project', '')} · "
            f"{len(s['files'])} files{mark}"
        )
        for n in s["notes"]:
            lines.append(f"  - note: {n.get('text', '')}")
    lines += ["", "## By actor", ""]
    lines += _actor_lines(actors, non_billable)
    lines += ["", "## Top files", ""]
    if files:
        for path, n in files.most_common(15):
            lines.append(f"- {n:>4}  {path}")
    else:
        lines.append("_None._")
    lines += ["", "## Commits", ""]
    if commits:
        for c in commits:
            lines.append(f"- `{c.get('hash', '')[:8]}` {c.get('message', '')} — {c.get('author', '')}")
    else:
        lines.append("_None._")
    lines += ["", "## Notes", ""]
    if notes:
        for n in notes:
            lines.append(f"- {n.get('ts', '')[11:16]} {n.get('text', '')}")
    else:
        lines.append("_None._")
    return "\n".join(lines) + "\n"


def monthly_report(log_dir: str, month: str, billing: dict | None = None,
                   watch: str = "", non_billable: set[str] | None = None) -> str:
    """month = 'YYYY-MM'"""
    billing = billing or {}
    inc = float(billing.get("increment_hours", 4))
    target = float(billing.get("monthly_target_days", 10))
    day_hours = float(billing.get("day_hours", 8))
    non_billable = non_billable or set()

    events = [e for e in sessionizer.load_events(log_dir)
              if e.get("ts", "").startswith(month)]
    sessions = [s for s in sessionizer.build_sessions(
        log_dir, non_billable=non_billable)
                if s["start"].startswith(month)]

    per_day: dict[str, float] = defaultdict(float)
    per_project: dict[str, float] = defaultdict(float)
    actors = Counter()
    commits = [e for e in events if e.get("type") == "git_commit"]
    for s in sessions:
        if not s.get("billable", True):
            continue
        span = s.get("duration_min", 0)   # billable: full session block
        per_day[_day_of(s["start"])] += span
        per_project[s.get("project", "?")] += span
    for e in events:
        if e.get("type") == "file":
            actors[e.get("actor", "unknown")] += 1

    total = sessionizer.total_active_min(sessions)
    non_bill_min = sessionizer.total_active_min(
        sessions, billable_only=False) - total
    days_worked = len(per_day)
    full_days = total / (day_hours * 60)
    units = _billing_units(total, inc)

    lines = [
        f"# btl monthly report — {month}" + (f" · watch: {watch}" if watch else ""),
        "",
        f"- **Billable time** (session spans): {_hm(total)}",
        f"- **Days worked**: {days_worked}",
        f"- **Full days equivalent** ({day_hours:g}h days): {full_days:.1f}",
        f"- **Billable units** ({inc:g}h): {units}",
        f"- **Target** ({target:g} days): {'met ✓' if days_worked >= target else f'{target - days_worked:.0f} days short'}",
        f"- **Commits**: {len(commits)}",
        "",
    ]
    if non_bill_min > 0:
        lines.append(f"- **Non-billable** (cron/system): {_hm(non_bill_min)}")
    lines += ["## Per day", ""]
    if per_day:
        for d in sorted(per_day):
            lines.append(f"- {d}  {_hm(per_day[d])}")
    else:
        lines.append("_No activity._")
    lines += ["", "## Per project", ""]
    if per_project:
        for p, m in sorted(per_project.items(), key=lambda x: -x[1]):
            lines.append(f"- {p}  {_hm(m)}")
    else:
        lines.append("_None._")
    lines += ["", "## By actor", ""]
    lines += _actor_lines(actors, non_billable)
    lines += ["", "## Commits", ""]
    if commits:
        for c in commits:
            lines.append(f"- `{c.get('hash', '')[:8]}` {c.get('message', '')} — {c.get('author', '')}")
    else:
        lines.append("_None._")
    return "\n".join(lines) + "\n"


def _norm_bound(v: str | None, end_of_day: bool) -> str | None:
    """Normalize a --since/--until bound: bare dates get padded so string
    comparison against full ISO timestamps works."""
    if not v:
        return None
    if len(v) == 10:  # YYYY-MM-DD
        return v + ("T23:59:59.999999" if end_of_day else "T00:00:00")
    return v


def export_json(log_dir: str, since: str | None = None, until: str | None = None,
                include_raw: bool = False,
                non_billable: set[str] | None = None) -> str:
    import json
    since = _norm_bound(since, end_of_day=False)
    until = _norm_bound(until, end_of_day=True)
    events = sessionizer.load_events(log_dir)
    if since:
        events = [e for e in events if e.get("ts", "") >= since]
    if until:
        events = [e for e in events if e.get("ts", "") <= until]
    sessions = sessionizer.build_sessions(log_dir, non_billable=non_billable)
    if include_raw:
        return json.dumps({"events": events, "sessions": sessions}, indent=2)
    return json.dumps({"sessions": sessions}, indent=2)


def export_markdown(log_dir: str, since: str | None = None, until: str | None = None,
                    include_raw: bool = False,
                    non_billable: set[str] | None = None) -> str:
    since = _norm_bound(since, end_of_day=False)
    until = _norm_bound(until, end_of_day=True)
    events = sessionizer.load_events(log_dir)
    if since:
        events = [e for e in events if e.get("ts", "") >= since]
    if until:
        events = [e for e in events if e.get("ts", "") <= until]
    sessions = [s for s in sessionizer.build_sessions(
        log_dir, non_billable=non_billable)
                if (not since or s["start"] >= since) and (not until or s["end"] <= until)]
    lines = [
        "# btl export",
        "",
        f"- Sessions: {len(sessions)}",
        f"- Billable (session spans): {_hm(sessionizer.total_active_min(sessions))}",
        "",
        "## Sessions",
        "",
    ]
    for s in sessions:
        mark = " (open)" if s.get("open") else ""
        if not s.get("billable", True):
            mark += " (non-billable)"
        extra = ""
        if s.get("active_min") is not None:
            extra = f" / {_hm(s['active_min'])} writing"
        lines.append(
            f"- **#{s['id']}** {s['start']} → {s['end']} "
            f"({_hm(s['duration_min'])} span{extra})"
            f" · {s.get('project', '')}{mark}"
        )
        for n in s["notes"]:
            lines.append(f"  - note: {n.get('text', '')}")
        for c in s["commits"]:
            lines.append(f"  - commit: `{c.get('hash', '')[:8]}` {c.get('message', '')}")
    if include_raw:
        lines += ["", "## Raw events", ""]
        for e in events:
            etype = e.get("type", "file")
            if etype == "file":
                lines.append(f"- {e.get('ts', '')} {e.get('event', '')} {e.get('path', '')}")
            elif etype in ("note_start", "note_stop", "session_start", "session_end", "git_commit"):
                lines.append(f"- {e.get('ts', '')} {etype}: {json_dumps(e)}")
    return "\n".join(lines) + "\n"


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
