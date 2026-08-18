from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
import traceback
import urllib.error
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from flask_sock import Sock
from simple_websocket import ConnectionClosed
from werkzeug.exceptions import HTTPException

from cvd.config import DB_PATH
from cvd.database import aggregate_cvd, connect, initialize, latest_trade_id, trades_after
from cvd.market import INTERVALS_MS, fetch_klines, fetch_symbols

app = Flask(__name__)
sock = Sock(app)
initialize(DB_PATH)
PROJECT_ROOT = Path(__file__).resolve().parent


class WorkerSupervisor:
    def __init__(self) -> None:
        self.processes: list[subprocess.Popen] = []

    def start(self) -> None:
        for script in ("collector.py", "cleanup.py"):
            process = subprocess.Popen([sys.executable, str(PROJECT_ROOT / script)], cwd=PROJECT_ROOT)
            self.processes.append(process)

    def stop(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def run() -> None:
    workers = WorkerSupervisor()
    workers.start()
    try:
        app.run(host="127.0.0.1", port=5000, debug=False)
    finally:
        workers.stop()


@app.get("/")
def index():
    return render_template("index.html", intervals=INTERVALS_MS.keys())


@app.get("/api/symbols")
def symbols():
    try:
        return jsonify([item for item in fetch_symbols() if item["quoteAsset"] == "USDT"])
    except (urllib.error.URLError, TimeoutError, KeyError) as error:
        return jsonify({"error": f"Binance symbols unavailable: {error}"}), 502


@app.get("/api/chart")
def chart_data():
    symbol = request.args.get("symbol", "BTCUSDT").strip().upper()
    interval = request.args.get("interval", "5m")
    try:
        limit = min(max(int(request.args.get("limit", "500")), 60), 1000)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    if not symbol.isalnum() or len(symbol) > 20:
        return jsonify({"error": "Invalid symbol"}), 400
    if interval not in INTERVALS_MS:
        return jsonify({"error": "Unsupported interval"}), 400

    try:
        candles = fetch_klines(symbol, interval, limit)
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
        checkpoint = latest_trade_id(connection, symbol)
        cvd = aggregate_cvd(connection, symbol, interval_ms, start_ms, end_ms, checkpoint)
    finally:
        connection.close()
    return jsonify(
        {
            "symbol": symbol,
            "interval": interval,
            "intervalMs": interval_ms,
            "lastTradeId": checkpoint,
            "candles": candles,
            "cvd": cvd,
        }
    )


@sock.route("/ws/market")
def market_socket(websocket):
    symbol = request.args.get("symbol", "BTCUSDT").strip().upper()
    try:
        trade_id = max(int(request.args.get("after", "0")), 0)
    except ValueError:
        websocket.send(json.dumps({"error": "after must be an integer"}))
        return
    connection = connect(DB_PATH)
    try:
        while True:
            rows = trades_after(connection, symbol, trade_id)
            if rows:
                trade_id = int(rows[-1]["tradeId"])
                websocket.send(json.dumps({"type": "trades", "symbol": symbol, "trades": rows}))
            time.sleep(0.25)
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