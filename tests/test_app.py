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


class ApiTest(unittest.TestCase):
    @patch("app.fetch_symbols")
    def test_symbols_returns_only_usdt_pairs(self, fetch_symbols) -> None:
        fetch_symbols.return_value = [
            {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT"},
            {"symbol": "ETHBTC", "baseAsset": "ETH", "quoteAsset": "BTC"},
        ]

        response = app_module.app.test_client().get("/api/symbols")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["symbol"] for item in response.get_json()], ["BTCUSDT"])

    @patch("app.fetch_klines", side_effect=RuntimeError("database exploded"))
    def test_unexpected_api_error_returns_details(self, fetch_klines) -> None:
        response = app_module.app.test_client().get("/api/chart?symbol=BTCUSDT&interval=1m")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["type"], "RuntimeError")
        self.assertIn("database exploded", response.get_json()["details"])


if __name__ == "__main__":
    unittest.main()