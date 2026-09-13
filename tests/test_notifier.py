import sqlite3
import unittest
from unittest.mock import patch

from cvd.market import INTERVALS_MS
from notifier import detect_abs_signal, evaluate_symbol, send_test_message, signal_caption


class NotifierTest(unittest.TestCase):
    def test_evaluate_symbol_treats_missing_trade_buckets_as_zero_delta(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE spot_trade_minutes (symbol TEXT, bucket_time INTEGER)")
        interval_ms = INTERVALS_MS["5m"]
        with patch("notifier.time.time", return_value=100 * interval_ms / 1000):
            start_ms = interval_ms
            connection.executemany(
                "INSERT INTO spot_trade_minutes VALUES (?, ?)",
                [("BTCUSDT", start_ms), ("BTCUSDT", 100 * interval_ms)],
            )
            latest = {
                "time": 100 * interval_ms // 1000,
                "open": 10.0,
                "high": 12.0,
                "low": 8.0,
                "close": 11.5,
                "volume": 1.0,
                "usdVolume": 60_000.0,
                "delta": -100.0,
            }
            with patch("notifier.recent_trade_buckets_from_minutes", return_value=[latest]):
                signal = evaluate_symbol(connection, "BTCUSDT", "5m")

        self.assertIsNotNone(signal)
        connection.close()

    @patch("notifier.telegram_post_json")
    @patch("notifier.TELEGRAM_CHAT_ID", "123456")
    @patch("notifier.TELEGRAM_BOT_TOKEN", "test-token")
    def test_sends_telegram_test_message_after_destination_validation(self, telegram_post_json) -> None:
        send_test_message()

        self.assertEqual(telegram_post_json.call_args_list[0].args[0], "getChat")
        self.assertEqual(telegram_post_json.call_args_list[1].args[0], "sendMessage")
        self.assertEqual(telegram_post_json.call_args_list[1].args[2]["chat_id"], "123456")

    def make_buckets(self, interval: str, latest_usd_volume: float = 60_000.0) -> list[dict[str, float | int]]:
        step = INTERVALS_MS[interval] // 1000
        buckets = [
            {
                "time": index * step,
                "open": 10.0,
                "high": 12.0,
                "low": 8.0,
                "close": 10.0,
                "volume": 1.0,
                "usdVolume": 10_000.0,
                "delta": 0.0,
            }
            for index in range(100)
        ]
        buckets[-1]["close"] = 11.5
        buckets[-1]["usdVolume"] = latest_usd_volume
        buckets[-1]["delta"] = -100.0
        return buckets

    def test_detects_bullish_5m_abs_above_usd_volume_threshold(self) -> None:
        buckets = self.make_buckets("5m", latest_usd_volume=50_000.01)

        signal = detect_abs_signal("BTCUSDT", "5m", buckets, 50_000.0)

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.interval, "5m")
        self.assertEqual(signal.direction, "bullish")
        self.assertEqual(signal.notification_key, "5m:bullish")
        self.assertEqual(signal.usd_volume, 50_000.01)
        self.assertIn("5m BULLISH ABS", signal_caption(signal))
        self.assertIn("USD volume threshold: $50,000.00", signal_caption(signal))

    def test_detects_bullish_1m_abs_above_usd_volume_threshold(self) -> None:
        buckets = self.make_buckets("1m", latest_usd_volume=20_000.01)

        signal = detect_abs_signal("ETHUSDT", "1m", buckets, 20_000.0)

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.interval, "1m")
        self.assertEqual(signal.notification_key, "1m:bullish")

    def test_ignores_bullish_abs_below_usd_volume_threshold(self) -> None:
        buckets = self.make_buckets("5m", latest_usd_volume=50_000.0)

        self.assertIsNone(detect_abs_signal("BTCUSDT", "5m", buckets, 50_000.0))

    def test_ignores_bearish_abs(self) -> None:
        buckets = self.make_buckets("5m", latest_usd_volume=80_000.0)
        buckets[-1]["close"] = 8.5
        buckets[-1]["delta"] = 100.0

        self.assertIsNone(detect_abs_signal("BTCUSDT", "5m", buckets, 50_000.0))

    def test_caption_includes_usd_volume_ma20(self) -> None:
        buckets = [
            {
                "time": index * 300,
                "open": 10.0,
                "high": 12.0,
                "low": 8.0,
                "close": 10.0,
                "volume": 1.0,
                "usdVolume": float(index + 1),
                "delta": 0.0,
            }
            for index in range(100)
        ]
        buckets[-1]["close"] = 11.5
        buckets[-1]["delta"] = -100.0
        buckets[-1]["usdVolume"] = 60_000.0

        signal = detect_abs_signal("BTCUSDT", "5m", buckets, 50_000.0)

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.usd_volume_ma20, 3_085.5)
        self.assertIn("Symbol: BTCUSDT", signal_caption(signal))
        self.assertIn("USD volume MA20: $3,085.50", signal_caption(signal))

    def test_requires_contiguous_100_bucket_history(self) -> None:
        buckets = [
            {
                "time": index * 300,
                "open": 10.0,
                "high": 12.0,
                "low": 8.0,
                "close": 11.5,
                "volume": 1.0,
                "usdVolume": 10.0,
                "delta": -100.0 if index == 98 else 0.0,
            }
            for index in range(99)
        ]

        self.assertIsNone(detect_abs_signal("BTCUSDT", "5m", buckets, 50_000.0))


if __name__ == "__main__":
    unittest.main()