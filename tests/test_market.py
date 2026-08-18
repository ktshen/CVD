import asyncio
import contextlib
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from collector import parse_trade, write_batches
from cvd.database import connect, initialize
from cvd.market import fetch_klines


class MarketTest(unittest.TestCase):
    def test_parse_raw_trade_preserves_maker_flag(self) -> None:
        trade = parse_trade(
            {"e": "trade", "s": "BTCUSDT", "t": 42, "T": 1000, "p": "10.5", "q": "2.25", "m": True}
        )
        self.assertEqual(trade, ("BTCUSDT", 42, 1000, 10.5, 2.25, 1))

    @patch("cvd.market.get_json")
    def test_fetch_klines_adds_sma(self, get_json) -> None:
        get_json.return_value = [
            [index * 60_000, str(index), str(index), str(index), str(index), "1"]
            for index in range(1, 61)
        ]
        candles = fetch_klines("BTCUSDT", "1m", 60)
        self.assertNotIn("sma30", candles[28])
        self.assertEqual(candles[29]["sma30"], 15.5)
        self.assertEqual(candles[59]["sma60"], 30.5)


class CollectorWriterTest(unittest.IsolatedAsyncioTestCase):
    async def test_writer_persists_on_its_connection_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "collector.db"
            initialize(db_path)
            queue = asyncio.Queue()
            with patch("collector.DB_PATH", db_path):
                writer = asyncio.create_task(write_batches(queue))
                await queue.put(("BTCUSDT", 1, 1_000, 10.0, 2.0, 0))
                await asyncio.wait_for(queue.join(), timeout=2)
                writer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await writer
            connection = connect(db_path)
            try:
                count = connection.execute("SELECT COUNT(*) FROM spot_trades").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()