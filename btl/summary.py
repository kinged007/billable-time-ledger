"""Cached per-watch totals used by terminal startup."""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from btl import sessionizer
from btl.config import non_billable_actors, summary_path

SUMMARY_VERSION = 1


def _number(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def build_summary(watches: list[dict], root: Path, now: datetime | None = None) -> dict:
    """Build one all-time billable total per configured watch."""
    non_billable = non_billable_actors(root)
    totals = []
    for watch in watches:
        log_dir = watch.get("log_dir")
        sessions = (
            sessionizer.build_sessions(str(log_dir), now=now, non_billable=non_billable)
            if log_dir else []
        )
        minutes = sessionizer.total_active_min(sessions)
        billing = watch.get("billing")
        if not isinstance(billing, dict):
            billing = {}
        day_hours = _number(billing.get("day_hours"), 8.0)
        if day_hours <= 0:
            day_hours = 8.0
        totals.append({
            "name": str(watch.get("name", "?")),
            "project": str(watch.get("project", watch.get("name", "?"))),
            "minutes": round(minutes, 1),
            "hours": round(minutes / 60, 2),
            "days": round(minutes / (day_hours * 60), 2),
            "day_hours": day_hours,
        })
    generated = now or datetime.now(timezone.utc)
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    return {
        "version": SUMMARY_VERSION,
        "generated_at": generated.astimezone(timezone.utc).isoformat(),
        "watches": totals,
    }


def write_summary(watches: list[dict], root: Path, now: datetime | None = None) -> Path:
    """Atomically publish the summary for cache-only readers."""
    data = build_summary(watches, root, now=now)
    path = summary_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return path


def read_summary(root: Path | None = None) -> dict | None:
    """Read and validate the cached summary without opening any log files."""
    try:
        with summary_path(root).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != SUMMARY_VERSION:
        return None
    raw_watches = data.get("watches")
    if not isinstance(raw_watches, list):
        return None

    watches = []
    for item in raw_watches:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        minutes = _number(item.get("minutes"), -1.0)
        if minutes < 0:
            continue
        days = _number(item.get("days"), -1.0)
        if days < 0:
            days = 0.0
        watches.append({
            "name": str(item["name"]),
            "project": str(item.get("project", item["name"])),
            "minutes": round(minutes, 1),
            "hours": round(minutes / 60, 2),
            "days": round(days, 2),
        })
    return {
        "generated_at": data.get("generated_at", ""),
        "watches": watches,
    }


def format_minutes(minutes: float) -> str:
    total = int(round(minutes))
    return f"{total // 60}h {total % 60:02d}m"


def format_summary(data: dict) -> list[str]:
    lines = []
    for watch in data.get("watches", []):
        days = f"{watch['days']:.2f}".rstrip("0").rstrip(".")
        lines.append(
            f"  {watch['name']:<18} {format_minutes(watch['minutes'])} "
            f"({days}d)"
        )
    return lines
