from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from flask_compress import Compress
from flask_sock import Sock
from simple_websocket import ConnectionClosed
from werkzeug.exceptions import HTTPException

from cvd.config import (
    BINANCE_DEPTH_WS_URL,
    CLEANUP_AUTO_START,
    COLLECTOR_AUTO_START,
    COLLECTOR_LOCK_PATH,
    COLLECTOR_SHUTDOWN_SECONDS,
    DB_PATH,
    MARKET_DATA_THREADS,
    NOTIFIER_AUTO_START,
    OI_CHANGE_LENGTH,
    QUOTE_ASSETS,
    SERVER_HOST,
    SERVER_PORT,
    SYMBOLS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    WEBSOCKET_POLL_SECONDS,
)
from cvd.database import (
    aggregate_cvd_from_minutes,
    connect,
    ensure_database_ready,
    latest_trade_id,
    minute_rollup_migration_required,
    recovery_revision,
    trades_after,
)
from cvd.indicators import enrich_indicators
from cvd.market import (
    INTERVALS_MS,
    align_open_interest,
    fetch_current_open_interest,
    fetch_klines,
    fetch_open_interest_history,
    fetch_symbols,
)
from cvd.process_lock import process_is_running

app = Flask(__name__)
app.config["COMPRESS_MIN_SIZE"] = 1_024
Compress(app)
sock = Sock(app)
PROJECT_ROOT = Path(__file__).resolve().parent
MARKET_EXECUTOR = ThreadPoolExecutor(max_workers=MARKET_DATA_THREADS, thread_name_prefix="market-data")


class WorkerSupervisor:
    def __init__(self) -> None:
        self.processes: list[subprocess.Popen] = []
        self.workers: dict[str, subprocess.Popen | None] = {}
        self.stopping = threading.Event()
        self.monitor_thread: threading.Thread | None = None

    def _launch(self, script: str) -> subprocess.Popen | None:
        try:
            process = subprocess.Popen(
                [sys.executable, "-u", str(PROJECT_ROOT / script)], cwd=PROJECT_ROOT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
        except OSError:
            logging.exception("Failed to start %s; supervisor will retry", script)
            return None
        self.processes.append(process)
        logging.info("Started %s (PID %d)", script, process.pid)
        return process

    def check_workers(self) -> None:
        for script, process in list(self.workers.items()):
            if self.stopping.is_set():
                return
            if process is not None and process.poll() is None:
                continue
            if script == "collector.py" and process_is_running(COLLECTOR_LOCK_PATH):
                continue
            if process is not None:
                logging.error("%s exited with code %s; restarting", script, process.returncode)
                self.processes.remove(process)
            self.workers[script] = self._launch(script)

    def _monitor(self) -> None:
        while not self.stopping.wait(5):
            try:
                self.check_workers()
            except Exception:
                logging.exception("Worker supervision failed; will retry")

    def start(self) -> None:
        scripts: list[str] = []
        if COLLECTOR_AUTO_START:
            if process_is_running(COLLECTOR_LOCK_PATH):
                logging.info("Collector is already running; server will use the existing process")
                self.workers["collector.py"] = None
            else:
                scripts.append("collector.py")
        if CLEANUP_AUTO_START:
            scripts.append("cleanup.py")
        if NOTIFIER_AUTO_START and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            scripts.append("notifier.py")
        elif NOTIFIER_AUTO_START:
            logging.warning("ABS notifier disabled: set notifications.bot_token and notifications.chat_id in config.json")
        for script in scripts:
            self.workers[script] = self._launch(script)
        if self.workers:
            self.monitor_thread = threading.Thread(target=self._monitor, name="worker-supervisor", daemon=True)
            self.monitor_thread.start()

    def stop(self) -> None:
        self.stopping.set()
        if self.monitor_thread is not None:
            self.monitor_thread.join()
        for process in self.processes:
            if process.poll() is None:
                logging.info("Stopping worker PID %d", process.pid)
                try:
                    if self.workers.get("collector.py") is process:
                        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
                    else:
                        process.terminate()
                except OSError:
                    logging.exception("Could not signal worker PID %d", process.pid)
        for process in self.processes:
            try:
                timeout = COLLECTOR_SHUTDOWN_SECONDS + 10 if self.workers.get("collector.py") is process else 10
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logging.error("Worker PID %d did not stop gracefully; terminating", process.pid)
                process.kill()
                process.wait()


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info(
        "Startup plan: collector=%s, notifier=%s, cleanup=%s",
        "enabled" if COLLECTOR_AUTO_START else "disabled",
        "enabled" if NOTIFIER_AUTO_START and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else "disabled",
        "enabled" if CLEANUP_AUTO_START else "disabled (raw ticks retained)",
    )
    if minute_rollup_migration_required(DB_PATH) and process_is_running(COLLECTOR_LOCK_PATH):
        raise RuntimeError(
            "A one-time database migration is required while an external collector is running. "
            "Stop collector.py, then start app.py again; app.py will migrate first and restart its managed collector."
        )
    logging.info("Preparing database at %s", DB_PATH)
    started_at = time.monotonic()
    changed = ensure_database_ready(DB_PATH)
    logging.info(
        "Database ready in %.2f seconds (%s)",
        time.monotonic() - started_at,
        "schema created or upgraded" if changed else "schema already current; no write lock used",
    )
    workers = WorkerSupervisor()
    workers.start()
    try:
        logging.info("Starting web server at http://%s:%d", SERVER_HOST, SERVER_PORT)
        app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False)
    finally:
        logging.info("Stopping managed workers")
        workers.stop()


@app.get("/")
def index():
    return render_template("index.html", intervals=INTERVALS_MS.keys())


@app.get("/api/symbols")
def symbols():
    try:
        return jsonify(
            [
                item
                for item in fetch_symbols()
                if (not SYMBOLS or item["symbol"] in SYMBOLS)
                and (not QUOTE_ASSETS or item["quoteAsset"] in QUOTE_ASSETS)
            ]
        )
    except (urllib.error.URLError, TimeoutError, KeyError) as error:
        return jsonify({"error": f"Binance symbols unavailable: {error}"}), 502


@app.get("/api/collector-status")
def collector_status():
    connection = connect(DB_PATH)
    try:
        connection.execute("BEGIN")
        latest = connection.execute("SELECT MAX(trade_time) FROM symbol_trade_state").fetchone()[0]
        gaps = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(last_id - first_id + 1), 0) FROM trade_gaps"
        ).fetchone()
        connection.commit()
    finally:
        connection.close()
    return jsonify({
        "running": process_is_running(COLLECTOR_LOCK_PATH),
        "lastTradeTime": latest,
        "lastTradeAgeSeconds": None if latest is None else max(0, (time.time() * 1000 - latest) / 1000),
        "pendingGaps": int(gaps[0]),
        "pendingTradeIds": int(gaps[1]),
    })


@app.get("/api/chart")
def chart_data():
    symbol = request.args.get("symbol", "BTCUSDT").strip().upper()
    interval = request.args.get("interval", "15m")
    try:
        limit = min(max(int(request.args.get("limit", "500")), 60), 1000)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    if not symbol.isalnum() or len(symbol) > 20:
        return jsonify({"error": "Invalid symbol"}), 400
    if interval not in INTERVALS_MS:
        return jsonify({"error": "Unsupported interval"}), 400

    candles_future = MARKET_EXECUTOR.submit(fetch_klines, symbol, interval, limit)
    oi_future = MARKET_EXECUTOR.submit(fetch_open_interest_history, symbol, interval, limit)
    try:
        candles = candles_future.result()
    except urllib.error.HTTPError as error:
        status = 400 if error.code == 400 else 502
        return jsonify({"error": f"Binance rejected the request ({error.code})"}), status
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as error:
        return jsonify({"error": f"Binance market data unavailable: {error}"}), 502

    if not candles:
        return jsonify({"error": "No candles returned for this symbol"}), 404
    interval_ms = INTERVALS_MS[interval]
    start_ms = int(candles[0]["time"]) * 1000
    end_ms = max(int(time.time() * 1000) + 1, (int(candles[-1]["time"]) * 1000) + interval_ms)
    connection = connect(DB_PATH)
    try:
        connection.execute("BEGIN")
        revision = recovery_revision(connection, symbol)
        checkpoint = latest_trade_id(connection, symbol)
        cvd = aggregate_cvd_from_minutes(connection, symbol, interval_ms, start_ms, end_ms)
        connection.commit()
    finally:
        connection.close()
    oi_error = None
    try:
        oi_history = oi_future.result()
    except Exception as error:
        logging.warning("Open interest unavailable for %s: %s", symbol, error)
        oi_history = []
        oi_error = f"{type(error).__name__}: {error}"
    deltas_by_time = {int(row["time"]): float(row["delta"]) for row in cvd}
    open_interest_by_time = align_open_interest(candles, oi_history)
    compact = request.args.get("compact") == "1"
    if compact:
        indicators = [
            {"time": candle["time"], "openInterest": open_interest_by_time.get(int(candle["time"]))}
            for candle in candles
        ]
        candles = [
            {key: candle[key] for key in ("time", "open", "high", "low", "close", "volume")}
            for candle in candles
        ]
    else:
        indicators = enrich_indicators(candles, deltas_by_time, open_interest_by_time, OI_CHANGE_LENGTH)
    return jsonify(
        {
            "symbol": symbol,
            "interval": interval,
            "intervalMs": interval_ms,
            "lastTradeId": checkpoint,
            "recoveryRevision": revision,
            "candles": candles,
            "cvd": cvd,
            "indicators": indicators,
            "oiError": oi_error,
            "oiChangeLength": OI_CHANGE_LENGTH,
            "depthWebSocketUrl": BINANCE_DEPTH_WS_URL,
        }
    )


@app.get("/api/open-interest")
def current_open_interest():
    symbol = request.args.get("symbol", "BTCUSDT").strip().upper()
    try:
        return jsonify(fetch_current_open_interest(symbol))
    except Exception as error:
        logging.warning("Current open interest unavailable for %s: %s", symbol, error)
        return jsonify({"error": str(error), "type": type(error).__name__}), 502


@sock.route("/ws/market")
def market_socket(websocket):
    stream_market(websocket)


def stream_market(websocket):
    symbol = request.args.get("symbol", "BTCUSDT").strip().upper()
    try:
        trade_id = max(int(request.args.get("after", "0")), 0)
        requested_revision = request.args.get("revision")
        revision = None if requested_revision is None else int(requested_revision)
        if trade_id > 2**63 - 1 or (revision is not None and revision < 0):
            raise ValueError
    except ValueError:
        websocket.send(json.dumps({"error": "after/revision must be valid nonnegative integers"}))
        return
    if not symbol.isalnum() or len(symbol) > 20:
        websocket.send(json.dumps({"error": "Invalid symbol"}))
        return
    connection = connect(DB_PATH)
    last_heartbeat = 0.0
    try:
        while True:
            connection.execute("BEGIN")
            current_revision = recovery_revision(connection, symbol)
            rows = trades_after(connection, symbol, trade_id)
            connection.commit()
            if revision is None:
                revision = current_revision
            if current_revision != revision:
                websocket.send(json.dumps({"type": "reload", "symbol": symbol}))
                return
            if rows:
                trade_id = int(rows[-1]["tradeId"])
                websocket.send(json.dumps({"type": "trades", "symbol": symbol, "trades": rows}))
            elif time.monotonic() - last_heartbeat >= 15:
                websocket.send(json.dumps({"type": "heartbeat", "symbol": symbol}))
                last_heartbeat = time.monotonic()
            time.sleep(WEBSOCKET_POLL_SECONDS)
    except ConnectionClosed:
        return
    except Exception:
        logging.exception("Market WebSocket failed for %s after trade %d", symbol, trade_id)
        try:
            websocket.send(json.dumps({"error": traceback.format_exc()}))
        except ConnectionClosed:
            pass
    finally:
        connection.close()


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        return error
    logging.exception("Unhandled request error: %s %s", request.method, request.path)
    details = traceback.format_exc()
    if request.path.startswith("/api/"):
        return jsonify({"error": str(error), "type": type(error).__name__, "details": details}), 500
    return f"<h1>Internal Server Error</h1><pre>{details}</pre>", 500


if __name__ == "__main__":
    run()