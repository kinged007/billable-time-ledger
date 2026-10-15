import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from btl import cli, config as cfg, report, sessionizer
from btl.daemon import Daemon


class MultiRootWatchTests(unittest.TestCase):
    def test_cli_writes_multiple_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            roots = [base / "one" / "demo-project", base / "two" / "demo-project-2"]
            for root in roots:
                root.mkdir(parents=True)
            config_root = base / "config"

            with patch.dict(os.environ, {"BTL_CONFIG_ROOT": str(config_root)}):
                args = cli.build_parser().parse_args(
                    ["watch", "add", "demo-project", *map(str, roots)]
                )
                with redirect_stdout(io.StringIO()):
                    cli.cmd_watch_add(args)
                watches = cfg.load_all_watches()

            self.assertEqual(len(watches), 1)
            self.assertEqual(watches[0]["roots"], list(map(str, roots)))
            self.assertEqual(watches[0]["root"], str(base))

    def test_legacy_root_config_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "legacy"
            root.mkdir()
            config_root = base / "config"
            watch_dir = cfg.watch_dir(config_root, "legacy")
            watch_dir.mkdir(parents=True)
            (watch_dir / "watch.yaml").write_text(f"root: {root}\n", encoding="utf-8")

            with patch.dict(os.environ, {"BTL_CONFIG_ROOT": str(config_root)}):
                watch = cfg.load_watch_config("legacy")

            self.assertEqual(watch["roots"], [str(root)])
            self.assertEqual(watch["root"], str(root))

    def test_daemon_uses_one_state_for_all_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            roots = [base / "one" / "demo-project", base / "two" / "demo-project-2"]
            for root in roots:
                root.mkdir(parents=True)
            created_roots = []

            class FakeLedger:
                count = 0

                def __init__(self, log_dir):
                    self.log_dir = log_dir

            class FakeBackend:
                kind = "fake"

                def start(self):
                    pass

                def stop(self):
                    pass

            class FakeTimer:
                def __init__(self, states):
                    self.states = states

                def start(self):
                    pass

                def stop(self):
                    pass

            def create_backend(root, excludes, config_root, on_event, process_tracking=True):
                created_roots.append(root)
                return FakeBackend()

            daemon = Daemon.__new__(Daemon)
            daemon.root = base / "config"
            daemon.global_cfg = {"process_tracking": False}
            daemon.watches = [{
                "name": "demo-project",
                "root": str(base),
                "roots": list(map(str, roots)),
                "project": "demo-project",
                "git": True,
                "idle_gap_min": 15,
                "exclude": [],
                "billing": {},
                "log_dir": str(base / "logs"),
            }]
            daemon.states = []
            daemon.backends = []
            daemon.ledgers = []
            daemon.idle_timer = None

            with (
                patch("btl.daemon.Ledger", FakeLedger),
                patch("btl.daemon.create_backend", side_effect=create_backend),
                patch("btl.daemon.IdleTimer", FakeTimer),
                patch("builtins.print"),
            ):
                daemon.start()

            self.assertEqual(created_roots, list(map(str, roots)))
            self.assertEqual(len(daemon.states), 1)
            self.assertEqual(len(daemon.ledgers), 1)
            self.assertEqual(len(daemon.backends), 2)


class ReportTimeOwnershipTests(unittest.TestCase):
    def test_backfills_mixed_history_and_drops_canonical_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            broad_root = base / "workspaces"
            demo_root = broad_root / "demo-project"
            second_root = base / "orca" / "workspaces" / "demo-project-2"
            broad_logs = base / "broad-logs"
            home_logs = base / "home-logs"
            demo_logs = base / "demo-logs"

            def write_log(path, rows):
                path.mkdir(parents=True)
                (path / "2026-01-01.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows),
                    encoding="utf-8",
                )

            write_log(broad_logs, [
                {"ts": "2026-01-01T00:00:00+00:00", "type": "session_start", "session": 1, "project": "workspaces", "watch": "workspaces"},
                {"ts": "2026-01-01T00:01:00+00:00", "type": "file", "event": "MODIFIED", "path": "demo-project/file", "actor": "unknown"},
                {"ts": "2026-01-01T00:02:00+00:00", "type": "git_commit", "repo": str(demo_root), "hash": "abc", "author": "A", "message": "demo commit"},
                {"ts": "2026-01-01T00:02:30+00:00", "type": "file", "event": "MODIFIED", "path": "other/file", "actor": "unknown"},
                {"ts": "2026-01-01T00:03:00+00:00", "type": "session_end", "session": 1, "reason": "idle_gap"},
                {"ts": "2026-01-01T01:00:00+00:00", "type": "session_start", "session": 2, "project": "workspaces", "watch": "workspaces"},
                {"ts": "2026-01-01T01:01:00.050000+00:00", "type": "file", "event": "MODIFIED", "path": "demo-project/file", "actor": "unknown"},
                {"ts": "2026-01-01T01:03:00+00:00", "type": "session_end", "session": 2, "reason": "idle_gap"},
                {"ts": "2026-01-01T02:00:00+00:00", "type": "session_start", "session": 3, "project": "workspaces", "watch": "workspaces"},
                {"ts": "2026-01-01T02:01:00+00:00", "type": "file", "event": "MODIFIED", "path": "demo-project/parent-only", "actor": "unknown"},
                {"ts": "2026-01-01T02:03:00+00:00", "type": "session_end", "session": 3, "reason": "idle_gap"},
            ])
            write_log(home_logs, [
                {"ts": "2026-01-01T00:00:00+00:00", "type": "session_start", "session": 20, "project": "home", "watch": "home"},
                {"ts": "2026-01-01T00:01:00.020000+00:00", "type": "file", "event": "MODIFIED", "path": "workspaces/demo-project/file", "actor": "unknown"},
                {"ts": "2026-01-01T00:03:00+00:00", "type": "session_end", "session": 20, "reason": "idle_gap"},
            ])
            write_log(demo_logs, [
                {"ts": "2026-01-01T00:30:00+00:00", "type": "session_start", "session": 10, "project": "demo-project", "watch": "demo-project"},
                {"ts": "2026-01-01T00:31:00+00:00", "type": "file", "event": "MODIFIED", "path": "workspaces/demo-project/file", "actor": "unknown", "project": "demo-project", "watch": "demo-project"},
                {"ts": "2026-01-01T00:40:00+00:00", "type": "session_end", "session": 10, "reason": "idle_gap"},
                {"ts": "2026-01-01T01:00:00+00:00", "type": "session_start", "session": 11, "project": "demo-project", "watch": "demo-project"},
                {"ts": "2026-01-01T01:01:00+00:00", "type": "file", "event": "MODIFIED", "path": "workspaces/demo-project/file", "actor": "unknown", "project": "demo-project", "watch": "demo-project"},
                {"ts": "2026-01-01T01:03:00+00:00", "type": "session_end", "session": 11, "reason": "idle_gap"},
                {"ts": "2026-01-02T00:00:00+00:00", "type": "session_start", "session": 12, "project": "demo-project", "watch": "demo-project"},
                {"ts": "2026-01-02T00:01:00+00:00", "type": "file", "event": "MODIFIED", "path": "workspaces/demo-project/next-day", "actor": "unknown", "project": "demo-project", "watch": "demo-project"},
                {"ts": "2026-01-02T00:02:00+00:00", "type": "session_end", "session": 12, "reason": "idle_gap"},
            ])
            watches = [
                {"name": "home", "root": str(base), "roots": [str(base)], "project": "home", "log_dir": str(home_logs)},
                {"name": "workspaces", "root": str(broad_root), "roots": [str(broad_root)], "project": "workspaces", "log_dir": str(broad_logs)},
                {"name": "demo-project", "root": str(base), "roots": [str(demo_root), str(second_root)], "project": "demo-project", "log_dir": str(demo_logs)},
            ]

            events, sessions = sessionizer.load_owned_view(
                watches, selected="demo-project",
            )

            self.assertEqual(sum(e.get("type") == "file" for e in events), 5)
            self.assertEqual(sum(e.get("type") == "git_commit" for e in events), 1)
            self.assertEqual(len(sessions), 5)
            self.assertEqual(sessionizer.total_active_min(sessions), 20.0)
            self.assertTrue(all(s["project"] == "demo-project" for s in sessions))

            workspace_events, _ = sessionizer.load_owned_view(
                watches, selected="workspaces",
            )
            self.assertTrue(all(e.get("watch") == "workspaces" for e in workspace_events))
            self.assertIn("other/file", {e.get("path") for e in workspace_events})

            daily = report.daily_report(
                [], "2026-01-01", watches=watches, selected="demo-project",
            )
            self.assertIn("4 sessions", daily)
            exported = json.loads(report.export_json(
                [], "2026-01-01", "2026-01-01", include_raw=True,
                watches=watches, selected="demo-project",
            ))
            self.assertEqual(len(exported["sessions"]), 4)
            self.assertEqual(len(exported["events"]), 13)


if __name__ == "__main__":
    unittest.main()
