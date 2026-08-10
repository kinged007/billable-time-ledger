"""btl CLI — argparse-based."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from btl import __version__, config as cfg, ipc, report, sessionizer

DAEMON_MODULE = "btl.daemon_main"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _this_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _find_watch_logdir(name: str | None, require: bool = True) -> tuple[str, dict]:
    watches = cfg.load_all_watches()
    if name:
        w = next((w for w in watches if w["name"] == name), None)
        if not w:
            sys.exit(f"error: unknown watch '{name}' — available: {[x['name'] for x in watches]}")
        return w["log_dir"], w
    if len(watches) == 1:
        return watches[0]["log_dir"], watches[0]
    if require and not watches:
        sys.exit("error: no watches configured — see 'btl watch add'")
    if require:
        sys.exit(f"error: multiple watches — specify one: {[x['name'] for x in watches]}")
    return "", {}


# -- watch management ------------------------------------------------

def cmd_watch_add(args) -> int:
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        sys.exit(f"error: root does not exist: {root}")
    cfg_root = cfg.config_root()
    cfg.ensure_layout(cfg_root)
    wdir = cfg_root / "watches" / args.name
    if wdir.exists():
        sys.exit(f"error: watch '{args.name}' already exists")
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "logs").mkdir(exist_ok=True)
    lines = [
        f"# watch: {args.name}",
        f"root: \"{root}\"",
    ]
    if args.project:
        lines.append(f"project: \"{args.project}\"")
    if args.idle_gap is not None:
        lines.append(f"idle_gap_min: {args.idle_gap}")
    if args.no_git:
        lines.append("git: false")
    lines.append("")
    (wdir / "watch.yaml").write_text("\n".join(lines), encoding="utf-8")
    print(f"added watch '{args.name}' → {wdir}")
    print(f"  root: {root}")
    print("restart the daemon to pick it up: btl restart")
    return 0


def cmd_watch_list(args) -> int:
    watches = cfg.load_all_watches()
    if not watches:
        print("no watches configured")
        return 0
    for w in watches:
        print(f"{w['name']:<20} {w['root']:<50} project={w['project']} idle={w['idle_gap_min']}m")
    return 0


def cmd_watch_remove(args) -> int:
    wdir = cfg.watch_dir(None, args.name)
    if not (wdir / "watch.yaml").exists():
        sys.exit(f"error: watch '{args.name}' not found")
    import shutil
    shutil.rmtree(wdir)
    print(f"removed watch '{args.name}' (including its logs)")
    return 0


# -- daemon lifecycle ------------------------------------------------

def cmd_status(args) -> int:
    root = cfg.config_root()
    sock = cfg.resolved_socket_path()
    try:
        resp = ipc.send_request(root, {"cmd": "status"}, sock_path=sock)
    except ConnectionError:
        print("btl daemon: not running")
        return 1
    pt = resp.get("process_tracking")
    pt_s = f", process tracking {'ON' if pt else 'OFF'}" if pt is not None else ""
    print(f"btl daemon: running (config: {resp['config_root']}{pt_s})")
    for w in resp["watches"]:
        note = f" · note: {w['note']}" if w["note"] else ""
        sess = "ACTIVE" if w["active_session"] else "idle"
        print(f"  {w['name']:<18} {sess:<8} backend={w.get('backend', '?'):<9} "
              f"events={w['events_logged']} root={w['root']}{note}")
    return 0


def cmd_start(args) -> int:
    """Start the daemon as a background process (detached)."""
    root = cfg.config_root()
    try:
        resp = ipc.send_request(root, {"cmd": "status"}, timeout=1,
                                sock_path=cfg.resolved_socket_path())
        if resp.get("running"):
            print("btl daemon already running")
            return 0
    except ConnectionError:
        pass
    python = sys.executable
    logfile = root / "daemon.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)
    devnull = open(os.devnull, "w")
    proc = subprocess.Popen(
        [python, "-m", DAEMON_MODULE, "--foreground"],
        stdin=devnull, stdout=logfile.open("a"), stderr=subprocess.STDOUT,
        start_new_session=True, cwd=str(Path.home()),
    )
    # wait for socket
    import time
    for _ in range(50):
        time.sleep(0.1)
        try:
            resp = ipc.send_request(root, {"cmd": "status"}, timeout=0.5,
                                    sock_path=cfg.resolved_socket_path())
            if resp.get("running"):
                print(f"btl daemon started (pid {proc.pid}, logs: {logfile})")
                return 0
        except ConnectionError:
            continue
    print(f"btl daemon failed to start — check {logfile}")
    return 1


def cmd_stop(args) -> int:
    root = cfg.config_root()
    try:
        resp = ipc.send_request(root, {"cmd": "shutdown"},
                                sock_path=cfg.resolved_socket_path())
    except ConnectionError:
        print("btl daemon: not running")
        return 1
    print("btl daemon stopped")
    return 0


def cmd_restart(args) -> int:
    root = cfg.config_root()
    try:
        resp = ipc.send_request(root, {"cmd": "restart"},
                                sock_path=cfg.resolved_socket_path())
    except ConnectionError as e:
        print(f"error: {e}")
        return 1
    if resp.get("ok"):
        unit = resp.get("unit", "btl.service")
        print(f"restarting daemon via {unit} (config reload)...")
        return 0
    print(f"error: {resp.get('error', 'restart failed')}")
    return 1


def cmd_autostart(args) -> int:
    import platform
    root = cfg.config_root()
    system = platform.system()
    if args.mode not in ("on", "off"):
        sys.exit("usage: btl autostart on|off")
    cfg_root = cfg.config_root()
    if system == "Linux":
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit = unit_dir / "btl.service"
        if args.mode == "on":
            unit.write_text(
                "[Unit]\nDescription=btl — Billable Time Ledger daemon\n"
                "After=default.target\n\n"
                "[Service]\nType=simple\n"
                f"ExecStart={sys.executable} -m {DAEMON_MODULE} --foreground\n"
                "Restart=on-failure\nRestartSec=5\n\n"
                "[Install]\nWantedBy=default.target\n",
                encoding="utf-8",
            )
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            subprocess.run(["systemctl", "--user", "enable", "btl.service"], check=False)
            subprocess.run(["systemctl", "--user", "start", "btl.service"], check=False)
            print("autostart enabled (systemd user unit btl.service) + started")
        else:
            subprocess.run(["systemctl", "--user", "disable", "--now", "btl.service"], check=False)
            if unit.exists():
                unit.unlink()
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            print("autostart disabled")
    elif system == "Darwin":
        launch_agents = Path.home() / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True, exist_ok=True)
        plist = launch_agents / "com.btl.daemon.plist"
        if args.mode == "on":
            plist.write_text(
                "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
                "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
                "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
                "<plist version=\"1.0\"><dict>\n"
                "  <key>Label</key><string>com.btl.daemon</string>\n"
                "  <key>ProgramArguments</key><array>\n"
                f"    <string>{sys.executable}</string><string>-m</string>"
                f"<string>{DAEMON_MODULE}</string><string>--foreground</string>\n"
                "  </array>\n"
                "  <key>RunAtLoad</key><true/>\n"
                "  <key>KeepAlive</key><true/>\n"
                "</dict></plist>\n",
                encoding="utf-8",
            )
            subprocess.run(["launchctl", "load", str(plist)], check=False)
            print("autostart enabled (launchd com.btl.daemon) + loaded")
        else:
            subprocess.run(["launchctl", "unload", str(plist)], check=False)
            if plist.exists():
                plist.unlink()
            print("autostart disabled")
    else:
        print(f"autostart not supported on {system}")
        return 1
    return 0


def _setup_root_unit(sudo_user: str, btl_bin: str, cfg_root: str,
                     uid: int, gid: int) -> str:
    """Return the btl-root.service unit content for the given user."""
    return f"""[Unit]
Description=btl — Billable Time Ledger daemon (root, fanotify process tracking)
After=multi-user.target

[Service]
Type=simple
User=root
Environment=BTL_CONFIG_ROOT={cfg_root}
Environment=BTL_LOG_OWNER={uid}:{gid}
ExecStart={btl_bin} --foreground
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def cmd_setup_root(args) -> int:
    """Install the root fanotify daemon (system unit) for process tracking.

    Run as:  sudo btl setup-root
    Replaces the user-level btl.service with btl-root.service.
    """
    import platform
    import pwd
    if platform.system() != "Linux":
        sys.exit("setup-root is Linux-only (fanotify)")
    if os.geteuid() != 0:
        print("btl setup-root must run as root:")
        print("  sudo btl setup-root")
        return 1
    sudo_user = os.environ.get("SUDO_USER") or os.environ.get("USER")
    if not sudo_user or sudo_user == "root":
        sys.exit("cannot determine the target user (run via sudo)")
    try:
        pw = pwd.getpwnam(sudo_user)
    except KeyError:
        sys.exit(f"unknown user: {sudo_user}")
    home = pw.pw_dir
    btl_bin = os.path.join(home, ".local", "bin", "btl-daemon")
    if not os.path.exists(btl_bin):
        sys.exit(f"btl-daemon not found for {sudo_user}: {btl_bin}")
    cfg_root = os.path.join(home, ".config", "btl")
    unit_path = "/etc/systemd/system/btl-root.service"
    unit = _setup_root_unit(sudo_user, btl_bin, cfg_root, pw.pw_uid, pw.pw_gid)
    Path(unit_path).write_text(unit, encoding="utf-8")
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    # stop the user-level daemon so only the root one runs. systemctl --user
    # needs the user session env — run it as the target user with
    # XDG_RUNTIME_DIR set (root can sudo to the user without a password).
    r = subprocess.run(
        ["sudo", "-u", sudo_user, "env",
         f"XDG_RUNTIME_DIR=/run/user/{pw.pw_uid}",
         "systemctl", "--user", "disable", "--now", "btl.service"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"warning: could not stop user daemon ({r.returncode}):")
        print("  run manually: systemctl --user stop btl.service")
    subprocess.run(
        ["systemctl", "enable", "--now", "btl-root.service"], check=False,
    )
    print(f"installed {unit_path}")
    print(f"  config root : {cfg_root} (BTL_CONFIG_ROOT)")
    print(f"  log owner   : {pw.pw_uid}:{pw.pw_gid} (BTL_LOG_OWNER)")
    print("  daemon      : btl-root.service (enabled + started)")
    print("")
    print("user daemon btl.service stopped. verify with:  btl status")
    print("(socket moves to /run/btl/btl.sock; CLI works unchanged)")
    return 0


# -- notes ------------------------------------------------------------

def cmd_note(args) -> int:
    root = cfg.config_root()
    try:
        resp = ipc.send_request(
            root, {"cmd": "note", "text": args.text, "watch": args.watch},
            sock_path=cfg.resolved_socket_path())
    except ConnectionError as e:
        print(f"error: {e}")
        return 1
    print(f"note attached → watch {resp['watch']}, session #{resp['session']}")
    return 0


def cmd_note_stop(args) -> int:
    root = cfg.config_root()
    try:
        resp = ipc.send_request(
            root, {"cmd": "note_stop", "watch": args.watch},
            sock_path=cfg.resolved_socket_path())
    except ConnectionError as e:
        print(f"error: {e}")
        return 1
    if resp.get("note_stopped"):
        print("note stopped")
    else:
        print("no active note")
    return 0


def cmd_session_stop(args) -> int:
    root = cfg.config_root()
    try:
        resp = ipc.send_request(
            root, {"cmd": "session_stop", "watch": args.watch},
            sock_path=cfg.resolved_socket_path())
    except ConnectionError as e:
        print(f"error: {e}")
        return 1
    print("session stopped")
    return 0


def cmd_notes(args) -> int:
    logdir, w = _find_watch_logdir(args.watch)
    day = args.day or _today()
    events = [e for e in sessionizer.load_events(logdir)
              if e.get("type") == "note_start" and e.get("ts", "").startswith(day)]
    if not events:
        print(f"no notes for {day}")
        return 0
    for e in events:
        print(f"{e['ts'][11:16]}  {e.get('text', '')}")
    return 0


# -- reports ----------------------------------------------------------

def cmd_report(args) -> int:
    logdir, w = _find_watch_logdir(args.watch)
    g = cfg.load_global_config()
    billing = {**g.get("billing", {}), **w.get("billing", {})}
    nb = cfg.non_billable_actors()
    if args.day:
        out = report.daily_report(logdir, args.day, billing, watch=w["name"],
                                  non_billable=nb)
    else:
        month = args.month or _this_month()
        out = report.monthly_report(logdir, month, billing, watch=w["name"],
                                    non_billable=nb)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(out)
    return 0


def cmd_sessions(args) -> int:
    logdir, w = _find_watch_logdir(args.watch)
    day = args.day or _today()
    sessions = [s for s in sessionizer.build_sessions(
        logdir, non_billable=cfg.non_billable_actors())
        if s["start"].startswith(day)]
    if not sessions:
        print(f"no sessions for {day}")
        return 0
    print(f"{'session':<14} {'window':<14} {'active':<8} {'span':<7} files")
    for s in sessions:
        mark = " ●" if s.get("open") else ""
        if not s.get("billable", True):
            mark += " (non-billable)"
        print(
            f"#{s['id']} {s['start'][11:16]}–{s['end'][11:16]} "
            f"{s['active_min']:6.1f}m  {s['duration_min']:6.1f}m  "
            f"{len(s['files']):>4}{mark}"
        )
    total_a = sessionizer.total_active_min(sessions, billable_only=True)
    total_nb = sessionizer.total_active_min(sessions, billable_only=False) - total_a
    total_d = round(sum(s.get("duration_min", 0) for s in sessions), 1)
    print(f"\ntotal: {total_a:.1f}m active / {total_d:.1f}m span (● = open)")
    if total_nb > 0:
        print(f"non-billable: {total_nb:.1f}m (cron/system)")
    return 0


def cmd_export(args) -> int:
    logdir, w = _find_watch_logdir(args.watch)
    fmt = args.format or "json"
    nb = cfg.non_billable_actors()
    if fmt == "json":
        out = report.export_json(logdir, args.since, args.until,
                                 include_raw=args.include_raw, non_billable=nb)
    else:
        out = report.export_markdown(logdir, args.since, args.until,
                                     include_raw=args.include_raw,
                                     non_billable=nb)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(out)
    return 0


# -- config -----------------------------------------------------------

def cmd_config(args) -> int:
    root = cfg.config_root()
    gpath = root / "btl.yaml"
    if args.action == "show":
        print(f"config root: {root}")
        print(f"global:      {gpath} ({'exists' if gpath.exists() else 'defaults'})")
        print("watches:     ", ", ".join(cfg.discover_watches(root)) or "none")
        print("---")
        g = cfg.load_global_config(root)
        print(f"idle_gap_min: {g['idle_gap_min']}")
        print(f"exclude: {', '.join(g['exclude'])}")
        print(f"actors.agents: {', '.join(g['actors']['agents'])}")
        print(f"actors.humans: {', '.join(g['actors']['humans'])}")
        print(f"actors.system: {', '.join(g['actors'].get('system', []))}")
        print(f"actors.non_billable: {', '.join(g['actors'].get('non_billable', []))}")
        print(f"actors.exclude: {', '.join(g['actors'].get('exclude', [])) or '-'}")
        print(f"process_tracking: {g.get('process_tracking', True)}")
        print(f"socket_path: {g.get('socket_path') or '(default)'}")
        b = g["billing"]
        print(f"billing: {b['day_hours']}h days / {b['increment_hours']}h increment / {b['monthly_target_days']} days target")
        print(f"autostart: {g.get('autostart', True)}")
        if gpath.exists():
            print("--- file ---")
            print(gpath.read_text(encoding="utf-8"))
    elif args.action == "edit":
        editor = os.environ.get("EDITOR", "nano")
        subprocess.run([editor, str(gpath)], check=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="btl",
        description="Billable Time Ledger — automated file-level activity logger",
    )
    p.add_argument("--version", action="version", version=f"btl {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("watch", help="manage watches")
    wsub = sp.add_subparsers(dest="watch_cmd", required=True)
    wa = wsub.add_parser("add", help="add a watch (root folder)")
    wa.add_argument("name")
    wa.add_argument("root")
    wa.add_argument("--project", default=None)
    wa.add_argument("--idle-gap", type=float, default=None, dest="idle_gap")
    wa.add_argument("--no-git", action="store_true", dest="no_git")
    wa.set_defaults(func=cmd_watch_add)
    wl = wsub.add_parser("list", help="list watches")
    wl.set_defaults(func=cmd_watch_list)
    wr = wsub.add_parser("remove", help="remove a watch")
    wr.add_argument("name")
    wr.set_defaults(func=cmd_watch_remove)

    sub.add_parser("status", help="daemon status").set_defaults(func=cmd_status)
    sub.add_parser("start", help="start daemon").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop daemon").set_defaults(func=cmd_stop)
    sub.add_parser("restart", help="restart daemon (reload config)").set_defaults(func=cmd_restart)
    sa = sub.add_parser("autostart", help="configure startup autostart")
    sa.add_argument("mode", choices=["on", "off"])
    sa.set_defaults(func=cmd_autostart)
    sr = sub.add_parser(
        "setup-root",
        help="install root fanotify daemon for process tracking (needs sudo)",
    )
    sr.set_defaults(func=cmd_setup_root)

    n = sub.add_parser("note", help="attach a working note to the current session")
    n.add_argument("text")
    n.add_argument("--watch", default=None)
    n.set_defaults(func=cmd_note)
    ns = sub.add_parser("note-stop", help="stop current note")
    ns.add_argument("--watch", default=None)
    ns.set_defaults(func=cmd_note_stop)
    ss = sub.add_parser("session-stop", help="stop current session explicitly")
    ss.add_argument("--watch", default=None)
    ss.set_defaults(func=cmd_session_stop)
    nl = sub.add_parser("notes", help="list notes for a day")
    nl.add_argument("--day", default=None)
    nl.add_argument("--watch", default=None)
    nl.set_defaults(func=cmd_notes)

    r = sub.add_parser("report", help="daily/monthly report")
    r.add_argument("--day", default=None)
    r.add_argument("--month", default=None)
    r.add_argument("--watch", default=None)
    r.add_argument("--out", default=None)
    r.set_defaults(func=cmd_report)

    se = sub.add_parser("sessions", help="list sessions for a day")
    se.add_argument("--day", default=None)
    se.add_argument("--watch", default=None)
    se.set_defaults(func=cmd_sessions)

    ex = sub.add_parser("export", help="agent-readable export (json|md)")
    ex.add_argument("--since", default=None)
    ex.add_argument("--until", default=None)
    ex.add_argument("--format", choices=["json", "md"], default="json")
    ex.add_argument("--include-raw", action="store_true",
                    help="include raw file events (default: sessions summary only)")
    ex.add_argument("--watch", default=None)
    ex.add_argument("--out", default=None)
    ex.set_defaults(func=cmd_export)

    c = sub.add_parser("config", help="show/edit config")
    c.add_argument("action", choices=["show", "edit"], default="show", nargs="?")
    c.set_defaults(func=cmd_config)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
