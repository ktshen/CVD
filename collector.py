from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import signal
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

import aiohttp

from cvd.config import (
    BINANCE_WS_URL,
    BINANCE_REST_URL,
    COLLECTOR_SHUTDOWN_SECONDS,
    COLLECTOR_LOCK_PATH,
    DB_PATH,
    QUEUE_MAX_SIZE,
    QUOTE_ASSETS,
    RECONNECT_DELAY_SECONDS,
    REPORT_INTERVAL_SECONDS,
    STREAMS_PER_CONNECTION,
    SYMBOLS,
    WRITE_BATCH_SIZE,
    WS_HEARTBEAT_SECONDS,
)
from cvd.database import connect, ensure_database_ready, insert_trades, next_trade_gap, recover_trade_gap
from cvd.market import fetch_symbols
from cvd.process_lock import ProcessLock

Trade = tuple[str, int, int, float, float, int]


@dataclass
class InsertionStats:
    rows: int = 0

    def take(self) -> int:
        rows = self.rows
        self.rows = 0
        return rows


def selected_symbols() -> list[str]:
    symbols = fetch_symbols()
    return [
        item["symbol"]
        for item in symbols
        if (not SYMBOLS or item["symbol"] in SYMBOLS)
        and (not QUOTE_ASSETS or item["quoteAsset"] in QUOTE_ASSETS)
    ]


def parse_trade(payload: object) -> Trade | None:
    if not isinstance(payload, dict) or payload.get("e") != "trade":
        return None
    try:
        symbol = payload["s"]
        trade_id = int(payload["t"])
        trade_time = int(payload["T"])
        price = float(payload["p"])
        quantity = float(payload["q"])
        maker = payload["m"]
        if (
            not isinstance(symbol, str) or not symbol or not symbol.isalnum()
            or trade_id < 0 or trade_id > 2**63 - 1
            or trade_time < 0 or trade_time > 2**63 - 1
            or not math.isfinite(price) or price <= 0
            or not math.isfinite(quantity) or quantity <= 0
            or not math.isfinite(price * quantity)
            or not isinstance(maker, bool)
        ):
            raise ValueError("Invalid trade values")
        return (symbol, trade_id, trade_time, price, quantity, int(maker))
    except (KeyError, TypeError, ValueError, OverflowError):
        logging.warning("Ignoring malformed trade payload: %r", payload)
        return None


def persist_batch(batch: Sequence[Trade]) -> int:
    connection = connect(DB_PATH)
    try:
        return insert_trades(connection, batch, track_gaps=True)
    finally:
        connection.close()


async def database_operation(function, *args):
    operation = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await operation
        raise


async def write_batches(queue: asyncio.Queue[Trade], stats: InsertionStats | None = None) -> None:
    while True:
        first = await queue.get()
        batch = [first]
        deadline = time.monotonic() + 0.25
        while len(batch) < WRITE_BATCH_SIZE and time.monotonic() < deadline:
            try:
                batch.append(await asyncio.wait_for(queue.get(), timeout=min(0.05, deadline - time.monotonic())))
            except TimeoutError:
                break
        while True:
            try:
                inserted = await database_operation(persist_batch, batch)
                break
            except (sqlite3.Error, OSError):
                logging.exception("Database write failed; retaining %d ticks for retry", len(batch))
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
        if stats is not None:
            stats.rows += inserted
        for _ in batch:
            queue.task_done()
        if inserted:
            logging.debug("Inserted %d raw trades", inserted)


async def report_ingestion(stats: InsertionStats, queue: asyncio.Queue[Trade]) -> None:
    while True:
        await asyncio.sleep(REPORT_INTERVAL_SECONDS)
        logging.info(
            "Collector minute summary: %d new rows inserted; queue depth %d",
            stats.take(),
            queue.qsize(),
        )
        if queue.qsize() >= QUEUE_MAX_SIZE * 0.8:
            logging.warning("Collector queue is nearly full; database storage cannot keep up")


def load_gap() -> tuple[str, int, int] | None:
    connection = connect(DB_PATH)
    try:
        return next_trade_gap(connection)
    finally:
        connection.close()


def defer_gap(gap: tuple[str, int, int], delay: float) -> None:
    connection = connect(DB_PATH)
    try:
        with connection:
            connection.execute(
                "UPDATE trade_gaps SET retry_at = ? WHERE symbol = ? AND first_id = ?",
                (time.time() + delay, gap[0], gap[1]),
            )
    finally:
        connection.close()


def persist_recovered(gap: tuple[str, int, int], trades: Sequence[Trade]) -> int:
    connection = connect(DB_PATH)
    try:
        return recover_trade_gap(connection, gap, trades)
    finally:
        connection.close()


def parse_history(payload: object, gap: tuple[str, int, int]) -> list[Trade]:
    if not isinstance(payload, list) or not payload:
        raise ValueError("Historical raw trades are not available yet")
    symbol, first_id, last_id = gap
    trades: list[Trade] = []
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError("Malformed historical trade")
        trade = parse_trade({"e": "trade", "s": symbol, "t": row.get("id"), "T": row.get("time"),
                             "p": row.get("price"), "q": row.get("qty"), "m": row.get("isBuyerMaker")})
        if trade is None or trade[1] != first_id + len(trades) or trade[1] > last_id:
            raise ValueError("Historical response does not cover the requested consecutive raw trade IDs")
        trades.append(trade)
    return trades


async def recover_gaps(session: aiohttp.ClientSession, stats: InsertionStats) -> None:
    while True:
        gap = None
        delay = 1.0
        try:
            gap = await database_operation(load_gap)
            if gap is not None:
                symbol, first_id, last_id = gap
                async with session.get(
                    f"{BINANCE_REST_URL}/api/v3/historicalTrades",
                    params={"symbol": symbol, "fromId": first_id, "limit": min(1000, last_id - first_id + 1)},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status in {418, 429}:
                        try:
                            delay = max(60.0, float(response.headers.get("Retry-After", "60")))
                            if not math.isfinite(delay):
                                delay = 60.0
                        except ValueError:
                            delay = 60.0
                    response.raise_for_status()
                    trades = parse_history(await response.json(), gap)
                inserted = await database_operation(persist_recovered, gap, trades)
                stats.rows += inserted
                logging.info("Recovered raw ticks: %s IDs %d..%d", symbol, first_id, trades[-1][1])
        except Exception:
            logging.exception("Raw trade recovery failed; gap remains pending: %s", gap)
            delay = max(delay, float(RECONNECT_DELAY_SECONDS))
            if gap is not None:
                try:
                    await database_operation(defer_gap, gap, max(delay, 60))
                except (sqlite3.Error, OSError):
                    logging.exception("Could not postpone gap retry")
        await asyncio.sleep(delay)


async def consume_streams(
    session: aiohttp.ClientSession,
    symbols: Sequence[str],
    queue: asyncio.Queue[Trade],
    connection_number: int,
) -> None:
    streams = [f"{symbol.lower()}@trade" for symbol in symbols]
    while True:
        try:
            logging.info("Connecting stream group %d (%d symbols)", connection_number, len(symbols))
            async with session.ws_connect(BINANCE_WS_URL, heartbeat=WS_HEARTBEAT_SECONDS) as websocket:
                await websocket.send_json({"method": "SUBSCRIBE", "params": streams, "id": connection_number})
                subscribed = False
                subscription_deadline = time.monotonic() + 15
                while True:
                    if not subscribed and time.monotonic() >= subscription_deadline:
                        raise TimeoutError("Subscription acknowledgement timed out")
                    timeout = 90 if subscribed else max(0.001, subscription_deadline - time.monotonic())
                    message = await asyncio.wait_for(websocket.receive(), timeout=timeout)
                    if message.type == aiohttp.WSMsgType.TEXT:
                        try:
                            payload = json.loads(message.data)
                        except (json.JSONDecodeError, TypeError):
                            logging.warning("Ignoring invalid JSON in stream group %d", connection_number)
                            continue
                        if isinstance(payload, dict) and ("code" in payload or "error" in payload):
                            raise RuntimeError(f"Subscription rejected: {payload}")
                        if isinstance(payload, dict) and payload.get("id") == connection_number:
                            if payload.get("result", "missing") is not None:
                                raise RuntimeError(f"Invalid subscription response: {payload}")
                            subscribed = True
                            logging.info("Stream group %d subscribed successfully", connection_number)
                        trade = parse_trade(payload)
                        if trade is not None:
                            if trade[0] in symbols:
                                await queue.put(trade)
                    elif message.type in {aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING}:
                        logging.warning("Stream group %d closed: %s", connection_number, websocket.exception())
                        break
        except Exception:
            logging.exception("Stream group %d disconnected; reconnecting", connection_number)
        await asyncio.sleep(RECONNECT_DELAY_SECONDS)


async def main() -> None:
    if await asyncio.to_thread(ensure_database_ready, DB_PATH):
        logging.info("Database schema created or upgraded")
    else:
        logging.info("Database schema already current")
    symbols = await asyncio.to_thread(selected_symbols)
    if not symbols:
        raise RuntimeError("No matching Binance Spot symbols were found")
    logging.info("Collecting raw trade ticks for %d symbols", len(symbols))
    queue: asyncio.Queue[Trade] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
    stats = InsertionStats()
    writer = asyncio.create_task(write_batches(queue, stats))
    reporter = asyncio.create_task(report_ingestion(stats, queue))
    async with aiohttp.ClientSession() as session:
        recovery = asyncio.create_task(recover_gaps(session, stats))
        consumers = [
            asyncio.create_task(consume_streams(session, symbols[offset : offset + STREAMS_PER_CONNECTION], queue, index + 1))
            for index, offset in enumerate(range(0, len(symbols), STREAMS_PER_CONNECTION))
        ]
        try:
            await asyncio.wait([writer, reporter, recovery, *consumers], return_when=asyncio.FIRST_COMPLETED)
            for task in [writer, reporter, recovery, *consumers]:
                if task.done():
                    task.result()
            raise RuntimeError("Collector task stopped unexpectedly")
        finally:
            for task in [*consumers, recovery]:
                task.cancel()
            await asyncio.gather(*consumers, recovery, return_exceptions=True)
            reporter.cancel()
            if not writer.done():
                logging.info("Draining pending ticks before shutdown (up to %d seconds)", COLLECTOR_SHUTDOWN_SECONDS)
                try:
                    await asyncio.wait_for(queue.join(), timeout=COLLECTOR_SHUTDOWN_SECONDS)
                except TimeoutError:
                    logging.error("Shutdown drain timed out; uncommitted ticks require recovery on restart")
            writer.cancel()
            await asyncio.gather(writer, reporter, return_exceptions=True)


async def run_forever() -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    previous_handlers = {}
    for name in ("SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous_handlers[signum] = signal.signal(signum, lambda *_: loop.call_soon_threadsafe(task.cancel))
    try:
        while True:
            try:
                await main()
            except Exception:
                logging.exception("Collector failed; restarting in %d seconds", RECONNECT_DELAY_SECONDS)
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(DB_PATH.parent / "collector.log", maxBytes=20_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(handler)
    except OSError:
        logging.exception("Cannot open collector log; using console logging only")
    collector_lock = ProcessLock(COLLECTOR_LOCK_PATH)
    if not collector_lock.acquire():
        logging.info("Collector is already running; exiting")
    else:
        try:
            asyncio.run(run_forever())
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            collector_lock.release()