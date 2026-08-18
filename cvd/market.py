from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections import deque
from typing import Any

from cvd.config import BINANCE_REST_URL

INTERVALS_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


def get_json(path: str, params: dict[str, str | int] | None = None) -> Any:
    query = urllib.parse.urlencode(params or {})
    url = f"{BINANCE_REST_URL}{path}{'?' + query if query else ''}"
    request = urllib.request.Request(url, headers={"User-Agent": "CVD-Dashboard/1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def fetch_symbols() -> list[dict[str, str]]:
    exchange_info = get_json("/api/v3/exchangeInfo")
    return [
        {
            "symbol": item["symbol"],
            "baseAsset": item["baseAsset"],
            "quoteAsset": item["quoteAsset"],
        }
        for item in exchange_info["symbols"]
        if item["status"] == "TRADING" and item.get("isSpotTradingAllowed", False)
    ]


def fetch_klines(symbol: str, interval: str, limit: int = 500) -> list[dict[str, float | int]]:
    if interval not in INTERVALS_MS:
        raise ValueError("Unsupported interval")
    raw_klines = get_json(
        "/api/v3/klines",
        {"symbol": symbol.upper(), "interval": interval, "limit": limit},
    )
    candles = [
        {
            "time": int(item[0] // 1000),
            "open": float(item[1]),
            "high": float(item[2]),
            "low": float(item[3]),
            "close": float(item[4]),
            "volume": float(item[5]),
        }
        for item in raw_klines
    ]
    add_sma(candles, (30, 45, 60))
    return candles


def add_sma(candles: list[dict[str, float | int]], periods: tuple[int, ...]) -> None:
    for period in periods:
        values: deque[float] = deque()
        running_total = 0.0
        key = f"sma{period}"
        for candle in candles:
            close = float(candle["close"])
            values.append(close)
            running_total += close
            if len(values) > period:
                running_total -= values.popleft()
            if len(values) == period:
                candle[key] = running_total / period