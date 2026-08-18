from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass

import aiohttp

from cvd.config import BINANCE_WS_URL, DB_PATH, QUOTE_ASSETS, SYMBOLS
from cvd.database import connect, initialize, insert_trades
from cvd.market import fetch_symbols

Trade = tuple[str, int, int, float, float, int]
STREAMS_PER_CONNECTION = 500
WRITE_BATCH_SIZE = 5_000


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


def parse_trade(payload: dict[str, object]) -> Trade | None:
    if payload.get("e") != "trade":
        return None
    return (
        str(payload["s"]),
        int(payload["t"]),
        int(payload["T"]),
        float(payload["p"]),
        float(payload["q"]),
        int(bool(payload["m"])),
    )


async def write_batches(queue: asyncio.Queue[Trade], stats: InsertionStats | None = None) -> None:
    connection = connect(DB_PATH)
    try:
        while True:
            first = await queue.get()
            batch = [first]
            while len(batch) < WRITE_BATCH_SIZE:
                try:
                    batch.append(await asyncio.wait_for(queue.get(), timeout=0.1))
                except TimeoutError:
                    break
            inserted = insert_trades(connection, batch)
            if stats is not None:
                stats.rows += inserted
            for _ in batch:
                queue.task_done()
            if inserted:
                logging.debug("Inserted %d raw trades", inserted)
    finally:
        connection.close()


async def report_ingestion(stats: InsertionStats, queue: asyncio.Queue[Trade]) -> None:
    while True:
        await asyncio.sleep(60)
        logging.info(
            "Collector minute summary: %d new rows inserted; queue depth %d",
            stats.take(),
            queue.qsize(),
        )


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
            async with session.ws_connect(BINANCE_WS_URL, heartbeat=30) as websocket:
                await websocket.send_json({"method": "SUBSCRIBE", "params": streams, "id": connection_number})
                async for message in websocket:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        trade = parse_trade(json.loads(message.data))
                        if trade is not None:
                            await queue.put(trade)
                    elif message.type in {aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED}:
                        break
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as error:
            logging.warning("Stream group %d disconnected: %s", connection_number, error)
        await asyncio.sleep(5)


async def main() -> None:
    initialize(DB_PATH)
    symbols = await asyncio.to_thread(selected_symbols)
    if not symbols:
        raise RuntimeError("No matching Binance Spot symbols were found")
    logging.info("Collecting raw trade ticks for %d symbols", len(symbols))
    queue: asyncio.Queue[Trade] = asyncio.Queue(maxsize=200_000)
    stats = InsertionStats()
    writer = asyncio.create_task(write_batches(queue, stats))
    reporter = asyncio.create_task(report_ingestion(stats, queue))
    async with aiohttp.ClientSession() as session:
        consumers = [
            asyncio.create_task(consume_streams(session, symbols[offset : offset + STREAMS_PER_CONNECTION], queue, index + 1))
            for index, offset in enumerate(range(0, len(symbols), STREAMS_PER_CONNECTION))
        ]
        try:
            await asyncio.gather(writer, reporter, *consumers)
        finally:
            for consumer in consumers:
                consumer.cancel()
            writer.cancel()
            reporter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass