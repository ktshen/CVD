import tempfile
import unittest
from pathlib import Path

from cvd.database import (
    aggregate_cvd,
    aggregate_cvd_from_minutes,
    cleanup_old_trades,
    connect,
    database_is_ready,
    ensure_database_ready,
    initialize,
    insert_trades,
    trades_after,
)
from cvd.database import recent_trade_buckets, recent_trade_buckets_from_minutes


class DatabaseTest(unittest.TestCase):
    def test_ensure_database_ready_only_initializes_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "market.db"

            self.assertFalse(database_is_ready(db_path))
            self.assertTrue(ensure_database_ready(db_path))
            self.assertTrue(database_is_ready(db_path))
            self.assertFalse(ensure_database_ready(db_path))

    def test_recent_trade_buckets_include_ohlc_delta_and_usd_volume(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = connect(Path(temp_dir) / "market.db")
            initialize(Path(temp_dir) / "market.db")
            insert_trades(
                connection,
                [
                    ("BTCUSDT", 1, 300_001, 100.0, 2.0, 0),
                    ("BTCUSDT", 2, 300_002, 110.0, 1.0, 1),
                    ("BTCUSDT", 3, 600_001, 105.0, 3.0, 0),
                ],
            )

            buckets = recent_trade_buckets(connection, "BTCUSDT", 300_000, 0)

            self.assertEqual(len(buckets), 2)
            self.assertEqual(buckets[0]["open"], 100.0)
            self.assertEqual(buckets[0]["close"], 110.0)
            self.assertEqual(buckets[0]["volume"], 3.0)
            self.assertEqual(buckets[0]["usdVolume"], 310.0)
            self.assertEqual(buckets[0]["delta"], 1.0)
            self.assertEqual(recent_trade_buckets_from_minutes(connection, "BTCUSDT", 300_000, 0), buckets)
            connection.close()

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

    def test_minute_rollup_matches_raw_trade_aggregation(self) -> None:
        insert_trades(
            self.connection,
            [
                ("BTCUSDT", 1, 10_000, 100.0, 2.0, 0),
                ("BTCUSDT", 2, 20_000, 101.0, 0.5, 1),
                ("BTCUSDT", 3, 70_000, 102.0, 1.0, 1),
            ],
        )

        raw = aggregate_cvd(self.connection, "BTCUSDT", 60_000, 0, 120_000)
        rolled_up = aggregate_cvd_from_minutes(self.connection, "BTCUSDT", 60_000, 0, 120_000)

        self.assertEqual(rolled_up, raw)

    def test_insert_count_excludes_rollup_trigger_changes(self) -> None:
        trades = [("BTCUSDT", 1, 10_000, 100.0, 2.0, 0)]

        self.assertEqual(insert_trades(self.connection, trades), 1)
        self.assertEqual(insert_trades(self.connection, trades), 0)

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
        raw = aggregate_cvd(self.connection, "BTCUSDT", 60_000, 0, 10 * day_ms)
        rolled_up = aggregate_cvd_from_minutes(self.connection, "BTCUSDT", 60_000, 0, 10 * day_ms)
        self.assertEqual(rolled_up, raw)

    def test_checkpoint_and_incremental_trades_do_not_overlap(self) -> None:
        insert_trades(
            self.connection,
            [
                ("BTCUSDT", 10, 10_000, 100.0, 2.0, 0),
                ("BTCUSDT", 11, 20_000, 101.0, 0.5, 1),
            ],
        )

        snapshot = aggregate_cvd(self.connection, "BTCUSDT", 60_000, 0, 60_000, max_trade_id=10)
        incremental = trades_after(self.connection, "BTCUSDT", 10)

        self.assertEqual(snapshot[0]["trades"], 1)
        self.assertEqual(snapshot[0]["cvd"], 2.0)
        self.assertEqual([trade["tradeId"] for trade in incremental], [11])

    def test_readable_view_documents_time_and_taker_side(self) -> None:
        insert_trades(self.connection, [("BTCUSDT", 1, 1_000, 10.5, 2.0, 0)])

        row = self.connection.execute("SELECT * FROM spot_trades_readable").fetchone()

        self.assertEqual(row["trade_time_ms"], 1_000)
        self.assertEqual(row["trade_time_utc"], "1970-01-01T00:00:01.000Z")
        self.assertEqual(row["quote_quantity"], 21.0)
        self.assertEqual(row["taker_side"], "buy")


if __name__ == "__main__":
    unittest.main()