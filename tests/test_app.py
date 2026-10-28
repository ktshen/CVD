import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import app as app_module
from cvd.database import connect, initialize, insert_trades, recover_trade_gap
from simple_websocket import ConnectionClosed


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
        self.assertIn("-u", scripts)
        self.assertTrue(popen.call_args.args[0][-1].endswith("collector.py"))
        collector.send_signal.assert_called_once()
        collector.wait.assert_called_once_with(timeout=app_module.COLLECTOR_SHUTDOWN_SECONDS + 10)

    @patch("app.process_is_running", return_value=True)
    @patch("app.subprocess.Popen")
    def test_does_not_start_an_existing_collector(self, popen, process_is_running) -> None:
        supervisor = app_module.WorkerSupervisor()

        supervisor.start()
        try:
            popen.assert_not_called()
        finally:
            supervisor.stop()

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
        self.assertTrue(command[-1].endswith("notifier.py"))
        notifier.terminate.assert_called_once()
        notifier.wait.assert_called_once_with(timeout=10)

    @patch("app.CLEANUP_AUTO_START", False)
    @patch("app.NOTIFIER_AUTO_START", False)
    @patch("app.COLLECTOR_AUTO_START", True)
    @patch("app.process_is_running", return_value=False)
    @patch("app.subprocess.Popen")
    def test_restarts_a_crashed_collector(self, popen, process_is_running) -> None:
        crashed = MagicMock()
        crashed.poll.return_value = None
        restarted = MagicMock()
        restarted.poll.return_value = None
        popen.side_effect = [crashed, restarted]
        supervisor = app_module.WorkerSupervisor()
        supervisor.start()
        try:
            crashed.poll.return_value = 1
            crashed.returncode = 1
            supervisor.check_workers()
            self.assertEqual(popen.call_count, 2)
            self.assertIs(supervisor.workers["collector.py"], restarted)
        finally:
            supervisor.stop()


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "test.db"
        initialize(self.db_path)
        self.db_patch = patch.object(app_module, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)

    @patch("app.process_is_running", return_value=True)
    def test_collector_status_exposes_pending_tick_count(self, process_is_running) -> None:
        connection = connect(self.db_path)
        try:
            insert_trades(connection, [("BTCUSDT", 10, 1000, 100.0, 1.0, 0),
                                       ("BTCUSDT", 14, 1004, 104.0, 1.0, 0)], track_gaps=True)
        finally:
            connection.close()
        response = app_module.app.test_client().get("/api/collector-status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["pendingTradeIds"], 3)
        self.assertTrue(response.json["running"])

    def test_market_socket_requests_reload_after_historical_repair(self) -> None:
        connection = connect(self.db_path)
        try:
            insert_trades(connection, [("BTCUSDT", 10, 1000, 100.0, 1.0, 0),
                                       ("BTCUSDT", 12, 1002, 102.0, 1.0, 0)], track_gaps=True)
            recover_trade_gap(connection, ("BTCUSDT", 11, 11), [("BTCUSDT", 11, 1001, 101.0, 1.0, 0)])
        finally:
            connection.close()
        websocket = MagicMock()
        with app_module.app.test_request_context("/ws/market?symbol=BTCUSDT&after=12&revision=0"):
            app_module.stream_market(websocket)
        self.assertEqual(json.loads(websocket.send.call_args.args[0])["type"], "reload")

    def test_idle_socket_detects_browser_disconnect(self) -> None:
        websocket = MagicMock()
        websocket.send.side_effect = ConnectionClosed()
        with app_module.app.test_request_context("/ws/market?symbol=BTCUSDT&after=0&revision=0"):
            app_module.stream_market(websocket)
        self.assertEqual(json.loads(websocket.send.call_args.args[0])["type"], "heartbeat")

    @patch("app.fetch_symbols")
    def test_symbols_returns_only_usdt_pairs(self, fetch_symbols) -> None:
        fetch_symbols.return_value = [
            {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT"},
            {"symbol": "ETHBTC", "baseAsset": "ETH", "quoteAsset": "BTC"},
        ]

        response = app_module.app.test_client().get("/api/symbols")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["symbol"] for item in response.get_json()], ["BTCUSDT"])

    @patch("app.fetch_open_interest_history", return_value=[])
    @patch("app.fetch_klines", side_effect=RuntimeError("database exploded"))
    def test_unexpected_api_error_returns_details(self, fetch_klines, fetch_open_interest_history) -> None:
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