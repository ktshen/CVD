from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

DEFAULT_DB_PATH = Path("data/market.db")


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def initialize(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    connection = connect(db_path)
    try:
        connection.executescript(
            """
            DROP TABLE IF EXISTS agg_trades;

            CREATE TABLE IF NOT EXISTS spot_trades (
                symbol TEXT NOT NULL,
                trade_id INTEGER NOT NULL,
                trade_time INTEGER NOT NULL,
                price REAL NOT NULL,
                quantity REAL NOT NULL,
                buyer_is_maker INTEGER NOT NULL CHECK (buyer_is_maker IN (0, 1)),
                PRIMARY KEY (symbol, trade_id)
            ) WITHOUT ROWID;

            CREATE INDEX IF NOT EXISTS idx_spot_trades_time
                ON spot_trades(trade_time);

            CREATE INDEX IF NOT EXISTS idx_spot_trades_symbol_time
                ON spot_trades(symbol, trade_time);
            """
        )
    finally:
        connection.close()


def insert_trades(
    connection: sqlite3.Connection,
    trades: Iterable[tuple[str, int, int, float, float, int]],
) -> int:
    before = connection.total_changes
    connection.executemany(
        """
        INSERT OR IGNORE INTO spot_trades
            (symbol, trade_id, trade_time, price, quantity, buyer_is_maker)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        trades,
    )
    connection.commit()
    return connection.total_changes - before


def aggregate_cvd(
    connection: sqlite3.Connection,
    symbol: str,
    interval_ms: int,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, float | int]]:
    rows = connection.execute(
        """
        SELECT
            CAST(trade_time / ? AS INTEGER) * ? AS bucket_time,
            COUNT(*) AS trade_count,
            SUM(CASE WHEN buyer_is_maker = 0 THEN quantity ELSE 0 END) AS buy_volume,
            SUM(CASE WHEN buyer_is_maker = 1 THEN quantity ELSE 0 END) AS sell_volume
        FROM spot_trades
        WHERE symbol = ? AND trade_time >= ? AND trade_time < ?
        GROUP BY bucket_time
        ORDER BY bucket_time
        """,
        (interval_ms, interval_ms, symbol.upper(), start_ms, end_ms),
    ).fetchall()

    cumulative = 0.0
    result: list[dict[str, float | int]] = []
    for row in rows:
        buy_volume = float(row["buy_volume"])
        sell_volume = float(row["sell_volume"])
        delta = buy_volume - sell_volume
        cumulative += delta
        result.append(
            {
                "time": int(row["bucket_time"] // 1000),
                "trades": int(row["trade_count"]),
                "buyVolume": buy_volume,
                "sellVolume": sell_volume,
                "delta": delta,
                "cvd": cumulative,
            }
        )
    return result


def cleanup_old_trades(
    connection: sqlite3.Connection,
    retention_days: int = 5,
    now_ms: int | None = None,
) -> int:
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    cutoff_ms = current_ms - retention_days * 24 * 60 * 60 * 1000
    before = connection.total_changes
    connection.execute("DELETE FROM spot_trades WHERE trade_time < ?", (cutoff_ms,))
    connection.commit()
    return connection.total_changes - before