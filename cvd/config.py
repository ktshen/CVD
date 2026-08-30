from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "database": {"path": "data/market.db"},
    "binance": {
        "rest_url": "https://data-api.binance.vision",
        "websocket_url": "wss://data-stream.binance.vision/ws",
        "futures_rest_url": "https://fapi.binance.com",
        "depth_websocket_url": "wss://data-stream.binance.vision/ws",
    },
    "collector": {
        "auto_start": True,
        "quote_assets": ["USDT"],
        "symbols": [],
        "streams_per_connection": 500,
        "write_batch_size": 5_000,
        "queue_max_size": 200_000,
        "websocket_heartbeat_seconds": 30,
        "reconnect_delay_seconds": 5,
        "report_interval_seconds": 60,
        "lock_file": "data/collector.lock",
    },
    "cleanup": {"auto_start": False, "retention_days": 90, "interval_seconds": 3_600},
    "indicators": {"open_interest_change_length": 5},
    "server": {"host": "127.0.0.1", "port": 5_000},
}


def _merge(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    result = defaults.copy()
    for key, value in overrides.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return DEFAULT_CONFIG
    try:
        with CONFIG_PATH.open(encoding="utf-8") as config_file:
            loaded = json.load(config_file)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cannot load {CONFIG_PATH}: {error}") from error
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{CONFIG_PATH} must contain a JSON object")
    return _merge(DEFAULT_CONFIG, loaded)


def _project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


CONFIG = _load_config()
DB_PATH = _project_path(str(CONFIG["database"]["path"]))
BINANCE_REST_URL = str(CONFIG["binance"]["rest_url"]).rstrip("/")
BINANCE_WS_URL = str(CONFIG["binance"]["websocket_url"])
BINANCE_FUTURES_REST_URL = str(CONFIG["binance"]["futures_rest_url"]).rstrip("/")
BINANCE_DEPTH_WS_URL = str(CONFIG["binance"]["depth_websocket_url"])
COLLECTOR_AUTO_START = bool(CONFIG["collector"]["auto_start"])
QUOTE_ASSETS = {str(asset).strip().upper() for asset in CONFIG["collector"]["quote_assets"] if str(asset).strip()}
SYMBOLS = {str(symbol).strip().upper() for symbol in CONFIG["collector"]["symbols"] if str(symbol).strip()}
STREAMS_PER_CONNECTION = int(CONFIG["collector"]["streams_per_connection"])
WRITE_BATCH_SIZE = int(CONFIG["collector"]["write_batch_size"])
QUEUE_MAX_SIZE = int(CONFIG["collector"]["queue_max_size"])
WS_HEARTBEAT_SECONDS = int(CONFIG["collector"]["websocket_heartbeat_seconds"])
RECONNECT_DELAY_SECONDS = int(CONFIG["collector"]["reconnect_delay_seconds"])
REPORT_INTERVAL_SECONDS = int(CONFIG["collector"]["report_interval_seconds"])
COLLECTOR_LOCK_PATH = _project_path(str(CONFIG["collector"]["lock_file"]))
CLEANUP_AUTO_START = bool(CONFIG["cleanup"]["auto_start"])
RETENTION_DAYS = int(CONFIG["cleanup"]["retention_days"])
CLEANUP_INTERVAL_SECONDS = int(CONFIG["cleanup"]["interval_seconds"])
OI_CHANGE_LENGTH = int(CONFIG["indicators"]["open_interest_change_length"])
SERVER_HOST = str(CONFIG["server"]["host"])
SERVER_PORT = int(CONFIG["server"]["port"])