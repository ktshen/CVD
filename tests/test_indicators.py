import unittest

from cvd.indicators import enrich_indicators, rolling_zscore


class IndicatorTest(unittest.TestCase):
    def test_zero_standard_deviation_returns_zero(self) -> None:
        result = rolling_zscore([1.0] * 100)
        self.assertEqual(result[-1], 0.0)

    def test_absorption_and_oi_context(self) -> None:
        candles = [
            {"time": index * 60, "open": 10.0, "high": 12.0, "low": 8.0, "close": 11.0, "volume": 1.0}
            for index in range(105)
        ]
        deltas = {index * 60: 0.0 for index in range(105)}
        deltas[104 * 60] = -100.0
        oi = {index * 60: 100.0 + index for index in range(105)}
        candles[-1]["close"] = 11.5

        rows = enrich_indicators(candles, deltas, oi)

        self.assertLess(rows[-1]["deltaZ"], -2)
        self.assertTrue(rows[-1]["bullishAbsorption"])
        self.assertEqual(rows[-1]["oiContext"], "price_up_oi_up")
        self.assertTrue(rows[-1]["price_up_oi_up"])
        self.assertGreater(rows[-1]["oiChange"], 0)


if __name__ == "__main__":
    unittest.main()