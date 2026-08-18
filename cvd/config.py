from __future__ import annotations

import os
from pathlib import Path

DB_PATH = Path(os.getenv("CVD_DB_PATH", "data/market.db"))
BINANCE_REST_URL = os.getenv("BINANCE_REST_URL", "https://data-api.binance.vision")
BINANCE_WS_URL = os.getenv("BINANCE_WS_URL", "wss://data-stream.binance.vision/ws")
QUOTE_ASSETS = {
    asset.strip().upper()
    for asset in os.getenv("CVD_QUOTE_ASSETS", "USDT").split(",")
    if asset.strip()
}
SYMBOLS = {
    symbol.strip().upper()
    for symbol in os.getenv("CVD_SYMBOLS", "").split(",")
    if symbol.strip()
}
RETENTION_DAYS = int(os.getenv("CVD_RETENTION_DAYS", "5"))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CVD_CLEANUP_INTERVAL_SECONDS", "3600"))