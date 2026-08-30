import tempfile
import unittest
from pathlib import Path

from cvd.process_lock import ProcessLock, process_is_running


class ProcessLockTest(unittest.TestCase):
    def test_reports_running_while_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "collector.lock"
            lock = ProcessLock(lock_path)
            self.assertTrue(lock.acquire())
            try:
                self.assertTrue(process_is_running(lock_path))
            finally:
                lock.release()
            self.assertFalse(process_is_running(lock_path))


if __name__ == "__main__":
    unittest.main()