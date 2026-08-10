"""IPC between CLI and daemon over a Unix socket.

Protocol: one JSON object per line, request {cmd, ...} -> response {ok, ...}.
Socket lives at <config_root>/btl.sock. Only local, user-owned.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path


def socket_path(root, socket: str | None = None) -> str:
    """Resolve the IPC socket path.

    Explicit config wins; otherwise the root daemon (/run/btl/btl.sock,
    user-accessible) vs user daemon (config_root/btl.sock).
    """
    if socket:
        return socket
    if os.geteuid() == 0:
        return "/run/btl/btl.sock"
    return str(Path(root) / "btl.sock")


def send_request(root, payload: dict, timeout: float = 3.0,
                 sock_path: str | None = None) -> dict:
    """Send one request to the daemon. Raises ConnectionError if not running."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path(root, sock_path))
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        if not buf:
            raise ConnectionError("daemon returned empty response")
        return json.loads(buf.decode("utf-8"))
    except FileNotFoundError:
        raise ConnectionError("btl daemon not running (socket missing)")
    finally:
        sock.close()


class IpcServer(threading.Thread):
    def __init__(self, root, handler, socket_path_str: str | None = None):
        super().__init__(daemon=True, name="btl-ipc")
        self.root = root
        self.handler = handler
        self.sock_path = socket_path_str or socket_path(root)
        self._stop = threading.Event()
        self._server: socket.socket | None = None

    def stop(self):
        self._stop.set()
        try:
            if self._server:
                self._server.close()
        except OSError:
            pass
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass

    def run(self):
        path = self.sock_path
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(8)
        os.chmod(path, 0o666 if os.geteuid() == 0 else 0o600)
        self._server = server
        while not self._stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket):
        try:
            conn.settimeout(5)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            if not buf:
                conn.close()
                return
            req = json.loads(buf.decode("utf-8"))
            try:
                resp = self.handler(req)
            except Exception as e:  # noqa: BLE001 - report back to CLI
                resp = {"ok": False, "error": str(e)}
            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        except Exception:
            pass
        finally:
            conn.close()
