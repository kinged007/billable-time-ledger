---
title: Billable Time Ledger — PRD
status: draft
created: 2026-08-10
updated: 2026-08-10
project: billable-time-ledger
---

# Billable Time Ledger — Product Requirements Document

## 1. Context & Problem

The operator bills client projects on a time-and-materials basis. Roughly 99% of that work happens inside a **Coder workspace** (Docker container): VS Code in the web IDE (code-server), desktop VS Code over SSH, and AI coding agents (OpenCode today, others possible) running inside the container.

The SOW bills in **half-day increments** (8-hour day, ~10 days/month target). Today there is no automated record of time spent — no manual tracking system that fits the workflow.

**Requirement**: an automated, file-level **log timer** for Unix systems that records activity (file writes, git activity, working notes) so it can be audited and later summarized into billing reports — by a script now, by an agent later.

## 2. Goals

- **G1** — Record every relevant file write in the watched roots as a single, auditable ledger (human + agent activity, one entity)
- **G2** — Run on any Unix system: **Linux (incl. Docker containers) and macOS**
- **G3** — Start on system startup — **configurable** on/off
- **G4** — Configurable: watch roots (per-project segmentation), idle gap, excludes, agent patterns, billing params
- **G5** — Track **git activity** (writes, commits, commit messages) as part of the ledger
- **G6** — Classify actor as human / agent / other — **reference only**, never hard-coded to a specific agent, never blocks recording
- **G7** — Produce per-day / per-project reports; output must be **agent-readable** so an LLM can build narrative reports from the log
- **G8** — Local-first, private, free
- **G9** — Support lightweight **working notes** (`btl note "working on X"`) scoped to the active session, ending on idle-gap or explicit stop

## 3. Non-Goals

- **NG1** — No hard dependency on agent-vs-human attribution (classification is best-effort reference; recording never depends on it)
- **NG2** — No web dashboard in v1 (CLI + markdown reports)
- **NG3** — No cloud sync / hosted service
- **NG4** — No integration with external tools (WakaTime, Clockify, etc.)
- **NG5** — No read-access tracking (writes only — reads are noise)
- **NG6** — No repository coupling: config and logs live on the system, not in any repo
- **NG7** — No CSV export in v1 (JSON + Markdown only)

## 4. Key Decisions (from discussion)

- **D1** — **Cross-platform event backends**: Linux → fanotify via **fatrace** (events + PID + process name); macOS → **FSEvents** (file events; process attribution not available — actor degraded to unknown, acceptable since classification is reference-only)
- **D2** — **Single ledger** for human + agent activity (both billable)
- **D3** — **Write events only** (WRITE/CREATE/DELETE/MODIFY); READ/OPEN excluded
- **D4** — **Watch roots**: monitor activity *within* configured folders only (e.g. `~/workspace/`) — segments by project, avoids system-wide noise
- **D5** — **Git activity captured**: `.git` writes are kept and tagged; commit events (hash + message) recorded — commit messages provide the "why" for audit
- **D6** — Local storage: per-day JSONL logs inside the system data dir; **Coder workspace volumes are persisted** — no sync to `~/Sync/` needed
- **D7** — Config on the system (XDG), **completely decoupled from any repository**
- **D8** — Rejected **WakaTime**: free tier keeps only **1 week** of history; also only tracks editor heartbeats and misses agent writes
- **D9** — **Names**: CLI = `btl`; project folder = `billable-time-ledger` (full name)
- **D10** — **Idle gap**: default **15 minutes**, configurable
- **D11** — **Excludes confirmed**: `node_modules`, `.cache`, `__pycache__` excluded (system-ish, not authored content); `.git` kept
- **D12** — **No CSV** in v1 — JSON + Markdown only
- **D13** — **Working notes** (`btl note`) included: session-scoped; auto-stop on idle gap; explicit stop CLI
- **D14** — **Git message enrichment is the default** for explaining *why* work happened

## 5. User Story

> As the operator (sole user), I want the daemon to automatically log every file I touch (VS Code web/SSH) and every file the coding agent touches, plus git commits and occasional working notes, so that at any time I can audit exactly what was done and why — and point an agent at the log to produce an invoice-ready report. No manual tracking.

## 6. Features

### F1 — Activity capture (recorder)

- **FR-1** — Daemon runs continuously as a background process on the system
- **FR-2** — Captures events: timestamp (UTC), process name (where available), path, event type
- **FR-3** — Filters to WRITE/CREATE/DELETE/MODIFY; READ/OPEN excluded
- **FR-4** — Records interactive (code-server/VS Code/terminal) and agent (opencode, claude code, codex, …) processes into the **same ledger** — one billable entity
- **FR-5** — **Watch roots**: monitors only configured roots (e.g. `~/workspace/`, `~/projects/`); anything outside is ignored — project-folder segmentation, no system monitoring
- **FR-6** — Excludes configured noise paths within roots (default: `node_modules`, `.cache`, `__pycache__`) via config
- **FR-7** — Survives daemon restarts (append-only, resume on restart)

### F2 — Sessionization

- **FR-8** — Groups write events into work sessions using an **idle-gap threshold** (default **15 minutes**, configurable)
- **FR-9** — Each session carries: start, end, duration, files touched, dominant project, actor mix, notes

### F3 — Project mapping

- **FR-10** — Maps watch-root subpaths to project names via config (e.g. `~/workspace/project/` → `project`)
- **FR-11** — Paths inside roots but outside any mapped prefix roll into an "unmapped" bucket `[ASSUMPTION]`

### F4 — Git activity tracking

- **FR-12** — `.git` writes are captured and **tagged as git activity** (not noise)
- **FR-13** — Detects commits; records commit event: timestamp, repo/project, commit hash, **commit message**, author (if available)
- **FR-14** — Commit messages enrich the ledger so reports can explain *why* changes were made — **on by default** per root
- **FR-15** — Git tracking is configurable per root (default: on)

### F5 — Actor classification (reference only)

- **FR-16** — Each event optionally labeled: `human` | `agent` | `other` | `unknown`
- **FR-17** — Classification based on configurable process-name patterns; ships with defaults for common agents (opencode, claude, codex) and editors (code-server, vscode, terminal) — **not hard-coded**: users can add/remove patterns; unrecognized processes are recorded as `other`/`unknown`, never dropped
- **FR-18** — Classification never gates recording (fail-open by design)
- **FR-19** — On platforms without process attribution (macOS FSEvents), actor is `unknown` — ledger still complete

### F6 — Working notes

- **FR-20** — `btl note "working on X"` attaches a working note to the **current session**; if no session is active, it starts one with the note as its label
- **FR-21** — **Idle-gap auto-stop**: when the idle gap elapses (user no longer active), the session — and its note — ends; the note never bleeds into later sessions/logs
- **FR-22** — `btl note stop` (alias `btl note end`) explicitly ends the current note/session
- **FR-23** — Notes recorded as timestamped events in the ledger; carried into sessions, reports and exports
- **FR-24** — `btl notes --day ...` lists notes for a window

### F7 — Reporting & agent-readability

- **FR-25** — Daily report (markdown): sessions, total active time, files touched, per-project totals, git commits, notes
- **FR-26** — Monthly report: per-day totals, per-project totals, days worked vs SOW day target, half-day unit counts
- **FR-27** — CLI: `btl status`, `btl report --day 2026-08-10`, `btl report --month 2026-08`
- **FR-28** — **Agent-readable export**: stable, complete export (JSON or markdown digest) of the ledger for a given window — designed so an agent (Hermes, OpenCode, Claude) can be pointed at it to produce a narrative/invoice-ready report

### F8 — Autostart

- **FR-29** — Starts on system startup by default
- **FR-30** — **Configurable**: `btl autostart on|off`; Linux → systemd user service, macOS → launchd agent
- **FR-31** — Daemon self-heals (restart on crash via service manager)

### F9 — Configuration (system-decoupled)

- **FR-32** — Single YAML config at XDG location (`~/.config/btl/config.yaml`; macOS same convention)
- **FR-33** — Config is **not** tied to any repository — lives on the system, editable anytime
- **FR-34** — Env override: `BTL_CONFIG` for alternate config path
- **FR-35** — `btl config` prints effective config (defaults + overrides)
- **FR-36** — All settings configurable: watch roots, excludes, idle gap, agent patterns, billing params, log/output dirs

## 7. Non-Functional Requirements

- **NFR-1** — Performance: recorder overhead negligible (<1% CPU); fatrace is a C binary; FSEvents is OS-native
- **NFR-2** — Reliability: append-only logs, no data loss on crash; daily rotation
- **NFR-3** — Privacy: all data stays on the system (or in the persisted workspace volume)
- **NFR-4** — Cost: zero (free tooling)
- **NFR-5** — Portability: Python 3; Linux (fatrace) + macOS (FSEvents); systemd + launchd; no Docker-specific assumptions

## 8. Data Model

Per-day log file, JSONL, one event per line:

```json
{"ts": "2026-08-10T14:23:05Z", "pid": 1234, "proc": "opencode", "actor": "agent", "event": "WRITE", "path": "/home/user/workspace/project/src/scraper.py"}
{"ts": "2026-08-10T14:24:11Z", "proc": "code-server", "actor": "human", "event": "WRITE", "path": "/home/user/workspace/project/src/utils.py"}
```

Git commit event:

```json
{"ts": "2026-08-10T15:01:00Z", "type": "git_commit", "repo": "/home/user/workspace/project", "hash": "a1b2c3d", "message": "fix: normalize listing prices before ROI calc", "author": "operator"}
```

Note event:

```json
{"ts": "2026-08-10T14:30:00Z", "type": "note_start", "session": 7, "text": "refactoring ROI calc module"}
{"ts": "2026-08-10T15:45:00Z", "type": "note_stop", "session": 7, "reason": "idle_gap"}
```

Session summary (derived): `{start, end, duration_min, project, files: [paths], actors: {human: n, agent: n}, notes: [text]}`

## 9. Config Schema (proposed)

```yaml
watch:
  roots:
    - path: "/home/user/workspace"
      project: ""            # optional override; derived from dir name by default
      git: true              # capture git activity in this root
  exclude:
    - "node_modules"
    - ".cache"
    - "__pycache__"
idle_gap_min: 15
actors:
  agents:
    - "opencode"
    - "claude"
    - "codex"
  humans:
    - "code-server"
    - "vscode"
    - "node"                # your interactive sessions if applicable
billing:
  day_hours: 8
  increment_hours: 4        # half-day
  monthly_target_days: 10
log_dir: "~/.local/share/btl/logs"
report_dir: "~/.local/share/btl/reports"
autostart: true
```

## 10. CLI Surface (proposed)

```
btl status                    # daemon health, last event, active sessions
btl start | stop | restart    # manage daemon
btl autostart on|off          # systemd / launchd registration
btl note "working on X"       # attach note to current session (starts one if idle)
btl note stop                 # end current note/session explicitly
btl notes --day ...           # list notes for a window
btl report --day 2026-08-10   # daily markdown report
btl report --month 2026-08    # monthly markdown report
btl sessions --day ...        # raw session list
btl export --since ... --until ... --format json|md   # agent-readable export
btl config                    # show effective config
btl config edit               # open config in editor
```

## 11. Deployment

- Runs on the host system or inside a container — **no Docker-specific assumptions**
- Linux: systemd user service (`btl.service`); macOS: launchd LaunchAgent (`com.btl.daemon.plist`)
- Coder workspace: logs live in the persisted workspace volume — **no sync needed**
- Agent integration: point any agent at `btl export` output or the log dir for narrative/invoice reports

## 12. Open Questions

All previously raised questions resolved (see Key Decisions D9–D14):

- OQ-1 Name — **RESOLVED**: CLI `btl`; project folder `billable-time-ledger`
- OQ-2 Idle gap — **RESOLVED**: default 15 min, configurable
- OQ-3 Persistence — **RESOLVED**: system data dir / Coder persisted volume; no sync
- OQ-4 Excludes — **RESOLVED**: `node_modules`/`.cache`/`__pycache__` out; `.git` kept
- OQ-5 Config location — **RESOLVED**: system XDG config, repo-decoupled
- OQ-6 CSV — **RESOLVED**: skip in v1; JSON + Markdown only
- OQ-7 Notes — **RESOLVED**: `btl note` included; idle-gap auto-stop + explicit `btl note stop`

## 13. Future Ideas (parking lot)

- Web dashboard (view sessions from any device)
- Multi-machine merge (Linux VPS + Mac desktop)
- Integration with TickTick / Beads task DB
- Auto-generated weekly narrative report via agent
- Snapshot diff summary per session (which files changed how)
- CSV export (if ever needed)
