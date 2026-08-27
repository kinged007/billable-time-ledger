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
- **Process attribution** — Linux: `sudo btl setup-root` enables a root fanotify daemon that records *which process* made each change (hermes, cron, editor...). Cron/systemd-timer activity is detected and tagged `cron` — kept in the ledger but excluded from billable time by default (`actors.non_billable`)

## Install

```bash
make install
```

This builds `.venv` (pinned to a uv-managed Python — see `.python-version` —
so a distro Python upgrade can't strand it), symlinks `btl`/`btl-daemon` into
`~/.local/bin`, and wires up autostart: a native systemd user unit / launchd
agent when one is available, otherwise a `~/.bashrc` hook (see
`scripts/btl-autostart.sh`) that starts the daemon and prints its status on
every new shell. `make uninstall` reverses it (config in `~/.config/btl` is
left alone).

Equivalent manual steps, if you'd rather not use `make`:

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
  agents: [hermes, opencode, claude, codex, aider, gemini, cursor]
  humans: [code-server, vscode, code, node, python, bash, zsh, ssh]
  system: [systemd, apt, rsync]
  non_billable: [cron]
  exclude: []
process_tracking: true
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

- venv: `.venv/bin/python`; package: `btl/`; entrypoints `btl` + `btl-daemon`
- Tests: `python -m compileall btl && python /tmp/btl_test_02.py` (process attribution, no root needed)

## Process attribution (Linux, optional)

Without it, actor is `unknown` on every event (inotify/FSEvents expose no PID).
With it, every event records `actor`, `proc`, `exe`, `unit`:

```bash
sudo btl setup-root        # installs btl-root.service (system unit, fanotify, root)
btl status                 # → backend=fanotify, process tracking ON
```

The root daemon reads the same `~/.config/btl/` config (via `BTL_CONFIG_ROOT`),
writes logs chowned to your uid (`BTL_LOG_OWNER`), and listens on
`/run/btl/btl.sock` — your user CLI keeps working with **no sudo after setup**.
`btl restart` reloads config through the unit. `sudo systemctl disable --now
btl-root.service` reverts to the user daemon.
