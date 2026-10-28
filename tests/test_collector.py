import asyncio
import sqlite3
import contextlib
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import collector
from aiohttp import web
from cvd.database import connect, next_trade_gap, recovery_revision


class CollectorTest(unittest.IsolatedAsyncioTestCase):
    def test_parse_trade_rejects_bad_messages(self) -> None:
        valid = {"e": "trade", "s": "BTCUSDT", "t": 1, "T": 1000, "p": "100", "q": "2", "m": False}
        self.assertEqual(collector.parse_trade(valid), ("BTCUSDT", 1, 1000, 100.0, 2.0, 0))
        for payload in [None, [], {}, {"e": "trade"}, {**valid, "p": "NaN"},
                        {**valid, "q": "inf"}, {**valid, "m": "false"}, {**valid, "t": 2**64}]:
            with self.subTest(payload=payload):
                self.assertIsNone(collector.parse_trade(payload))

    async def test_writer_retries_same_batch_before_acknowledging(self) -> None:
        queue = asyncio.Queue()
        trade = ("BTCUSDT", 1, 1000, 100.0, 2.0, 0)
        await queue.put(trade)
        stats = collector.InsertionStats()
        with patch.object(collector, "persist_batch", side_effect=[sqlite3.OperationalError("locked"), 1]) as persist:
            with patch.object(collector, "RECONNECT_DELAY_SECONDS", 0):
                writer = asyncio.create_task(collector.write_batches(queue, stats))
                try:
                    await asyncio.wait_for(queue.join(), timeout=2)
                    self.assertEqual(persist.call_count, 2)
                    self.assertEqual(persist.call_args_list[0], persist.call_args_list[1])
                    self.assertEqual(stats.rows, 1)
                finally:
                    writer.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await writer

    def test_history_requires_consecutive_raw_ids(self) -> None:
        rows = [{"id": trade_id, "time": 1000, "price": "100", "qty": "1", "isBuyerMaker": False}
                for trade_id in (11, 12)]
        self.assertEqual([trade[1] for trade in collector.parse_history(rows, ("BTCUSDT", 11, 12))], [11, 12])
        for payload in [[], {}, rows[1:], [rows[0], {**rows[1], "id": 13}]]:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    collector.parse_history(payload, ("BTCUSDT", 11, 12))

    async def test_startup_failure_is_retried_without_exiting(self) -> None:
        with patch.object(collector, "main", new=AsyncMock(side_effect=[OSError("offline"), asyncio.CancelledError])) as main:
            with patch.object(collector.asyncio, "sleep", new=AsyncMock()) as sleep:
                with self.assertRaises(asyncio.CancelledError):
                    await collector.run_forever()
                self.assertEqual(main.await_count, 2)
                sleep.assert_awaited_once_with(collector.RECONNECT_DELAY_SECONDS)

    async def test_rate_limit_preserves_gap_and_honors_retry_after(self) -> None:
        gap = ("BTCUSDT", 11, 12)
        response = MagicMock()
        response.status = 429
        response.headers = {"Retry-After": "120"}
        response.raise_for_status.side_effect = aiohttp.ClientResponseError(
            MagicMock(real_url="http://localhost/fake"), (), status=429,
        )
        session = MagicMock()
        session.get.return_value.__aenter__ = AsyncMock(return_value=response)
        session.get.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(collector, "load_gap", return_value=gap):
            with patch.object(collector, "defer_gap") as defer:
                with patch.object(collector, "persist_recovered") as persist:
                    with patch.object(collector.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)) as sleep:
                        with self.assertRaises(asyncio.CancelledError):
                            await collector.recover_gaps(session, collector.InsertionStats())
                        sleep.assert_awaited_once_with(120)
                        defer.assert_called_once_with(gap, 120)
                        persist.assert_not_called()

    async def test_recovery_does_not_overwrite_concurrent_ingestion_stats(self) -> None:
        gap = ("BTCUSDT", 11, 11)
        stats = collector.InsertionStats(rows=1)
        response = MagicMock(status=200)
        response.json = AsyncMock(return_value=[
            {"id": 11, "time": 1000, "price": "100", "qty": "1", "isBuyerMaker": False},
        ])
        session = MagicMock()
        session.get.return_value.__aenter__ = AsyncMock(return_value=response)
        session.get.return_value.__aexit__ = AsyncMock(return_value=False)

        async def operation(function, *args):
            if function is collector.load_gap:
                return gap
            stats.rows += 5
            return 1

        with patch.object(collector, "database_operation", side_effect=operation):
            with patch.object(collector.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await collector.recover_gaps(session, stats)
        self.assertEqual(stats.rows, 7)

    async def test_rejected_subscription_reconnects_and_collects_valid_tick(self) -> None:
        websocket = MagicMock()
        websocket.send_json = AsyncMock()
        websocket.receive = AsyncMock(side_effect=[
            aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, '{"code": 2, "msg": "rejected"}', ""),
            aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, '{"result": null, "id": 1}', ""),
            aiohttp.WSMessage(aiohttp.WSMsgType.TEXT,
                              '{"e":"trade","s":"BTCUSDT","t":1,"T":1000,"p":"100","q":"1","m":false}', ""),
            asyncio.CancelledError,
        ])
        session = MagicMock()
        session.ws_connect.return_value.__aenter__ = AsyncMock(return_value=websocket)
        session.ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)
        queue = asyncio.Queue()
        with patch.object(collector.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaises(asyncio.CancelledError):
                await collector.consume_streams(session, ["BTCUSDT"], queue, 1)
        self.assertEqual(session.ws_connect.call_count, 2)
        self.assertEqual(queue.get_nowait(), ("BTCUSDT", 1, 1000, 100.0, 1.0, 0))

    async def test_local_exchange_disconnect_backfill_and_graceful_stop(self) -> None:
        connections = 0
        repaired = asyncio.Event()
        history_calls = []

        async def websocket_handler(request):
            nonlocal connections
            connections += 1
            connection_number = connections
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            subscription = await websocket.receive_json()
            await websocket.send_json({"id": subscription["id"], "result": None})
            await websocket.send_str("invalid JSON")
            await websocket.send_json({"e": "trade"})
            trade_id = 10 if connection_number == 1 else 13
            await websocket.send_json({"e": "trade", "s": "BTCUSDT", "t": trade_id,
                                       "T": 1000 + trade_id, "p": "100", "q": "1", "m": False})
            if connection_number == 1:
                await websocket.close()
            else:
                async for message in websocket:
                    pass
            return websocket

        async def history_handler(request):
            history_calls.append(dict(request.query))
            return web.json_response([
                {"id": trade_id, "time": 1000 + trade_id, "price": "100", "qty": "1", "isBuyerMaker": False}
                for trade_id in (11, 12)
            ])

        exchange = web.Application()
        exchange.router.add_get("/ws", websocket_handler)
        exchange.router.add_get("/api/v3/historicalTrades", history_handler)
        runner = web.AppRunner(exchange)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        original_persist = collector.persist_recovered

        def persist_and_signal(gap, trades):
            result = original_persist(gap, trades)
            loop.call_soon_threadsafe(repaired.set)
            return result

        loop = asyncio.get_running_loop()
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "ticks.db"
                with patch.multiple(collector, DB_PATH=path, BINANCE_WS_URL=f"http://127.0.0.1:{port}/ws",
                                    BINANCE_REST_URL=f"http://127.0.0.1:{port}", RECONNECT_DELAY_SECONDS=0.01):
                    with patch.object(collector, "selected_symbols", return_value=["BTCUSDT"]):
                        with patch.object(collector, "persist_recovered", side_effect=persist_and_signal):
                            task = asyncio.create_task(collector.main())
                            try:
                                await asyncio.wait_for(repaired.wait(), timeout=5)
                            finally:
                                task.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await task
                connection = connect(path)
                try:
                    ids = [row[0] for row in connection.execute("SELECT trade_id FROM spot_trades ORDER BY trade_id")]
                    self.assertEqual(ids, [10, 11, 12, 13])
                    self.assertIsNone(next_trade_gap(connection))
                    self.assertEqual(recovery_revision(connection, "BTCUSDT"), 1)
                finally:
                    connection.close()
                self.assertGreaterEqual(connections, 2)
                self.assertEqual(history_calls, [{"symbol": "BTCUSDT", "fromId": "11", "limit": "2"}])
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()