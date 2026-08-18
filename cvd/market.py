from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections import deque
from typing import Any

from cvd.config import BINANCE_FUTURES_REST_URL, BINANCE_REST_URL

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


def get_futures_json(path: str, params: dict[str, str | int] | None = None) -> Any:
    query = urllib.parse.urlencode(params or {})
    url = f"{BINANCE_FUTURES_REST_URL}{path}{'?' + query if query else ''}"
    request = urllib.request.Request(url, headers={"User-Agent": "CVD-Dashboard/1.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
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
    add_sma(candles, (30, 45, 60, 120))
    return candles


def fetch_open_interest_history(symbol: str, interval: str, limit: int = 500) -> list[dict[str, float | int]]:
    supported = {"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}
    period = interval if interval in supported else "5m"
    rows = get_futures_json(
        "/futures/data/openInterestHist",
        {"symbol": symbol.upper(), "period": period, "limit": min(limit, 500)},
    )
    return [
        {"time": int(row["timestamp"] // 1000), "openInterest": float(row["sumOpenInterest"])}
        for row in rows
    ]


def fetch_current_open_interest(symbol: str) -> dict[str, float | int]:
    row = get_futures_json("/fapi/v1/openInterest", {"symbol": symbol.upper()})
    return {"time": int(row["time"]), "openInterest": float(row["openInterest"])}


def align_open_interest(
    candles: list[dict[str, float | int]],
    open_interest: list[dict[str, float | int]],
) -> dict[int, float]:
    sorted_oi = sorted(open_interest, key=lambda row: int(row["time"]))
    result: dict[int, float] = {}
    oi_index = 0
    latest: float | None = None
    for candle in candles:
        candle_time = int(candle["time"])
        while oi_index < len(sorted_oi) and int(sorted_oi[oi_index]["time"]) <= candle_time:
            latest = float(sorted_oi[oi_index]["openInterest"])
            oi_index += 1
        if latest is not None:
            result[candle_time] = latest
    return result


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