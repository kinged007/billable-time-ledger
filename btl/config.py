"""Configuration handling for btl.

Layout (all under the user config root, fully repo-decoupled):

    ~/.config/btl/
    ├── btl.yaml                  # global daemon defaults
    └── watches/
        └── <watch-name>/
            ├── watch.yaml        # this watch's own config (root, excludes, ...)
            └── logs/             # this watch's ledgers (co-located with config)
                ├── 2026-08-10.jsonl
                └── ...
"""

from __future__ import annotations

import copy
import os
import platform
from pathlib import Path
from typing import Any

import yaml

CONFIG_ROOT_ENV = "BTL_CONFIG_ROOT"
DEFAULT_CONFIG_ROOT = "~/.config/btl"

DEFAULT_IDLE_GAP_MIN = 15
DEFAULT_EXCLUDES = ["node_modules", ".cache", "__pycache__", ".venv", ".git", ".hermes", "WALLETS"]
DEFAULT_AGENTS = ["hermes", "opencode", "claude", "codex", "aider", "gemini", "cursor"]
DEFAULT_HUMANS = ["code-server", "vscode", "code", "node", "python", "bash", "zsh", "ssh", "nvim", "vim", "tmux"]
DEFAULT_SYSTEM = ["systemd", "systemd-tmpfiles", "rsync", "apt", "dpkg", "snapd", "flatpak"]
DEFAULT_NON_BILLABLE = ["cron"]
DEFAULT_BILLING = {"day_hours": 8, "increment_hours": 4}  # no target by default


def config_root() -> Path:
    """Resolve the btl config root (env override or default)."""
    env = os.environ.get(CONFIG_ROOT_ENV)
    if env:
        return Path(env).expanduser().resolve()
    return Path(DEFAULT_CONFIG_ROOT).expanduser().resolve()


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def load_global_config(root: Path | None = None) -> dict:
    """Load the global btl.yaml merged over defaults."""
    root = root or config_root()
    cfg = {
        "idle_gap_min": DEFAULT_IDLE_GAP_MIN,
        "exclude": list(DEFAULT_EXCLUDES),
        "process_tracking": True,
        "actors": {
            "agents": list(DEFAULT_AGENTS),
            "humans": list(DEFAULT_HUMANS),
            "system": list(DEFAULT_SYSTEM),
            "non_billable": list(DEFAULT_NON_BILLABLE),
            "exclude": [],
        },
        "billing": dict(DEFAULT_BILLING),
        "autostart": True,
        "log_dir": None,  # default: co-located per-watch logs/
        "report_dir": None,
        "socket_path": None,  # default: user daemon = config_root/btl.sock, root = /run/btl/btl.sock
    }
    return _deep_merge(cfg, _load_yaml(root / "btl.yaml"))


def resolved_socket_path(root: Path | None = None) -> str | None:
    """Explicit socket path from config, or None for the per-mode default."""
    root = root or config_root()
    g = load_global_config(root)
    v = g.get("socket_path")
    if not v:
        return None
    return str(Path(v).expanduser().resolve())


def non_billable_actors(root: Path | None = None) -> set[str]:
    """Actor names whose sessions don't count as billable (e.g. cron)."""
    root = root or config_root()
    g = load_global_config(root)
    return set(g.get("actors", {}).get("non_billable", []) or [])


def discover_watches(root: Path | None = None) -> list[str]:
    """Return sorted watch names configured under watches/."""
    root = root or config_root()
    wdir = root / "watches"
    if not wdir.exists():
        return []
    return sorted(
        p.name for p in wdir.iterdir()
        if p.is_dir() and (p / "watch.yaml").exists()
    )


def watch_dir(root: Path | None, name: str) -> Path:
    root = root or config_root()
    return root / "watches" / name


def load_watch_config(name: str, global_cfg: dict | None = None, root: Path | None = None) -> dict:
    """Load a single watch's config, merged over global defaults.

    Requires the watch to exist (has watch.yaml with a root).
    """
    root = root or config_root()
    g = global_cfg or load_global_config(root)
    wdir = watch_dir(root, name)
    raw = _load_yaml(wdir / "watch.yaml")

    watch_root = raw.get("root") or raw.get("path")
    if not watch_root:
        raise ValueError(f"watch '{name}' has no root configured in {wdir / 'watch.yaml'}")

    base = {
        "name": name,
        "root": str(Path(watch_root).expanduser().resolve()),
        "project": raw.get("project", name),       # default: watch name = project
        "git": raw.get("git", True),
        "idle_gap_min": raw.get("idle_gap_min", g["idle_gap_min"]),
        "exclude": list(raw.get("exclude", g["exclude"])),
        "billing": _deep_merge(g["billing"], raw.get("billing", {})),
        "log_dir": str((wdir / "logs").resolve()),
    }
    if raw.get("report_dir"):
        base["report_dir"] = str(Path(raw["report_dir"]).expanduser().resolve())
    return base


def load_all_watches(root: Path | None = None) -> list[dict]:
    root = root or config_root()
    g = load_global_config(root)
    return [load_watch_config(n, g, root) for n in discover_watches(root)]


def platform_default() -> str:
    return "darwin" if platform.system() == "Darwin" else "linux"


DEFAULT_GLOBAL_YAML = """\
# btl — global defaults (overridable per watch in watches/<name>/watch.yaml)
# All values here are the same as the built-in defaults; uncomment + edit to change.

idle_gap_min: 15
exclude:
  - node_modules
  - .cache
  - __pycache__
  - .venv
  - .git
  - .hermes
  - WALLETS
actors:
  agents:
    - hermes
    - opencode
    - claude
    - codex
    - aider
    - gemini
    - cursor
  humans:
    - code-server
    - vscode
    - code
    - node
    - python
    - bash
    - zsh
    - ssh
    - nvim
    - vim
    - tmux
  system:
    - systemd
    - systemd-tmpfiles
    - rsync
    - apt
    - dpkg
    - snapd
    - flatpak
  # actors whose sessions are excluded from billable time (still logged)
  non_billable:
    - cron
  # actors dropped from the ledger entirely (e.g. cron if you never want it)
  exclude: []
# process attribution (Linux fanotify, requires root daemon via
# 'btl setup-root'; inotify/FSEvents always report actor "unknown")
process_tracking: true
billing:
  day_hours: 8
  increment_hours: 4
autostart: true
# log_dir: "~/.local/share/btl/logs"   # default: co-located per-watch logs/
# report_dir: "~/.local/share/btl/reports"
# socket_path: "/run/btl/btl.sock"     # default: config_root/btl.sock (user), /run/btl/btl.sock (root)
"""


def ensure_layout(root: Path | None = None) -> Path:
    """Create config dirs + default global config if missing; return config root."""
    root = root or config_root()
    (root / "watches").mkdir(parents=True, exist_ok=True)
    gpath = root / "btl.yaml"
    if not gpath.exists():
        gpath.write_text(DEFAULT_GLOBAL_YAML, encoding="utf-8")
    return root
