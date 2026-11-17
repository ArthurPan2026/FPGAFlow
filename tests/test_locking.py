from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from fpga_flow.locking import ProjectRunLock, RunLockError


class ProjectRunLockTests(unittest.TestCase):
    def test_second_process_fails_fast_and_reports_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "out" / "run.lock"
            child_script = (
                "import sys,time; from pathlib import Path; "
                "from fpga_flow.locking import ProjectRunLock; "
                "lock=ProjectRunLock(Path(sys.argv[1]), action='child'); "
                "lock.__enter__(); print('LOCKED', flush=True); time.sleep(4); "
                "lock.__exit__(None,None,None)"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", child_script, str(lock_path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "LOCKED")
                with self.assertRaises(RunLockError) as raised:
                    with ProjectRunLock(lock_path, action="parent"):
                        pass
                message = str(raised.exception)
                self.assertIn("action=child", message)
                self.assertIn("pid=", message)
            finally:
                child.terminate()
                child.wait(timeout=5)

    def test_explicit_wait_acquires_lock_after_owner_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.lock"
            first = ProjectRunLock(path, action="first")
            first.__enter__()
            acquired = threading.Event()

            def wait_for_lock():
                with ProjectRunLock(path, action="second", wait=True):
                    acquired.set()

            thread = threading.Thread(target=wait_for_lock)
            thread.start()
            time.sleep(0.1)
            self.assertFalse(acquired.is_set())
            first.__exit__(None, None, None)
            thread.join(timeout=3)
            self.assertTrue(acquired.is_set())
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
