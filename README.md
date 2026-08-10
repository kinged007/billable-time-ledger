# btl — Billable Time Ledger

Automated file-level **log timer** for Unix systems (Linux + macOS). Records file writes, git commits and working notes into per-watch JSONL ledgers, then produces daily/monthly reports and agent-readable exports for billing.

- **Repo**: this project
- **Config & logs**: `~/.config/btl/` (never in a repo)
- **CLI**: `btl`

## Why

The operator bills client projects on a time & materials basis in half-day increments. Work happens inside a Coder workspace container (VS Code web/SSH + OpenCode agent). btl records all file activity as one billable entity — human and agent alike — so invoicing has an auditable ledger behind it. See `PRD.md` for the full spec.

## Features

- **File-write capture** — watches configured roots; records WRITE/CREATE/DELETE/MOVED events (reads are never tracked)
- **Git activity** — captures commits (hash + message + author) from `.git` signals
- **Working notes** — `btl note "working on X"`; auto-stops on idle gap, or `btl note stop`
- **Sessions** — grouped by idle gap (default 15 min); each session = billable block
- **Reports** — daily + monthly markdown, aligned to billing increments (half-days, day target)
- **Agent-readable export** — `btl export --format json|md` — point any agent at it for narrative/invoice reports
- **Multi-watch** — one daemon, many watches (one per project folder), each with its own config + logs
- **Autostart** — systemd user service (Linux) / launchd (macOS), `btl autostart on|off`

## Install

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e .
```

## Quick start

```bash
# add a watch (monitor a folder)
btl watch add workspace ~/workspace

# start the daemon (or: btl autostart on)
btl start
btl status

# track work
btl note "refactoring ROI calc module"
btl note stop

# reports & export
btl report --day 2026-08-10
btl report --month 2026-08
btl export --since 2026-08-10 --until 2026-08-17 --format md
```

## Config

`~/.config/btl/btl.yaml` (global defaults) + `~/.config/btl/watches/<name>/watch.yaml` (per watch). `BTL_CONFIG_ROOT` overrides the root. Logs co-locate per watch: `~/.config/btl/watches/<name>/logs/YYYY-MM-DD.jsonl`.

```yaml
# ~/.config/btl/btl.yaml
idle_gap_min: 15
exclude: [node_modules, .cache, __pycache__, .venv, .git]
actors:
  agents: [opencode, claude, codex, aider, gemini, cursor]
  humans: [code-server, vscode, code, node, python, bash, zsh, ssh]
billing:
  day_hours: 8
  increment_hours: 4
  monthly_target_days: 10
autostart: true
```

## How it works

- **Linux**: custom inotify backend with selective directory registration — only non-noise dirs get watches, so watching `~` stays far under the kernel inotify limit. `.git` dirs are watched only at refs/HEAD/logs (commit signals), never objects.
- **macOS**: watchdog FSEvents backend (single stream, recursive is cheap).
- Actor attribution: inotify/FSEvents don't expose the writing PID, so `actor` is `unknown` by default (fail-open per PRD FR-19). A fanotify/fatrace backend (root) could populate it — future enhancement.

## CLI

```
btl status                    # daemon health
btl start | stop | restart
btl autostart on|off
btl watch add <name> <root> [--idle-gap N] [--no-git]
btl watch list | remove <name>
btl note "text"               # attach to current session
btl note stop
btl notes --day ...
btl report --day|--month [--watch] [--out FILE]
btl sessions --day ...
btl export --since --until --format json|md [--out FILE]
btl config show | edit
```

## Development

```bash
.venv/bin/python -m btl.daemon_main --foreground   # run daemon in foreground
```
