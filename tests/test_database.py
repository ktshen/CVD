import tempfile
import unittest
from pathlib import Path

from cvd.database import aggregate_cvd, cleanup_old_trades, connect, initialize, insert_trades


class DatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        initialize(self.db_path)
        self.connection = connect(self.db_path)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_aggregate_cvd_uses_taker_direction(self) -> None:
        insert_trades(
            self.connection,
            [
                ("BTCUSDT", 1, 10_000, 100.0, 2.0, 0),
                ("BTCUSDT", 2, 20_000, 101.0, 0.5, 1),
                ("BTCUSDT", 3, 70_000, 102.0, 1.0, 1),
            ],
        )

        result = aggregate_cvd(self.connection, "btcusdt", 60_000, 0, 120_000)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["trades"], 2)
        self.assertEqual(result[0]["delta"], 1.5)
        self.assertEqual(result[0]["cvd"], 1.5)
        self.assertEqual(result[1]["delta"], -1.0)
        self.assertEqual(result[1]["cvd"], 0.5)

    def test_cleanup_deletes_only_expired_rows(self) -> None:
        day_ms = 24 * 60 * 60 * 1000
        insert_trades(
            self.connection,
            [
                ("BTCUSDT", 1, 4 * day_ms, 100.0, 1.0, 0),
                ("BTCUSDT", 2, 6 * day_ms, 100.0, 1.0, 0),
            ],
        )

        deleted = cleanup_old_trades(self.connection, retention_days=5, now_ms=10 * day_ms)

        self.assertEqual(deleted, 1)
        remaining = self.connection.execute("SELECT COUNT(*) FROM spot_trades").fetchone()[0]
        self.assertEqual(remaining, 1)


if __name__ == "__main__":
    unittest.main()