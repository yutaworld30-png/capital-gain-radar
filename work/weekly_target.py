from __future__ import annotations

from typing import Any


WEEKLY_TARGET_VERSION = "weekly-5pct-v1.0"
WEEKLY_TARGET_RETURN = 0.05
WEEKLY_STOP_LIMIT = 0.025
LOT_SIZE = 100
PROFILE_CONFIGS: dict[str, dict[str, int | str]] = {
    "single": {
        "label": "1銘柄運用",
        "positions": 1,
        "allocationYen": 180_000,
        "maxPrice": 1_800,
    },
    "double": {
        "label": "2銘柄運用",
        "positions": 2,
        "allocationYen": 90_000,
        "maxPrice": 900,
    },
}


def weekly_target_policy() -> dict[str, object]:
    return {
        "version": WEEKLY_TARGET_VERSION,
        "capitalYen": 200_000,
        "targetReturn": WEEKLY_TARGET_RETURN,
        "stopLimit": WEEKLY_STOP_LIMIT,
        "lotSize": LOT_SIZE,
        "profiles": PROFILE_CONFIGS,
        "qualification": {
            "overallScore": 75,
            "dataQuality": 85,
            "highDistance": 0.03,
            "relativeStrength": 80,
            "atrPercentMin": 0.025,
            "atrPercentMax": 0.055,
            "volumeRatio": 1.5,
            "averageTurnoverYen": 1_000_000_000,
            "salesGrowth": 0.10,
            "profitGrowth": 0.20,
            "roe": 0.10,
            "marginRatio": 3.0,
            "rsiMin": 55,
            "rsiMax": 70,
            "earningsBlackoutDays": 3,
        },
        "note": (
            "週5%の値動きを保証する判定ではありません。"
            "条件一致がない場合は売買を見送るための補助指標です。"
        ),
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _average(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains = 0.0
    losses = 0.0
    for index in range(len(closes) - period, len(closes)):
        change = closes[index] - closes[index - 1]
        if change >= 0:
            gains += change
        else:
            losses += abs(change)
    if losses == 0:
        return 100.0
    strength = gains / losses
    return 100 - (100 / (1 + strength))


def _chart_metrics(price: dict[str, object]) -> dict[str, float | bool | None]:
    raw_rows = price.get("chartHistory")
    rows = [row for row in raw_rows if isinstance(row, dict)] if isinstance(raw_rows, list) else []
    rows.sort(key=lambda row: str(row.get("date") or ""))
    closes = [
        float(row["close"])
        for row in rows
        if isinstance(row.get("close"), (int, float)) and float(row["close"]) > 0
    ]
    volumes = [
        float(row.get("volume") or 0)
        for row in rows
        if isinstance(row.get("close"), (int, float)) and float(row["close"]) > 0
    ]
    ma5 = _average(closes, 5)
    ma25 = _average(closes, 25)
    ma75 = _average(closes, 75)
    prior_ma25 = (
        sum(closes[-30:-5]) / 25
        if len(closes) >= 30
        else None
    )
    latest_close = closes[-1] if closes else None
    trend_strong = bool(
        latest_close is not None
        and ma5 is not None
        and ma25 is not None
        and ma75 is not None
        and prior_ma25 is not None
        and latest_close > ma5 > ma25 > ma75
        and ma25 > prior_ma25
    )
    trend_partial = bool(
        latest_close is not None
        and ma25 is not None
        and ma75 is not None
        and latest_close > ma25 > ma75
    )
    previous_volumes = volumes[-21:-1]
    average_volume20 = (
        sum(previous_volumes) / len(previous_volumes)
        if len(previous_volumes) == 20 and any(previous_volumes)
        else None
    )
    volume_ratio = (
        volumes[-1] / average_volume20
        if volumes and average_volume20 and average_volume20 > 0
        else None
    )
    return {
        "ma5": round(ma5, 4) if ma5 is not None else None,
        "ma25": round(ma25, 4) if ma25 is not None else None,
        "ma75": round(ma75, 4) if ma75 is not None else None,
        "ma25Rising": bool(ma25 is not None and prior_ma25 is not None and ma25 > prior_ma25),
        "trendStrong": trend_strong,
        "trendPartial": trend_partial,
        "rsi14": round(value, 2) if (value := _rsi(closes)) is not None else None,
        "volumeRatio20": round(volume_ratio, 3) if volume_ratio is not None else None,
    }


def _earned(points: int, ratio: float) -> int:
    return round(points * ratio)


def _profile_result(
    row: dict[str, object],
    metrics: dict[str, object],
    profile_key: str,
) -> dict[str, object]:
    config = PROFILE_CONFIGS[profile_key]
    close = _number(row.get("latestClose"))
    total_score = _number(row.get("score"))
    quality = _number(row.get("dataQuality"))
    relative = _number(row.get("relative"))
    atr14 = _number(row.get("atr14"))
    turnover = _number(row.get("averageTurnover20"))
    high_distance = _number(row.get("high52wDistance"))
    margin = _number(row.get("margin"))
    sales_growth = _number(row.get("salesGrowth"))
    profit_growth = _number(row.get("profitGrowth"))
    roe = _number(row.get("roe"))
    stop_width = _number(row.get("suggestedStopWidth"))
    rsi14 = _number(metrics.get("rsi14"))
    volume_ratio = _number(metrics.get("volumeRatio20"))
    atr_percent = atr14 / close if atr14 is not None and close and close > 0 else None
    max_price = int(config["maxPrice"])
    estimated_cost = close * LOT_SIZE if close is not None else None
    budget_fit = bool(close is not None and close <= max_price)
    near_high = bool(row.get("isNewHigh52w") is True or (high_distance is not None and high_distance <= 0.03))
    relative_ok = bool(relative is not None and relative >= 80)
    rsi_ok = bool(rsi14 is not None and 55 <= rsi14 <= 70)
    atr_ok = bool(atr_percent is not None and 0.025 <= atr_percent <= 0.055)
    volume_ok = bool(volume_ratio is not None and volume_ratio >= 1.5)
    liquidity_ok = bool(turnover is not None and turnover >= 1_000_000_000)
    fundamentals = [
        sales_growth is not None and sales_growth >= 0.10,
        profit_growth is not None and profit_growth >= 0.20,
        roe is not None and roe >= 0.10,
    ]
    fundamentals_ok = all(fundamentals)
    supply_ok = bool(margin is not None and margin <= 3)
    overall_ok = bool(total_score is not None and total_score >= 75)
    quality_ok = bool(quality is not None and quality >= 85)
    trend_ok = metrics.get("trendStrong") is True
    stop_ok = bool(stop_width is not None and stop_width <= WEEKLY_STOP_LIMIT)
    events = row.get("events") if isinstance(row.get("events"), list) else []
    earnings_risk = any(
        isinstance(event, dict)
        and event.get("type") == "earnings"
        and isinstance(event.get("daysFromNow"), int)
        and 0 <= int(event["daysFromNow"]) <= 3
        for event in events
    )

    components = [
        {"id": "budget", "label": "投資予算", "points": 5, "earned": 5 if budget_fit else 0, "passed": budget_fit},
        {"id": "overall", "label": "総合スコア", "points": 10, "earned": 10 if overall_ok else _earned(10, 0.6 if total_score is not None and total_score >= 65 else 0.3 if total_score is not None and total_score >= 55 else 0), "passed": overall_ok},
        {"id": "quality", "label": "データ信頼度", "points": 10, "earned": 10 if quality_ok else 5 if quality is not None and quality >= 70 else 0, "passed": quality_ok},
        {"id": "trend", "label": "上昇トレンド", "points": 15, "earned": 15 if trend_ok else 8 if metrics.get("trendPartial") is True else 0, "passed": trend_ok},
        {"id": "breakout", "label": "高値接近", "points": 10, "earned": 10 if near_high else 5 if high_distance is not None and high_distance <= 0.05 else 0, "passed": near_high},
        {"id": "momentum", "label": "相対強度・RSI", "points": 10, "earned": (5 if relative_ok else 3 if relative is not None and relative >= 65 else 0) + (5 if rsi_ok else 2 if rsi14 is not None and 45 <= rsi14 <= 75 else 0), "passed": relative_ok and rsi_ok},
        {"id": "atr", "label": "値幅適性", "points": 10, "earned": 10 if atr_ok else 5 if atr_percent is not None and 0.018 <= atr_percent <= 0.07 else 0, "passed": atr_ok},
        {"id": "volume", "label": "出来高増加", "points": 5, "earned": 5 if volume_ok else 2 if volume_ratio is not None and volume_ratio >= 1 else 0, "passed": volume_ok},
        {"id": "liquidity", "label": "流動性", "points": 10, "earned": 10 if liquidity_ok else 5 if turnover is not None and turnover >= 500_000_000 else 0, "passed": liquidity_ok},
        {"id": "fundamentals", "label": "業績・ROE", "points": 10, "earned": round(sum(fundamentals) / 3 * 10), "passed": fundamentals_ok},
        {"id": "supply", "label": "信用需給", "points": 5, "earned": 5 if supply_ok else 2 if margin is not None and margin <= 5 else 0, "passed": supply_ok},
    ]
    opportunity_score = max(0, min(100, sum(int(item["earned"]) for item in components)))
    strict_checks = {
        "budget": budget_fit,
        "overallScore": overall_ok,
        "dataQuality": quality_ok,
        "trend": trend_ok,
        "nearHigh": near_high,
        "relativeStrength": relative_ok,
        "rsi": rsi_ok,
        "atr": atr_ok,
        "volume": volume_ok,
        "liquidity": liquidity_ok,
        "fundamentals": fundamentals_ok,
        "supply": supply_ok,
        "stopWidth": stop_ok,
        "earningsSafe": not earnings_risk,
    }
    blockers: list[str] = []
    blocker_messages = {
        "budget": f"株価が{max_price:,}円以下の予算条件に未達",
        "overallScore": "総合スコア75点未満",
        "dataQuality": "データ信頼度85点未満",
        "trend": "株価＞5日線＞25日線＞75日線の上昇形未達",
        "nearHigh": "52週高値まで3%以内ではない",
        "relativeStrength": "相対強度が上位20%目安に未達",
        "rsi": "RSIが55〜70の範囲外",
        "atr": "ATR率が2.5〜5.5%の範囲外",
        "volume": "出来高が20日平均の1.5倍未満",
        "liquidity": "20日平均売買代金10億円未満",
        "fundamentals": "売上10%・利益20%・ROE10%のいずれか未達",
        "supply": "信用倍率3倍超",
        "stopWidth": "参考損切り幅が2.5%超または未取得",
        "earningsSafe": "3営業日以内に決算発表予定",
    }
    for key, passed in strict_checks.items():
        if not passed:
            blockers.append(blocker_messages[key])
    strict_match = all(strict_checks.values())
    if strict_match:
        status = "qualified"
    elif budget_fit and quality is not None and quality >= 70 and opportunity_score >= 50 and not earnings_risk:
        status = "watch"
    else:
        status = "not-eligible"
    positives = [
        f"{item['label']} {item['earned']}/{item['points']}点"
        for item in sorted(components, key=lambda item: (-int(item["earned"]), -int(item["points"])))
        if item["passed"]
    ][:3]
    return {
        "label": config["label"],
        "maxPrice": max_price,
        "budgetFit": budget_fit,
        "score": opportunity_score,
        "status": status,
        "strictMatch": strict_match,
        "components": components,
        "positives": positives,
        "blockers": blockers[:5],
        "estimatedCostYen": round(estimated_cost) if estimated_cost is not None else None,
        "targetProfitYen": round(estimated_cost * WEEKLY_TARGET_RETURN) if estimated_cost is not None else None,
        "stopLossYen": round(estimated_cost * WEEKLY_STOP_LIMIT) if estimated_cost is not None else None,
    }


def build_weekly_target(
    row: dict[str, object],
    price: dict[str, object] | None,
) -> dict[str, object]:
    metrics = _chart_metrics(price or {})
    close = _number(row.get("latestClose"))
    atr14 = _number(row.get("atr14"))
    metrics.update({
        "atrPercent": round(atr14 / close, 6) if atr14 is not None and close and close > 0 else None,
        "high52wDistance": _number(row.get("high52wDistance")),
        "averageTurnover20": _number(row.get("averageTurnover20")),
        "relativeStrength": _number(row.get("relative")),
        "marginRatio": _number(row.get("margin")),
        "suggestedStopWidth": _number(row.get("suggestedStopWidth")),
    })
    return {
        "version": WEEKLY_TARGET_VERSION,
        "targetReturn": WEEKLY_TARGET_RETURN,
        "stopLimit": WEEKLY_STOP_LIMIT,
        "metrics": metrics,
        "profiles": {
            key: _profile_result(row, metrics, key)
            for key in PROFILE_CONFIGS
        },
    }


def attach_weekly_targets(dataset: dict[str, object]) -> None:
    prices = dataset.get("nikkei225Prices")
    price_map = {
        str(item.get("code")): item
        for item in prices
        if isinstance(item, dict) and item.get("code")
    } if isinstance(prices, list) else {}
    weekly_by_code: dict[str, dict[str, object]] = {}
    for key in ("searchUniverse", "candidates"):
        rows = dataset.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "")
            if not code:
                continue
            weekly = weekly_by_code.get(code)
            if weekly is None:
                weekly = build_weekly_target(row, price_map.get(code))
                weekly_by_code[code] = weekly
            row["weeklyTarget"] = weekly
    dataset["weeklyTargetPolicy"] = weekly_target_policy()
