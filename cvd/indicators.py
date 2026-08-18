from __future__ import annotations

import math
from collections.abc import Sequence


def rolling_zscore(values: Sequence[float | None], length: int = 100) -> list[float | None]:
    result: list[float | None] = []
    for index, value in enumerate(values):
        if value is None or index + 1 < length:
            result.append(None)
            continue
        window = values[index - length + 1 : index + 1]
        if any(item is None for item in window):
            result.append(None)
            continue
        numeric = [float(item) for item in window if item is not None]
        mean = sum(numeric) / length
        variance = sum((item - mean) ** 2 for item in numeric) / length
        std = math.sqrt(variance)
        result.append(0.0 if std == 0 else (float(value) - mean) / std)
    return result


def enrich_indicators(
    candles: list[dict[str, float | int]],
    deltas_by_time: dict[int, float],
    open_interest_by_time: dict[int, float],
    oi_change_length: int = 5,
) -> list[dict[str, float | int | bool | str | None]]:
    rows: list[dict[str, float | int | bool | str | None]] = []
    cumulative_volume = 0.0
    cumulative_notional = 0.0
    cumulative_delta = 0.0
    for candle in candles:
        timestamp = int(candle["time"])
        delta = float(deltas_by_time.get(timestamp, 0.0))
        cumulative_delta += delta
        volume = float(candle["volume"])
        typical_price = (float(candle["high"]) + float(candle["low"]) + float(candle["close"])) / 3
        cumulative_volume += volume
        cumulative_notional += typical_price * volume
        high_low = float(candle["high"]) - float(candle["low"])
        rows.append(
            {
                **candle,
                "delta": delta,
                "cvd": cumulative_delta,
                "closePosition": 0.5 if high_low == 0 else (float(candle["close"]) - float(candle["low"])) / high_low,
                "openInterest": open_interest_by_time.get(timestamp),
                "vwap": None if cumulative_volume == 0 else cumulative_notional / cumulative_volume,
            }
        )

    delta_z = rolling_zscore([float(row["delta"]) for row in rows])
    oi_changes: list[float | None] = []
    for index, row in enumerate(rows):
        current = row["openInterest"]
        previous = rows[index - oi_change_length]["openInterest"] if index >= oi_change_length else None
        if current is None or previous in (None, 0):
            oi_changes.append(None)
        else:
            oi_changes.append((float(current) - float(previous)) / float(previous))
    oi_z = rolling_zscore(oi_changes)

    for index, row in enumerate(rows):
        row["deltaZ"] = delta_z[index]
        row["oiChange"] = oi_changes[index]
        row["oiChangeZ"] = oi_z[index]
        row["bullishAbsorption"] = delta_z[index] is not None and delta_z[index] < -2 and float(row["closePosition"]) > 0.55
        row["bearishAbsorption"] = delta_z[index] is not None and delta_z[index] > 2 and float(row["closePosition"]) < 0.45
        previous_close = float(rows[index - oi_change_length]["close"]) if index >= oi_change_length else None
        price_up_oi_up = False
        price_up_oi_down = False
        price_down_oi_up = False
        price_down_oi_down = False
        if previous_close is not None and oi_changes[index] is not None:
            price_up_oi_up = float(row["close"]) > previous_close and float(oi_changes[index]) > 0
            price_up_oi_down = float(row["close"]) > previous_close and float(oi_changes[index]) < 0
            price_down_oi_up = float(row["close"]) < previous_close and float(oi_changes[index]) > 0
            price_down_oi_down = float(row["close"]) < previous_close and float(oi_changes[index]) < 0
        context = next(
            (name for name, active in (
                ("price_up_oi_up", price_up_oi_up),
                ("price_up_oi_down", price_up_oi_down),
                ("price_down_oi_up", price_down_oi_up),
                ("price_down_oi_down", price_down_oi_down),
            ) if active),
            "unavailable",
        )
        row["price_up_oi_up"] = price_up_oi_up
        row["price_up_oi_down"] = price_up_oi_down
        row["price_down_oi_up"] = price_down_oi_up
        row["price_down_oi_down"] = price_down_oi_down
        row["oiContext"] = context
        cvd_slope = float(row["cvd"]) - float(rows[index - 1]["cvd"]) if index else 0.0
        row["longTrend"] = bool(
            row["vwap"] is not None
            and float(row["close"]) > float(row["vwap"])
            and cvd_slope > 0
            and delta_z[index] is not None
            and delta_z[index] > 1
            and oi_changes[index] is not None
            and oi_changes[index] >= 0
        )
        row["shortTrend"] = bool(
            row["vwap"] is not None
            and float(row["close"]) < float(row["vwap"])
            and cvd_slope < 0
            and delta_z[index] is not None
            and delta_z[index] < -1
            and oi_changes[index] is not None
            and oi_changes[index] >= 0
        )
    return rows