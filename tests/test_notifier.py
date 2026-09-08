import unittest

from notifier import detect_abs_signal, signal_caption


class NotifierTest(unittest.TestCase):
    def test_detects_bullish_abs_with_usd_volume_ma20(self) -> None:
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

        signal = detect_abs_signal("BTCUSDT", buckets)

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.direction, "bullish")
        self.assertEqual(signal.usd_volume, 100.0)
        self.assertEqual(signal.usd_volume_ma20, 90.5)
        self.assertIn("Symbol: BTCUSDT", signal_caption(signal))
        self.assertIn("USD volume MA20: $90.50", signal_caption(signal))

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

        self.assertIsNone(detect_abs_signal("BTCUSDT", buckets))


if __name__ == "__main__":
    unittest.main()