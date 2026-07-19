from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any


TECHNICAL_VERSION = "nikkei225-technical-v1"
PER_MULTIPLIERS = tuple(range(12, 25))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def simple_moving_average(values: list[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    output: list[float | None] = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            output[index] = running / period
    return output


def exponential_moving_average(values: list[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    output: list[float | None] = [None] * len(values)
    if len(values) < period:
        return output
    seed_index = period - 1
    output[seed_index] = sum(values[:period]) / period
    multiplier = 2.0 / (period + 1)
    for index in range(seed_index + 1, len(values)):
        previous = output[index - 1]
        if previous is None:
            continue
        output[index] = (values[index] - previous) * multiplier + previous
    return output


def bollinger_bands(
    values: list[float],
    period: int = 20,
    sigma: float = 2.0,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    middle = simple_moving_average(values, period)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1:index + 1]
        average = middle[index]
        if average is None:
            continue
        variance = sum((value - average) ** 2 for value in window) / period
        deviation = math.sqrt(variance)
        upper[index] = average + sigma * deviation
        lower[index] = average - sigma * deviation
    return middle, upper, lower


def rsi_wilder(values: list[float], period: int = 14) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) <= period:
        return output
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    def value_for(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        relative_strength = gain / loss
        return 100.0 - (100.0 / (1.0 + relative_strength))

    output[period] = value_for(average_gain, average_loss)
    for index in range(period + 1, len(values)):
        change_index = index - 1
        average_gain = (average_gain * (period - 1) + gains[change_index]) / period
        average_loss = (average_loss * (period - 1) + losses[change_index]) / period
        output[index] = value_for(average_gain, average_loss)
    return output


def _ema_for_optional(values: list[float | None], period: int) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    valid_indexes = [index for index, value in enumerate(values) if value is not None]
    if len(valid_indexes) < period:
        return output
    seed_indexes = valid_indexes[:period]
    seed_index = seed_indexes[-1]
    output[seed_index] = sum(float(values[index]) for index in seed_indexes) / period
    multiplier = 2.0 / (period + 1)
    previous = output[seed_index]
    for index in valid_indexes[period:]:
        value = values[index]
        if value is None or previous is None:
            continue
        previous = (value - previous) * multiplier + previous
        output[index] = previous
    return output


def macd_series(
    values: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    fast_ema = exponential_moving_average(values, fast)
    slow_ema = exponential_moving_average(values, slow)
    macd: list[float | None] = [
        fast_value - slow_value
        if fast_value is not None and slow_value is not None
        else None
        for fast_value, slow_value in zip(fast_ema, slow_ema)
    ]
    signal_line = _ema_for_optional(macd, signal)
    histogram = [
        macd_value - signal_value
        if macd_value is not None and signal_value is not None
        else None
        for macd_value, signal_value in zip(macd, signal_line)
    ]
    return macd, signal_line, histogram


def parabolic_sar(
    rows: list[dict[str, Any]],
    *,
    step: float = 0.02,
    maximum: float = 0.20,
) -> list[float | None]:
    output: list[float | None] = [None] * len(rows)
    if len(rows) < 2:
        return output
    highs = [float(row["high"]) for row in rows]
    lows = [float(row["low"]) for row in rows]
    closes = [float(row["close"]) for row in rows]
    rising = closes[1] >= closes[0]
    sar = lows[0] if rising else highs[0]
    extreme = max(highs[:2]) if rising else min(lows[:2])
    acceleration = step
    output[0] = sar

    for index in range(1, len(rows)):
        sar = sar + acceleration * (extreme - sar)
        if rising:
            sar = min(sar, lows[index - 1])
            if index >= 2:
                sar = min(sar, lows[index - 2])
            if lows[index] < sar:
                rising = False
                sar = extreme
                extreme = lows[index]
                acceleration = step
            elif highs[index] > extreme:
                extreme = highs[index]
                acceleration = min(maximum, acceleration + step)
        else:
            sar = max(sar, highs[index - 1])
            if index >= 2:
                sar = max(sar, highs[index - 2])
            if highs[index] > sar:
                rising = True
                sar = extreme
                extreme = highs[index]
                acceleration = step
            elif lows[index] < extreme:
                extreme = lows[index]
                acceleration = min(maximum, acceleration + step)
        output[index] = sar
    return output


def _midpoint(rows: list[dict[str, Any]], index: int, period: int) -> float | None:
    if index < period - 1:
        return None
    window = rows[index - period + 1:index + 1]
    return (max(float(row["high"]) for row in window) + min(float(row["low"]) for row in window)) / 2


def ichimoku_series(
    rows: list[dict[str, Any]],
    *,
    tenkan_period: int = 9,
    kijun_period: int = 26,
    span_b_period: int = 52,
    displacement: int = 26,
) -> dict[str, list[float | None]]:
    length = len(rows)
    tenkan = [_midpoint(rows, index, tenkan_period) for index in range(length)]
    kijun = [_midpoint(rows, index, kijun_period) for index in range(length)]
    span_a: list[float | None] = [None] * length
    span_b: list[float | None] = [None] * length
    chikou: list[float | None] = [None] * length

    for index in range(length):
        target = index + displacement
        if target < length and tenkan[index] is not None and kijun[index] is not None:
            span_a[target] = (float(tenkan[index]) + float(kijun[index])) / 2
        midpoint_b = _midpoint(rows, index, span_b_period)
        if target < length and midpoint_b is not None:
            span_b[target] = midpoint_b
        chikou_target = index - displacement
        if chikou_target >= 0:
            chikou[chikou_target] = float(rows[index]["close"])

    return {
        "tenkan": tenkan,
        "kijun": kijun,
        "spanA": span_a,
        "spanB": span_b,
        "chikou": chikou,
    }


def normalize_ohlc_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        date_value = str(row.get("date") or "")
        values = {key: _number(row.get(key)) for key in ("open", "high", "low", "close")}
        if not date_value or any(value is None or value <= 0 for value in values.values()):
            continue
        normalized.append({"date": date_value, **values})
    by_date = {row["date"]: row for row in normalized}
    return [by_date[key] for key in sorted(by_date)]


def build_technical_rows(
    raw_rows: Iterable[dict[str, Any]],
    *,
    weighted_per_rows: Iterable[dict[str, Any]] = (),
    per_multipliers: tuple[int, ...] = PER_MULTIPLIERS,
) -> list[dict[str, Any]]:
    rows = normalize_ohlc_rows(raw_rows)
    closes = [float(row["close"]) for row in rows]
    ma5 = simple_moving_average(closes, 5)
    ma25 = simple_moving_average(closes, 25)
    ma75 = simple_moving_average(closes, 75)
    bb_middle, bb_upper, bb_lower = bollinger_bands(closes)
    rsi = rsi_wilder(closes)
    macd, macd_signal, macd_histogram = macd_series(closes)
    psar = parabolic_sar(rows)
    ichimoku = ichimoku_series(rows)
    per_by_date = {
        str(row.get("date") or ""): _number(row.get("weightedPer"))
        for row in weighted_per_rows
        if str(row.get("date") or "")
    }

    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        weighted_per = per_by_date.get(row["date"])
        implied_eps = float(row["close"]) / weighted_per if weighted_per and weighted_per > 0 else None
        per_bands = {
            str(multiplier): _round(implied_eps * multiplier, 2)
            for multiplier in per_multipliers
            if implied_eps is not None
        }
        output.append({
            **row,
            "ma5": _round(ma5[index]),
            "ma25": _round(ma25[index]),
            "ma75": _round(ma75[index]),
            "psar": _round(psar[index]),
            "bbMiddle": _round(bb_middle[index]),
            "bbUpper": _round(bb_upper[index]),
            "bbLower": _round(bb_lower[index]),
            "tenkan": _round(ichimoku["tenkan"][index]),
            "kijun": _round(ichimoku["kijun"][index]),
            "spanA": _round(ichimoku["spanA"][index]),
            "spanB": _round(ichimoku["spanB"][index]),
            "chikou": _round(ichimoku["chikou"][index]),
            "macd": _round(macd[index]),
            "macdSignal": _round(macd_signal[index]),
            "macdHistogram": _round(macd_histogram[index]),
            "rsi14": _round(rsi[index], 2),
            "weightedPer": _round(weighted_per, 2),
            "impliedEps": _round(implied_eps, 2),
            "perBands": per_bands,
        })
    return output


def indicator_parameters() -> dict[str, Any]:
    return {
        "movingAverages": [5, 25, 75],
        "parabolicSar": {"step": 0.02, "maximum": 0.20},
        "bollingerBands": {"period": 20, "sigma": 2, "deviation": "population"},
        "ichimoku": {
            "tenkan": 9,
            "kijun": 26,
            "spanB": 52,
            "displacement": 26,
            "chikouDisplayShiftOnly": True,
        },
        "macd": {"fast": 12, "slow": 26, "signal": 9},
        "rsi": {"period": 14, "method": "Wilder"},
        "perMultipliers": list(PER_MULTIPLIERS),
    }
