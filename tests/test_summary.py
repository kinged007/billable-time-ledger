import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from btl.summary import build_summary, format_summary, read_summary, write_summary


class SummaryTests(unittest.TestCase):
    def test_summary_uses_watch_sessions_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "config"
            log_dir = base / "logs"
            log_dir.mkdir()
            events = [
                {"type": "session_start", "session": 1, "ts": "2026-01-01T10:00:00+00:00"},
                {"type": "file", "path": "main.py", "actor": "human", "ts": "2026-01-01T10:30:00+00:00"},
                {"type": "session_end", "session": 1, "ts": "2026-01-01T12:00:00+00:00"},
            ]
            (log_dir / "2026-01-01.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )
            watch = {
                "name": "demo",
                "project": "demo",
                "log_dir": str(log_dir),
                "billing": {"day_hours": 8},
            }
            now = datetime(2026, 1, 2, tzinfo=timezone.utc)

            data = build_summary([watch], root, now=now)
            self.assertEqual(data["watches"][0]["minutes"], 120.0)
            self.assertEqual(data["watches"][0]["hours"], 2.0)
            self.assertEqual(data["watches"][0]["days"], 0.25)

            path = write_summary([watch], root, now=now)
            self.assertEqual(path.name, "watcher-summary.json")
            loaded = read_summary(root)
            self.assertEqual(loaded["watches"][0]["minutes"], 120.0)
            self.assertEqual(format_summary(loaded), ["  demo               2h 00m (0.25d)"])

    def test_invalid_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "watcher-summary.json").write_text("not json", encoding="utf-8")
            self.assertIsNone(read_summary(root))


if __name__ == "__main__":
    unittest.main()
