import unittest
from unittest.mock import MagicMock, patch

import app as app_module


class WorkerSupervisorTest(unittest.TestCase):
    @patch("app.subprocess.Popen")
    def test_starts_and_stops_collector_and_cleanup(self, popen) -> None:
        collector = MagicMock()
        cleanup = MagicMock()
        collector.poll.return_value = None
        cleanup.poll.return_value = None
        popen.side_effect = [collector, cleanup]
        supervisor = app_module.WorkerSupervisor()

        supervisor.start()
        supervisor.stop()

        scripts = [call.args[0][1] for call in popen.call_args_list]
        self.assertTrue(scripts[0].endswith("collector.py"))
        self.assertTrue(scripts[1].endswith("cleanup.py"))
        collector.terminate.assert_called_once()
        cleanup.terminate.assert_called_once()
        collector.wait.assert_called_once_with(timeout=10)
        cleanup.wait.assert_called_once_with(timeout=10)


if __name__ == "__main__":
    unittest.main()