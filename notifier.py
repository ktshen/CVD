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

INTERVAL = "5m"
INTERVAL_MS = INTERVALS_MS[INTERVAL]
ZSCORE_LENGTH = 100


@dataclass(frozen=True)
class AbsSignal:
    symbol: str
    bucket_time: int
    direction: str
    price: float
    usd_volume: float
    usd_volume_ma20: float
    delta_z: float


def detect_abs_signal(symbol: str, buckets: list[dict[str, float | int]]) -> AbsSignal | None:
    if len(buckets) < ZSCORE_LENGTH:
        return None
    recent = buckets[-ZSCORE_LENGTH:]
    expected_step = INTERVAL_MS // 1000
    if any(int(right["time"]) - int(left["time"]) != expected_step for left, right in zip(recent, recent[1:])):
        return None
    delta_z = rolling_zscore([float(bucket["delta"]) for bucket in recent])[-1]
    latest = recent[-1]
    price_range = float(latest["high"]) - float(latest["low"])
    close_position = 0.5 if price_range == 0 else (float(latest["close"]) - float(latest["low"])) / price_range
    direction = None
    if delta_z is not None and delta_z < -2 and close_position > 0.55:
        direction = "bullish"
    elif delta_z is not None and delta_z > 2 and close_position < 0.45:
        direction = "bearish"
    if direction is None:
        return None
    usd_volume = float(latest["usdVolume"])
    usd_volume_ma20 = sum(float(bucket["usdVolume"]) for bucket in recent[-20:]) / 20
    return AbsSignal(
        symbol=symbol,
        bucket_time=int(latest["time"]),
        direction=direction,
        price=float(latest["close"]),
        usd_volume=usd_volume,
        usd_volume_ma20=usd_volume_ma20,
        delta_z=float(delta_z),
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

    def capture(self, symbol: str, output_path: Path) -> None:
        assert self.browser is not None
        page = self.browser.new_page(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
            is_mobile=True,
        )
        query = urllib.parse.urlencode({"symbol": symbol, "interval": INTERVAL, "snapshot": "1"})
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
            f"5m {signal.direction.upper()} ABS",
            f"Symbol: {signal.symbol}",
            f"Price: {signal.price:,.8f}",
            f"USD volume: {format_usd(signal.usd_volume)}",
            f"USD volume MA20: {format_usd(signal.usd_volume_ma20)}",
            f"Delta Z-score: {signal.delta_z:.3f}",
            f"Candle: {timestamp}",
        ]
    )


def evaluate_symbol(connection, symbol: str) -> AbsSignal | None:
    current_bucket_ms = int(time.time() * 1000) // INTERVAL_MS * INTERVAL_MS
    start_ms = current_bucket_ms - (ZSCORE_LENGTH - 1) * INTERVAL_MS
    buckets = recent_trade_buckets_from_minutes(connection, symbol, INTERVAL_MS, start_ms, ZSCORE_LENGTH)
    return detect_abs_signal(symbol, buckets)


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
    connection = connect(DB_PATH)
    seen_markers: dict[str, int] = {}
    try:
        wait_for_dashboard()
        with MobileChartCapture(NOTIFIER_CHART_BASE_URL) as capture:
            logging.info("Monitoring all collected symbols for live 5m ABS signals")
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
                        signal = evaluate_symbol(connection, symbol)
                        if signal is not None and not notification_was_sent(
                            connection, signal.symbol, signal.bucket_time, signal.direction
                        ):
                            with tempfile.TemporaryDirectory() as temp_dir:
                                screenshot = Path(temp_dir) / f"{signal.symbol}-5m-abs.png"
                                capture.capture(signal.symbol, screenshot)
                                send_telegram_photo(
                                    TELEGRAM_BOT_TOKEN,
                                    TELEGRAM_CHAT_ID,
                                    screenshot,
                                    signal_caption(signal),
                                )
                            record_notification(connection, signal.symbol, signal.bucket_time, signal.direction)
                            sent += 1
                            logging.info("Sent %s 5m ABS alert for %s", signal.direction, signal.symbol)
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
    args = parser.parse_args()
    if args.discover_chat_id:
        discover_chat_ids()
    else:
        monitor()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except KeyboardInterrupt:
        pass