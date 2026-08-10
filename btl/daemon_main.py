"""Daemon entrypoint: wires Daemon + IPC server + commit capture.

Run with:  python -m btl.daemon_main --foreground
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

from btl import config as cfg
from btl import ipc
from btl.daemon import Daemon, capture_commits
from btl.ipc import IpcServer


def handle_request(daemon: Daemon, req: dict) -> dict:
    cmd = req.get("cmd")
    if cmd == "status":
        return daemon.cmd_status()
    if cmd == "note":
        return daemon.cmd_note(req.get("text", ""), req.get("watch"))
    if cmd == "note_stop":
        return daemon.cmd_note_stop(req.get("watch"))
    if cmd == "session_stop":
        return daemon.cmd_session_stop(req.get("watch"))
    if cmd == "restart":
        return daemon.cmd_restart()
    if cmd == "shutdown":
        threading.Thread(target=daemon.stop, daemon=True).start()
        return {"ok": True, "shutting_down": True}
    return {"ok": False, "error": f"unknown command: {cmd}"}


def main() -> int:
    ap = argparse.ArgumentParser(prog="btl-daemon")
    ap.add_argument("--foreground", action="store_true", help="stay in foreground")
    args = ap.parse_args()

    try:
        daemon = Daemon()
    except ValueError as e:
        print(f"btl daemon: {e}", file=sys.stderr)
        return 1

    root = cfg.config_root()
    sock = ipc.socket_path(root, cfg.resolved_socket_path())
    if os.geteuid() == 0 and sock == "/run/btl/btl.sock":
        os.makedirs("/run/btl", exist_ok=True)
    ipc_server = IpcServer(root, lambda req: handle_request(daemon, req),
                           socket_path_str=sock)
    ipc_server.start()
    daemon.start()
    commits = threading.Thread(
        target=capture_commits, args=(daemon.states,), daemon=True
    )
    commits.start()

    print(f"btl daemon running (pid {__import__('os').getpid()}) — config {root}")
    try:
        while True:
            import time
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        ipc_server.stop()
        daemon.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
