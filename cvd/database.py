from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from cvd.process_lock import ProcessLock

DEFAULT_DB_PATH = Path("data/market.db")
REQUIRED_SCHEMA_OBJECTS = {
    ("table", "spot_trades"),
    ("table", "symbol_trade_state"),
    ("table", "abs_notifications"),
    ("table", "schema_migrations"),
    ("table", "spot_trade_minutes"),
    ("table", "trade_gaps"),
    ("table", "symbol_recovery_state"),
    ("trigger", "trg_spot_trades_minute_insert"),
}


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
    except Exception:
        connection.close()
        raise
    return connection


def database_is_ready(db_path: str | Path = DEFAULT_DB_PATH) -> bool:
    path = Path(db_path)
    if not path.exists():
        return False
    connection = sqlite3.connect(path, timeout=5)
    try:
        objects = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger')"
            )
        }
        if not REQUIRED_SCHEMA_OBJECTS.issubset(objects):
            return False
        return connection.execute(
            "SELECT 1 FROM schema_migrations WHERE name = 'spot_trade_minutes_v2'"
        ).fetchone() is not None
    finally:
        connection.close()


def ensure_database_ready(db_path: str | Path = DEFAULT_DB_PATH) -> bool:
    if database_is_ready(db_path):
        return False
    initialize(db_path)
    return True


def minute_rollup_migration_required(db_path: str | Path = DEFAULT_DB_PATH) -> bool:
    path = Path(db_path)
    if not path.exists():
        return False
    connection = sqlite3.connect(path, timeout=5)
    try:
        has_trades = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'spot_trades'"
        ).fetchone()
        if has_trades is None or connection.execute("SELECT 1 FROM spot_trades LIMIT 1").fetchone() is None:
            return False
        has_migrations = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        if has_migrations is None:
            return True
        return connection.execute(
            "SELECT 1 FROM schema_migrations WHERE name = 'spot_trade_minutes_v2'"
        ).fetchone() is None
    finally:
        connection.close()


def initialize(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    path = Path(db_path)
    migration_lock = ProcessLock(path.with_name(f"{path.name}.migration.lock"))
    if not migration_lock.acquire(timeout_seconds=30):
        raise RuntimeError(f"Database initialization is already running for {path}")
    connection: sqlite3.Connection | None = None
    try:
        connection = connect(path)
        connection.executescript(
            """
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

            CREATE TABLE IF NOT EXISTS symbol_trade_state (
                symbol TEXT PRIMARY KEY,
                trade_id INTEGER NOT NULL,
                trade_time INTEGER NOT NULL
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS trade_gaps (
                symbol TEXT NOT NULL,
                first_id INTEGER NOT NULL,
                last_id INTEGER NOT NULL,
                retry_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (symbol, first_id),
                CHECK (last_id >= first_id)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS symbol_recovery_state (
                symbol TEXT PRIMARY KEY,
                revision INTEGER NOT NULL
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS abs_notifications (
                symbol TEXT NOT NULL,
                bucket_time INTEGER NOT NULL,
                direction TEXT NOT NULL,
                sent_at INTEGER NOT NULL,
                PRIMARY KEY (symbol, bucket_time, direction)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at INTEGER NOT NULL
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS spot_trade_minutes (
                symbol TEXT NOT NULL,
                bucket_time INTEGER NOT NULL,
                first_trade_id INTEGER NOT NULL,
                last_trade_id INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                trade_count INTEGER NOT NULL,
                volume REAL NOT NULL,
                usd_volume REAL NOT NULL,
                buy_volume REAL NOT NULL,
                sell_volume REAL NOT NULL,
                PRIMARY KEY (symbol, bucket_time)
            ) WITHOUT ROWID;

            DROP VIEW IF EXISTS spot_trades_readable;

            CREATE VIEW spot_trades_readable AS
            SELECT
                symbol,
                trade_id,
                trade_time AS trade_time_ms,
                strftime('%Y-%m-%dT%H:%M:%fZ', trade_time / 1000.0, 'unixepoch') AS trade_time_utc,
                price,
                quantity,
                price * quantity AS quote_quantity,
                CASE buyer_is_maker WHEN 0 THEN 'buy' ELSE 'sell' END AS taker_side
            FROM spot_trades;
            """
        )
        connection.execute("BEGIN IMMEDIATE")
        try:
            migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name = 'spot_trade_minutes_v2'"
            ).fetchone()
            if migration is None:
                raw_rows = int(connection.execute("SELECT COUNT(*) FROM spot_trades").fetchone()[0])
                migration_started = time.monotonic()
                logging.warning(
                    "One-time database migration: rebuilding 1-minute rollup from %d raw trades; "
                    "do not start another collector until this completes",
                    raw_rows,
                )
                connection.execute("DROP TRIGGER IF EXISTS trg_spot_trades_minute_insert")
                connection.execute("DROP TABLE spot_trade_minutes")
                connection.execute(
                    """
                    CREATE TABLE spot_trade_minutes (
                        symbol TEXT NOT NULL,
                        bucket_time INTEGER NOT NULL,
                        first_trade_id INTEGER NOT NULL,
                        last_trade_id INTEGER NOT NULL,
                        open REAL NOT NULL,
                        high REAL NOT NULL,
                        low REAL NOT NULL,
                        close REAL NOT NULL,
                        trade_count INTEGER NOT NULL,
                        volume REAL NOT NULL,
                        usd_volume REAL NOT NULL,
                        buy_volume REAL NOT NULL,
                        sell_volume REAL NOT NULL,
                        PRIMARY KEY (symbol, bucket_time)
                    ) WITHOUT ROWID
                    """
                )
                connection.execute(
                    """
                    WITH grouped AS (
                        SELECT
                            symbol,
                            CAST(trade_time / 60000 AS INTEGER) * 60000 AS bucket_time,
                            MIN(trade_id) AS first_trade_id,
                            MAX(trade_id) AS last_trade_id,
                            MAX(price) AS high,
                            MIN(price) AS low,
                            COUNT(*) AS trade_count,
                            SUM(quantity) AS volume,
                            SUM(price * quantity) AS usd_volume,
                            SUM(CASE WHEN buyer_is_maker = 0 THEN quantity ELSE 0 END) AS buy_volume,
                            SUM(CASE WHEN buyer_is_maker = 1 THEN quantity ELSE 0 END) AS sell_volume
                        FROM spot_trades
                        GROUP BY symbol, CAST(trade_time / 60000 AS INTEGER)
                    )
                    INSERT INTO spot_trade_minutes
                        (symbol, bucket_time, first_trade_id, last_trade_id, open, high, low, close,
                         trade_count, volume, usd_volume, buy_volume, sell_volume)
                    SELECT
                        grouped.symbol,
                        grouped.bucket_time,
                        grouped.first_trade_id,
                        grouped.last_trade_id,
                        first_trade.price,
                        grouped.high,
                        grouped.low,
                        last_trade.price,
                        grouped.trade_count,
                        grouped.volume,
                        grouped.usd_volume,
                        grouped.buy_volume,
                        grouped.sell_volume
                    FROM grouped
                    JOIN spot_trades AS first_trade
                        ON first_trade.symbol = grouped.symbol AND first_trade.trade_id = grouped.first_trade_id
                    JOIN spot_trades AS last_trade
                        ON last_trade.symbol = grouped.symbol AND last_trade.trade_id = grouped.last_trade_id
                    """
                )
                connection.execute(
                    "INSERT INTO schema_migrations (name, applied_at) VALUES ('spot_trade_minutes_v2', ?)",
                    (int(time.time() * 1000),),
                )
                minute_rows = int(connection.execute("SELECT COUNT(*) FROM spot_trade_minutes").fetchone()[0])
                logging.warning(
                    "One-time database migration complete: %d minute rows in %.2f seconds",
                    minute_rows,
                    time.monotonic() - migration_started,
                )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_spot_trades_minute_insert
                AFTER INSERT ON spot_trades
                BEGIN
                    INSERT INTO spot_trade_minutes
                        (symbol, bucket_time, first_trade_id, last_trade_id, open, high, low, close,
                         trade_count, volume, usd_volume, buy_volume, sell_volume)
                    VALUES (
                        NEW.symbol,
                        CAST(NEW.trade_time / 60000 AS INTEGER) * 60000,
                        NEW.trade_id,
                        NEW.trade_id,
                        NEW.price,
                        NEW.price,
                        NEW.price,
                        NEW.price,
                        1,
                        NEW.quantity,
                        NEW.price * NEW.quantity,
                        CASE WHEN NEW.buyer_is_maker = 0 THEN NEW.quantity ELSE 0 END,
                        CASE WHEN NEW.buyer_is_maker = 1 THEN NEW.quantity ELSE 0 END
                    )
                    ON CONFLICT(symbol, bucket_time) DO UPDATE SET
                        first_trade_id = MIN(first_trade_id, excluded.first_trade_id),
                        last_trade_id = MAX(last_trade_id, excluded.last_trade_id),
                        open = CASE WHEN excluded.first_trade_id < first_trade_id THEN excluded.open ELSE open END,
                        high = MAX(high, excluded.high),
                        low = MIN(low, excluded.low),
                        close = CASE WHEN excluded.last_trade_id > last_trade_id THEN excluded.close ELSE close END,
                        trade_count = trade_count + 1,
                        volume = volume + excluded.volume,
                        usd_volume = usd_volume + excluded.usd_volume,
                        buy_volume = buy_volume + excluded.buy_volume,
                        sell_volume = sell_volume + excluded.sell_volume;
                END
                """
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    finally:
        if connection is not None:
            connection.close()
        migration_lock.release()


def insert_trades(
    connection: sqlite3.Connection,
    trades: Iterable[tuple[str, int, int, float, float, int]],
    track_gaps: bool = False,
) -> int:
    rows = list(trades)
    with connection:
        if track_gaps:
            connection.execute("BEGIN IMMEDIATE")
            markers: dict[str, int] = {}
            for symbol, trade_id, *_ in sorted(rows, key=lambda row: (row[0], row[1])):
                if symbol not in markers:
                    state = connection.execute(
                        "SELECT trade_id FROM symbol_trade_state WHERE symbol = ?", (symbol,)
                    ).fetchone()
                    markers[symbol] = int(state[0]) if state is not None else trade_id
                previous = markers[symbol]
                if trade_id > previous + 1:
                    connection.execute(
                        "INSERT OR IGNORE INTO trade_gaps (symbol, first_id, last_id) VALUES (?, ?, ?)",
                        (symbol, previous + 1, trade_id - 1),
                    )
                    logging.warning("Raw trade gap detected: %s IDs %d..%d", symbol, previous + 1, trade_id - 1)
                markers[symbol] = max(previous, trade_id)
        return _insert_trades(connection, rows)


def _insert_trades(
    connection: sqlite3.Connection,
    trades: Iterable[tuple[str, int, int, float, float, int]],
) -> int:
    rows = list(trades)
    cursor = connection.executemany(
        """
        INSERT INTO spot_trades
            (symbol, trade_id, trade_time, price, quantity, buyer_is_maker)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, trade_id) DO NOTHING
        """,
        rows,
    )
    inserted = cursor.rowcount
    latest_by_symbol: dict[str, tuple[int, int]] = {}
    for symbol, trade_id, trade_time, *_ in rows:
        current = latest_by_symbol.get(symbol)
        if current is None or trade_id > current[0]:
            latest_by_symbol[symbol] = (trade_id, trade_time)
    connection.executemany(
        """
        INSERT INTO symbol_trade_state (symbol, trade_id, trade_time)
        VALUES (?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            trade_id = excluded.trade_id,
            trade_time = excluded.trade_time
        WHERE excluded.trade_id > symbol_trade_state.trade_id
        """,
        [(symbol, trade_id, trade_time) for symbol, (trade_id, trade_time) in latest_by_symbol.items()],
    )
    return inserted


def next_trade_gap(connection: sqlite3.Connection) -> tuple[str, int, int] | None:
    row = connection.execute(
        "SELECT symbol, first_id, last_id FROM trade_gaps WHERE retry_at <= ? "
        "ORDER BY retry_at, symbol, first_id LIMIT 1",
        (time.time(),),
    ).fetchone()
    return (str(row[0]), int(row[1]), int(row[2])) if row is not None else None


def advance_trade_gap(
    connection: sqlite3.Connection, symbol: str, first_id: int, last_id: int, recovered_id: int,
) -> None:
    with connection:
        _advance_trade_gap(connection, symbol, first_id, last_id, recovered_id)


def _advance_trade_gap(
    connection: sqlite3.Connection, symbol: str, first_id: int, last_id: int, recovered_id: int,
) -> None:
    if recovered_id < first_id or recovered_id > last_id:
        raise ValueError("Recovered trade ID outside gap")
    count = connection.execute(
        "SELECT COUNT(*) FROM spot_trades WHERE symbol = ? AND trade_id BETWEEN ? AND ?",
        (symbol, first_id, recovered_id),
    ).fetchone()[0]
    if count != recovered_id - first_id + 1:
        raise ValueError("Cannot advance gap before all raw trades are committed")
    connection.execute("DELETE FROM trade_gaps WHERE symbol = ? AND first_id = ?", (symbol, first_id))
    if recovered_id < last_id:
        connection.execute(
            "INSERT INTO trade_gaps (symbol, first_id, last_id, retry_at) VALUES (?, ?, ?, ?)",
            (symbol, recovered_id + 1, last_id, time.time()),
        )


def recover_trade_gap(
    connection: sqlite3.Connection, gap: tuple[str, int, int],
    trades: Iterable[tuple[str, int, int, float, float, int]],
) -> int:
    rows = list(trades)
    symbol, first_id, last_id = gap
    if not rows or any(row[0] != symbol or row[1] != first_id + index or row[1] > last_id
                       for index, row in enumerate(rows)):
        raise ValueError("Recovery batch must contain consecutive raw trade IDs within the gap")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT last_id FROM trade_gaps WHERE symbol = ? AND first_id = ?", (symbol, first_id)
        ).fetchone()
        if current is None or int(current[0]) != last_id:
            raise ValueError("Recovery gap no longer exists")
        inserted = _insert_trades(connection, rows)
        _advance_trade_gap(connection, *gap, rows[-1][1])
        connection.execute(
            "INSERT INTO symbol_recovery_state (symbol, revision) VALUES (?, 1) "
            "ON CONFLICT(symbol) DO UPDATE SET revision = revision + 1", (symbol,),
        )
    return inserted


def recovery_revision(connection: sqlite3.Connection, symbol: str) -> int:
    row = connection.execute("SELECT revision FROM symbol_recovery_state WHERE symbol = ?", (symbol,)).fetchone()
    return int(row[0]) if row is not None else 0


def latest_symbol_markers(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        str(row["symbol"]): int(row["trade_id"])
        for row in connection.execute("SELECT symbol, trade_id FROM symbol_trade_state")
    }


def notification_was_sent(
    connection: sqlite3.Connection,
    symbol: str,
    bucket_time: int,
    direction: str,
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM abs_notifications WHERE symbol = ? AND bucket_time = ? AND direction = ?",
        (symbol.upper(), bucket_time, direction),
    ).fetchone()
    return row is not None


def record_notification(
    connection: sqlite3.Connection,
    symbol: str,
    bucket_time: int,
    direction: str,
    sent_at: int | None = None,
) -> None:
    with connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO abs_notifications (symbol, bucket_time, direction, sent_at)
            VALUES (?, ?, ?, ?)
            """,
            (symbol.upper(), bucket_time, direction, sent_at if sent_at is not None else int(time.time() * 1000)),
        )


def aggregate_cvd(
    connection: sqlite3.Connection,
    symbol: str,
    interval_ms: int,
    start_ms: int,
    end_ms: int,
    max_trade_id: int | None = None,
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
            AND (? IS NULL OR trade_id <= ?)
        GROUP BY bucket_time
        ORDER BY bucket_time
        """,
        (interval_ms, interval_ms, symbol.upper(), start_ms, end_ms, max_trade_id, max_trade_id),
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


def aggregate_cvd_from_minutes(
    connection: sqlite3.Connection,
    symbol: str,
    interval_ms: int,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, float | int]]:
    rows = connection.execute(
        """
        SELECT
            CAST(bucket_time / ? AS INTEGER) * ? AS bucket_time,
            SUM(trade_count) AS trade_count,
            SUM(buy_volume) AS buy_volume,
            SUM(sell_volume) AS sell_volume
        FROM spot_trade_minutes
        WHERE symbol = ? AND bucket_time >= ? AND bucket_time < ?
        GROUP BY CAST(bucket_time / ? AS INTEGER)
        ORDER BY bucket_time
        """,
        (interval_ms, interval_ms, symbol.upper(), start_ms, end_ms, interval_ms),
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


def recent_trade_buckets(
    connection: sqlite3.Connection,
    symbol: str,
    interval_ms: int,
    start_ms: int,
    limit: int = 100,
) -> list[dict[str, float | int]]:
    rows = connection.execute(
        """
        WITH ranked AS (
            SELECT
                CAST(trade_time / ? AS INTEGER) * ? AS bucket_time,
                price,
                quantity,
                buyer_is_maker,
                ROW_NUMBER() OVER (
                    PARTITION BY CAST(trade_time / ? AS INTEGER)
                    ORDER BY trade_time, trade_id
                ) AS first_trade,
                ROW_NUMBER() OVER (
                    PARTITION BY CAST(trade_time / ? AS INTEGER)
                    ORDER BY trade_time DESC, trade_id DESC
                ) AS last_trade
            FROM spot_trades
            WHERE symbol = ? AND trade_time >= ?
        ), buckets AS (
            SELECT
                bucket_time,
                MAX(CASE WHEN first_trade = 1 THEN price END) AS open,
                MAX(price) AS high,
                MIN(price) AS low,
                MAX(CASE WHEN last_trade = 1 THEN price END) AS close,
                SUM(quantity) AS volume,
                SUM(price * quantity) AS usd_volume,
                SUM(CASE WHEN buyer_is_maker = 0 THEN quantity ELSE -quantity END) AS delta
            FROM ranked
            GROUP BY bucket_time
            ORDER BY bucket_time DESC
            LIMIT ?
        )
        SELECT * FROM buckets ORDER BY bucket_time
        """,
        (interval_ms, interval_ms, interval_ms, interval_ms, symbol.upper(), start_ms, limit),
    ).fetchall()
    return [
        {
            "time": int(row["bucket_time"] // 1000),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "usdVolume": float(row["usd_volume"]),
            "delta": float(row["delta"]),
        }
        for row in rows
    ]


def recent_trade_buckets_from_minutes(
    connection: sqlite3.Connection,
    symbol: str,
    interval_ms: int,
    start_ms: int,
    limit: int = 100,
) -> list[dict[str, float | int]]:
    rows = connection.execute(
        """
        WITH ranked AS (
            SELECT
                CAST(bucket_time / ? AS INTEGER) * ? AS interval_time,
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY CAST(bucket_time / ? AS INTEGER)
                    ORDER BY bucket_time
                ) AS first_minute,
                ROW_NUMBER() OVER (
                    PARTITION BY CAST(bucket_time / ? AS INTEGER)
                    ORDER BY bucket_time DESC
                ) AS last_minute
            FROM spot_trade_minutes
            WHERE symbol = ? AND bucket_time >= ?
        ), buckets AS (
            SELECT
                interval_time,
                MAX(CASE WHEN first_minute = 1 THEN open END) AS open,
                MAX(high) AS high,
                MIN(low) AS low,
                MAX(CASE WHEN last_minute = 1 THEN close END) AS close,
                SUM(volume) AS volume,
                SUM(usd_volume) AS usd_volume,
                SUM(buy_volume) - SUM(sell_volume) AS delta
            FROM ranked
            GROUP BY interval_time
            ORDER BY interval_time DESC
            LIMIT ?
        )
        SELECT * FROM buckets ORDER BY interval_time
        """,
        (interval_ms, interval_ms, interval_ms, interval_ms, symbol.upper(), start_ms, limit),
    ).fetchall()
    return [
        {
            "time": int(row["interval_time"] // 1000),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "usdVolume": float(row["usd_volume"]),
            "delta": float(row["delta"]),
        }
        for row in rows
    ]


def latest_trade_id(connection: sqlite3.Connection, symbol: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(trade_id), 0) FROM spot_trades WHERE symbol = ?",
        (symbol.upper(),),
    ).fetchone()
    return int(row[0])


def trades_after(
    connection: sqlite3.Connection,
    symbol: str,
    trade_id: int,
    limit: int = 5_000,
) -> list[dict[str, float | int]]:
    rows = connection.execute(
        """
        SELECT trade_id, trade_time, price, quantity, buyer_is_maker
        FROM spot_trades
        WHERE symbol = ? AND trade_id > ?
        ORDER BY trade_id
        LIMIT ?
        """,
        (symbol.upper(), trade_id, limit),
    ).fetchall()
    return [
        {
            "tradeId": int(row["trade_id"]),
            "time": int(row["trade_time"]),
            "price": float(row["price"]),
            "quantity": float(row["quantity"]),
            "buyerIsMaker": bool(row["buyer_is_maker"]),
        }
        for row in rows
    ]


def cleanup_old_trades(
    connection: sqlite3.Connection,
    retention_days: int = 90,
    now_ms: int | None = None,
) -> int:
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    cutoff_ms = current_ms - retention_days * 24 * 60 * 60 * 1000
    boundary_ms = cutoff_ms // 60_000 * 60_000
    before = connection.total_changes
    connection.execute("DELETE FROM spot_trades WHERE trade_time < ?", (cutoff_ms,))
    deleted = connection.total_changes - before
    connection.execute("DELETE FROM spot_trade_minutes WHERE bucket_time <= ?", (boundary_ms,))
    connection.execute(
        """
        INSERT INTO spot_trade_minutes
            (symbol, bucket_time, first_trade_id, last_trade_id, open, high, low, close,
             trade_count, volume, usd_volume, buy_volume, sell_volume)
        SELECT
            symbol,
            ?,
            MIN(trade_id),
            MAX(trade_id),
            (SELECT price FROM spot_trades AS first_trade
             WHERE first_trade.symbol = spot_trades.symbol AND first_trade.trade_time >= ?
                 AND first_trade.trade_time < ? ORDER BY trade_time, trade_id LIMIT 1),
            MAX(price),
            MIN(price),
            (SELECT price FROM spot_trades AS last_trade
             WHERE last_trade.symbol = spot_trades.symbol AND last_trade.trade_time >= ?
                 AND last_trade.trade_time < ? ORDER BY trade_time DESC, trade_id DESC LIMIT 1),
            COUNT(*),
            SUM(quantity),
            SUM(price * quantity),
            SUM(CASE WHEN buyer_is_maker = 0 THEN quantity ELSE 0 END),
            SUM(CASE WHEN buyer_is_maker = 1 THEN quantity ELSE 0 END)
        FROM spot_trades
        WHERE trade_time >= ? AND trade_time < ?
        GROUP BY symbol
        """,
        (
            boundary_ms,
            boundary_ms,
            boundary_ms + 60_000,
            boundary_ms,
            boundary_ms + 60_000,
            boundary_ms,
            boundary_ms + 60_000,
        ),
    )
    connection.commit()
    return deleted