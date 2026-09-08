import unittest
from unittest.mock import MagicMock, patch

import app as app_module


class WorkerSupervisorTest(unittest.TestCase):
    @patch("app.process_is_running", return_value=False)
    @patch("app.subprocess.Popen")
    def test_starts_and_stops_collector(self, popen, process_is_running) -> None:
        collector = MagicMock()
        collector.poll.return_value = None
        popen.return_value = collector
        supervisor = app_module.WorkerSupervisor()

        supervisor.start()
        supervisor.stop()

        scripts = [call.args[0][1] for call in popen.call_args_list]
        self.assertTrue(scripts[0].endswith("collector.py"))
        collector.terminate.assert_called_once()
        collector.wait.assert_called_once_with(timeout=10)

    @patch("app.process_is_running", return_value=True)
    @patch("app.subprocess.Popen")
    def test_does_not_start_an_existing_collector(self, popen, process_is_running) -> None:
        supervisor = app_module.WorkerSupervisor()

        supervisor.start()

        popen.assert_not_called()

    @patch("app.TELEGRAM_CHAT_ID", "123456")
    @patch("app.TELEGRAM_BOT_TOKEN", "test-token")
    @patch("app.NOTIFIER_AUTO_START", True)
    @patch("app.COLLECTOR_AUTO_START", False)
    @patch("app.CLEANUP_AUTO_START", False)
    @patch("app.subprocess.Popen")
    def test_starts_and_stops_notifier_with_server(self, popen) -> None:
        notifier = MagicMock()
        notifier.poll.return_value = None
        popen.return_value = notifier
        supervisor = app_module.WorkerSupervisor()

        supervisor.start()
        supervisor.stop()

        command = popen.call_args.args[0]
        self.assertTrue(command[1].endswith("notifier.py"))
        notifier.terminate.assert_called_once()
        notifier.wait.assert_called_once_with(timeout=10)


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

    @patch("app.fetch_open_interest_history", return_value=[])
    @patch("app.fetch_klines")
    def test_compact_chart_omits_recomputed_indicator_fields(self, fetch_klines, fetch_open_interest_history) -> None:
        fetch_klines.return_value = [
            {"time": index * 300, "open": 10.0, "high": 12.0, "low": 8.0, "close": 11.0, "volume": 1.0}
            for index in range(100)
        ]

        response = app_module.app.test_client().get("/api/chart?symbol=BTCUSDT&interval=5m&compact=1")

        self.assertEqual(response.status_code, 200)
        indicator = response.get_json()["indicators"][-1]
        self.assertEqual(set(indicator), {"time", "openInterest"})


if __name__ == "__main__":
    unittest.main()