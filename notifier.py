from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from cvd.config import (
    DB_PATH,
    NOTIFIER_CHART_BASE_URL,
    NOTIFIER_POLL_SECONDS,
    NOTIFIER_REPORT_INTERVAL_SECONDS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from cvd.database import (
    connect,
    ensure_database_ready,
    latest_symbol_markers,
    notification_was_sent,
    recent_trade_buckets_from_minutes,
    record_notification,
)
from cvd.indicators import rolling_zscore
from cvd.market import INTERVALS_MS

TIMEFRAME_VOLUME_THRESHOLDS = {
    "1m": 20_000.0,
    "5m": 50_000.0,
}
ZSCORE_LENGTH = 100


@dataclass(frozen=True)
class AbsSignal:
    symbol: str
    interval: str
    bucket_time: int
    direction: str
    price: float
    usd_volume: float
    usd_volume_ma20: float
    delta_z: float
    volume_threshold: float

    @property
    def notification_key(self) -> str:
        return f"{self.interval}:{self.direction}"


def detect_abs_signal(
    symbol: str,
    interval: str,
    buckets: list[dict[str, float | int]],
    usd_volume_threshold: float,
) -> AbsSignal | None:
    if len(buckets) < ZSCORE_LENGTH:
        return None
    recent = buckets[-ZSCORE_LENGTH:]
    expected_step = INTERVALS_MS[interval] // 1000
    if any(int(right["time"]) - int(left["time"]) != expected_step for left, right in zip(recent, recent[1:])):
        return None
    delta_z = rolling_zscore([float(bucket["delta"]) for bucket in recent])[-1]
    latest = recent[-1]
    price_range = float(latest["high"]) - float(latest["low"])
    close_position = 0.5 if price_range == 0 else (float(latest["close"]) - float(latest["low"])) / price_range
    usd_volume = float(latest["usdVolume"])
    if usd_volume <= usd_volume_threshold:
        return None
    if delta_z is None or delta_z >= -2 or close_position <= 0.55:
        return None
    usd_volume_ma20 = sum(float(bucket["usdVolume"]) for bucket in recent[-20:]) / 20
    return AbsSignal(
        symbol=symbol,
        interval=interval,
        bucket_time=int(latest["time"]),
        direction="bullish",
        price=float(latest["close"]),
        usd_volume=usd_volume,
        usd_volume_ma20=usd_volume_ma20,
        delta_z=float(delta_z),
        volume_threshold=usd_volume_threshold,
    )


def telegram_json(method: str, token: str) -> object:
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Telegram {method} failed: {type(error).__name__}") from error
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {payload.get('description', 'unknown error')}")
    return payload["result"]


def telegram_post_json(method: str, token: str, data: dict[str, str]) -> object:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            description = json.load(error).get("description", str(error))
        except (json.JSONDecodeError, AttributeError):
            description = str(error)
        raise RuntimeError(f"Telegram {method} failed: {description}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Telegram {method} failed: {type(error).__name__}") from error
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {payload.get('description', 'unknown error')}")
    return payload["result"]


def validate_telegram_destination() -> None:
    telegram_post_json("getChat", TELEGRAM_BOT_TOKEN, {"chat_id": TELEGRAM_CHAT_ID})
    logging.info("Telegram destination validated (chat ID ending in %s)", TELEGRAM_CHAT_ID[-4:])


def send_test_message() -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Set notifications.bot_token and notifications.chat_id in config.json")
    validate_telegram_destination()
    telegram_post_json(
        "sendMessage",
        TELEGRAM_BOT_TOKEN,
        {"chat_id": TELEGRAM_CHAT_ID, "text": "CVD notifier test: Telegram delivery is working."},
    )
    print("Telegram test message sent successfully.")


def send_telegram_photo(token: str, chat_id: str, photo_path: Path, caption: str) -> None:
    boundary = uuid.uuid4().hex
    photo = photo_path.read_bytes()
    content_type = mimetypes.guess_type(photo_path.name)[0] or "image/png"
    parts: list[bytes] = []
    for name, value in (("chat_id", chat_id), ("caption", caption)):
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    parts.extend(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="photo"; filename="{photo_path.name}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            photo,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Telegram sendPhoto failed: {type(error).__name__}") from error
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto failed: {payload.get('description', 'unknown error')}")


class MobileChartCapture:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.playwright = None
        self.browser = None

    def __enter__(self) -> MobileChartCapture:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError("Playwright is not installed; run: python -m pip install -r requirements.txt") from error
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch(headless=True)
        except Exception as chromium_error:
            try:
                self.browser = self.playwright.chromium.launch(channel="msedge", headless=True)
            except Exception as edge_error:
                self.playwright.stop()
                raise RuntimeError(
                    "No Playwright browser is available; install Chromium or Microsoft Edge"
                ) from edge_error
            logging.info("Bundled Chromium unavailable; using Microsoft Edge: %s", type(chromium_error).__name__)
        return self

    def __exit__(self, *_: object) -> None:
        if self.browser is not None:
            self.browser.close()
        if self.playwright is not None:
            self.playwright.stop()

    def capture(self, symbol: str, interval: str, output_path: Path) -> None:
        assert self.browser is not None
        page = self.browser.new_page(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
            is_mobile=True,
        )
        query = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "snapshot": "1"})
        try:
            page.goto(f"{self.base_url}/?{query}", wait_until="domcontentloaded", timeout=30_000)
            page.locator('body[data-chart-ready="true"]').wait_for(timeout=30_000)
            page.locator(".chart-shell").screenshot(path=str(output_path))
        finally:
            page.close()


def format_usd(value: float) -> str:
    return f"${value:,.2f}"


def signal_caption(signal: AbsSignal) -> str:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(signal.bucket_time))
    return "\n".join(
        [
            f"{signal.interval} {signal.direction.upper()} ABS",
            f"Symbol: {signal.symbol}",
            f"Price: {signal.price:,.8f}",
            f"USD volume: {format_usd(signal.usd_volume)}",
            f"USD volume threshold: {format_usd(signal.volume_threshold)}",
            f"USD volume MA20: {format_usd(signal.usd_volume_ma20)}",
            f"Delta Z-score: {signal.delta_z:.3f}",
            f"Candle: {timestamp}",
        ]
    )


def evaluate_symbol(connection, symbol: str, interval: str) -> AbsSignal | None:
    interval_ms = INTERVALS_MS[interval]
    current_bucket_ms = int(time.time() * 1000) // interval_ms * interval_ms
    start_ms = current_bucket_ms - (ZSCORE_LENGTH - 1) * interval_ms
    buckets = recent_trade_buckets_from_minutes(connection, symbol, interval_ms, start_ms, ZSCORE_LENGTH)
    if not buckets or int(buckets[-1]["time"]) * 1000 != current_bucket_ms:
        return None
    first_bucket = connection.execute(
        "SELECT MIN(bucket_time) FROM spot_trade_minutes WHERE symbol = ?",
        (symbol.upper(),),
    ).fetchone()[0]
    if first_bucket is None or int(first_bucket) > start_ms:
        return None
    by_time = {int(bucket["time"]): bucket for bucket in buckets}
    dense_buckets: list[dict[str, float | int]] = []
    for bucket_ms in range(start_ms, current_bucket_ms + interval_ms, interval_ms):
        bucket_time = bucket_ms // 1000
        dense_buckets.append(
            by_time.get(
                bucket_time,
                {
                    "time": bucket_time,
                    "open": 0.0,
                    "high": 0.0,
                    "low": 0.0,
                    "close": 0.0,
                    "volume": 0.0,
                    "usdVolume": 0.0,
                    "delta": 0.0,
                },
            )
        )
    return detect_abs_signal(symbol, interval, dense_buckets, TIMEFRAME_VOLUME_THRESHOLDS[interval])


def diagnose() -> None:
    ensure_database_ready(DB_PATH)
    connection = connect(DB_PATH)
    try:
        markers = latest_symbol_markers(connection)
        latest_trade_time = connection.execute("SELECT MAX(trade_time) FROM spot_trades").fetchone()[0]
        diagnostics = []
        signals = []
        for interval, threshold in TIMEFRAME_VOLUME_THRESHOLDS.items():
            interval_ms = INTERVALS_MS[interval]
            current_bucket_ms = int(time.time() * 1000) // interval_ms * interval_ms
            history_start_ms = current_bucket_ms - (ZSCORE_LENGTH - 1) * interval_ms
            eligible = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT symbol FROM spot_trade_minutes
                        GROUP BY symbol
                        HAVING MIN(bucket_time) <= ? AND MAX(bucket_time) >= ?
                    )
                    """,
                    (history_start_ms, current_bucket_ms),
                ).fetchone()[0]
            )
            interval_signals = [
                signal for symbol in markers if (signal := evaluate_symbol(connection, symbol, interval)) is not None
            ]
            diagnostics.append((interval, threshold, eligible, len(interval_signals)))
            signals.extend(interval_signals)
        notifications = int(connection.execute("SELECT COUNT(*) FROM abs_notifications").fetchone()[0])
        age = None if latest_trade_time is None else (int(time.time() * 1000) - int(latest_trade_time)) / 1000
        print(f"Database: {DB_PATH}")
        print(f"Tracked symbols: {len(markers)}")
        print(f"Latest tick age: {'none' if age is None else f'{age:.1f} seconds'}")
        for interval, threshold, eligible, signal_count in diagnostics:
            print(
                f"{interval}: symbols with complete 100-bar collection window: {eligible}; "
                f"bullish ABS above {format_usd(threshold)}: {signal_count}"
            )
        print(f"Current ABS signals: {len(signals)}")
        print(f"Recorded notifications: {notifications}")
        for signal in signals:
            print(f"  {signal.symbol} {signal.interval} {signal.direction} deltaZ={signal.delta_z:.3f}")
    finally:
        connection.close()


def wait_for_dashboard() -> None:
    logging.info("Waiting for chart server at %s", NOTIFIER_CHART_BASE_URL)
    while True:
        try:
            with urllib.request.urlopen(f"{NOTIFIER_CHART_BASE_URL}/", timeout=2) as response:
                if response.status == 200:
                    logging.info("Chart server is ready")
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1)


def monitor() -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Set notifications.bot_token and notifications.chat_id in config.json")
    if ensure_database_ready(DB_PATH):
        logging.info("Database schema created or upgraded")
    else:
        logging.info("Database schema already current")
    validate_telegram_destination()
    connection = connect(DB_PATH)
    seen_markers: dict[str, int] = {}
    try:
        wait_for_dashboard()
        with MobileChartCapture(NOTIFIER_CHART_BASE_URL) as capture:
            logging.info(
                "Monitoring all collected symbols for live bullish ABS signals: %s",
                ", ".join(
                    f"{interval} USD volume > {format_usd(threshold)}"
                    for interval, threshold in TIMEFRAME_VOLUME_THRESHOLDS.items()
                ),
            )
            report_started = time.monotonic()
            evaluated = 0
            sent = 0
            errors = 0
            while True:
                cycle_started = time.monotonic()
                markers = latest_symbol_markers(connection)
                for symbol, marker in markers.items():
                    if seen_markers.get(symbol) == marker:
                        continue
                    evaluated += 1
                    try:
                        for interval in TIMEFRAME_VOLUME_THRESHOLDS:
                            signal = evaluate_symbol(connection, symbol, interval)
                            if signal is None or notification_was_sent(
                                connection, signal.symbol, signal.bucket_time, signal.notification_key
                            ):
                                continue
                            with tempfile.TemporaryDirectory() as temp_dir:
                                screenshot = Path(temp_dir) / f"{signal.symbol}-{signal.interval}-abs.png"
                                capture.capture(signal.symbol, signal.interval, screenshot)
                                send_telegram_photo(
                                    TELEGRAM_BOT_TOKEN,
                                    TELEGRAM_CHAT_ID,
                                    screenshot,
                                    signal_caption(signal),
                                )
                            record_notification(connection, signal.symbol, signal.bucket_time, signal.notification_key)
                            sent += 1
                            logging.info("Sent %s %s ABS alert for %s", signal.interval, signal.direction, signal.symbol)
                        seen_markers[symbol] = marker
                    except Exception:
                        errors += 1
                        logging.exception("Failed to evaluate or notify %s", symbol)
                now = time.monotonic()
                if now - report_started >= NOTIFIER_REPORT_INTERVAL_SECONDS:
                    logging.info(
                        "Notifier summary: %d symbols tracked, %d evaluations, %d alerts, %d errors; "
                        "latest cycle %.3f seconds",
                        len(markers),
                        evaluated,
                        sent,
                        errors,
                        now - cycle_started,
                    )
                    report_started = now
                    evaluated = 0
                    sent = 0
                    errors = 0
                time.sleep(NOTIFIER_POLL_SECONDS)
    finally:
        connection.close()


def discover_chat_ids() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Set notifications.bot_token in config.json, message the bot, then run this command again")
    updates = telegram_json("getUpdates", TELEGRAM_BOT_TOKEN)
    chats: dict[str, str] = {}
    for update in updates:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if "id" in chat:
            chats[str(chat["id"])] = chat.get("title") or chat.get("username") or chat.get("first_name") or "unknown"
    if not chats:
        print("No chats found. Open the bot in Telegram, press Start, send a message, and retry.")
        return
    for chat_id, name in chats.items():
        print(f"{chat_id}\t{name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor live 5m ABS signals and notify Telegram")
    parser.add_argument("--discover-chat-id", action="store_true")
    parser.add_argument("--test-telegram", action="store_true")
    parser.add_argument("--diagnose", action="store_true")
    args = parser.parse_args()
    if args.discover_chat_id:
        discover_chat_ids()
    elif args.test_telegram:
        send_test_message()
    elif args.diagnose:
        diagnose()
    else:
        monitor()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except KeyboardInterrupt:
        pass