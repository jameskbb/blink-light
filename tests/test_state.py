from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from blink_light.state import is_process_running
from blink_light.paths import build_paths
from blink_light.watcher import watch_status


class StateTests(unittest.TestCase):
    def test_is_process_running_detects_current_pid(self) -> None:
        self.assertTrue(is_process_running(os.getpid()))

    def test_is_process_running_rejects_missing_pid(self) -> None:
        self.assertFalse(is_process_running(None))
        self.assertFalse(is_process_running(-1))

    def test_watch_status_uses_fresh_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            paths = build_paths(
                config_path=root / "blink-light.json",
                project_root=root,
                runtime_dir=root / "runtime",
                startup_dir=root / "startup",
            )
            paths.runtime_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "pid": 99999,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "source": "calendar",
            }
            paths.watcher_state_path.write_text(json.dumps(payload), encoding="utf-8")
            status = watch_status(paths)
            self.assertTrue(status["running"])
            self.assertEqual(status["pid"], 99999)


if __name__ == "__main__":
    unittest.main()
