from __future__ import annotations

import json
import math
from datetime import date, datetime
from statistics import pstdev
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://api.jquants.com/v2"
PRICE_DOCS_URL = "https://jpx-jquants.com/ja/spec/eq-bars-daily"
FINANCIAL_DOCS_URL = "https://jpx-jquants.com/ja/spec/fin-summary"


class JQuantsError(RuntimeError):
    pass


def fetch_all(endpoint: str, params: dict[str, str], api_key: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    pagination_key = ""
    for _ in range(100):
        query = dict(params)
        if pagination_key:
            query["pagination_key"] = pagination_key
        url = f"{BASE_URL}{endpoint}?{urlencode(query)}"
        request = Request(
            url,
            headers={
                "User-Agent": "CapitalGainRadar/0.2",
                "x-api-key": api_key,
            },
        )
        try:
            with urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise JQuantsError(f"J-Quants API HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise JQuantsError("J-Quants APIの取得または解析に失敗しました。") from error

        page_rows = payload.get("data")
        if not isinstance(page_rows, list):
            raise JQuantsError("J-Quants APIのレスポンスにdata配列がありません。")
        rows.extend(item for item in page_rows if isinstance(item, dict))
        pagination_key = str(payload.get("pagination_key") or "")
        if not pagination_key:
            return rows
    raise JQuantsError("J-Quants APIのページ数が上限を超えました。")


def _number(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _months_between(start: date, end: date) -> float:
    days = max(0, (end - start).days)
    return round(days / 30.4375, 1)


def _liquidity_score(average_turnover: float) -> int:
    thresholds = [
        (10_000_000_000, 95),
        (5_000_000_000, 88),
        (2_000_000_000, 80),
        (1_000_000_000, 72),
        (500_000_000, 64),
        (200_000_000, 52),
        (100_000_000, 42),
    ]
    for threshold, score in thresholds:
        if average_turnover >= threshold:
            return score
    return 30


def calculate_price_metrics(rows: list[dict[str, object]]) -> dict[str, object]:
    normalized: list[dict[str, object]] = []
    for row in rows:
        try:
            row_date = datetime.strptime(str(row.get("Date")), "%Y-%m-%d").date()
        except ValueError:
            continue
        high = _number(row.get("AdjH"))
        low = _number(row.get("AdjL"))
        close = _number(row.get("AdjC"))
        turnover = _number(row.get("Va"))
        if high is None or close is None:
            continue
        normalized.append({
            "date": row_date,
            "high": high,
            "low": low,
            "close": close,
            "turnover": turnover or 0,
        })

    normalized.sort(key=lambda item: item["date"])
    high_lookback_days = 252
    if len(normalized) < high_lookback_days:
        raise JQuantsError(f"価格履歴が{high_lookback_days}営業日未満です。")

    latest = normalized[-1]
    latest_date = latest["date"]
    lookback = normalized[-high_lookback_days:]
    high_record = max(lookback, key=lambda item: item["high"])
    prior_high_record = max(lookback[:-1], key=lambda item: item["high"])
    closes = [float(item["close"]) for item in normalized]
    latest_close = closes[-1]
    latest_high = float(latest["high"])
    high_52w = float(high_record["high"])
    ma20 = sum(closes[-20:]) / 20
    ma50 = sum(closes[-50:]) / 50
    ma200 = sum(closes[-200:]) / min(200, len(closes))
    return20 = latest_close / closes[-21] - 1 if len(closes) >= 21 else 0

    technical = 50
    technical += 15 if latest_close > ma50 else -15
    technical += 15 if ma50 > ma200 else -10
    technical += 10 if latest_close >= float(high_record["high"]) * 0.9 else 0
    technical += 10 if return20 > 0 else -5
    technical = max(0, min(100, technical))

    average_turnover = sum(float(item["turnover"]) for item in normalized[-20:]) / 20
    true_ranges = []
    for index in range(max(1, len(normalized) - 14), len(normalized)):
        high = normalized[index].get("high")
        low = normalized[index].get("low")
        previous_close = normalized[index - 1].get("close")
        if not all(isinstance(value, (int, float)) for value in (high, low, previous_close)):
            continue
        true_ranges.append(max(
            float(high) - float(low),
            abs(float(high) - float(previous_close)),
            abs(float(low) - float(previous_close)),
        ))
    atr14 = sum(true_ranges) / 14 if len(true_ranges) == 14 else None
    recent_lows = [
        float(item["low"])
        for item in normalized[-20:]
        if isinstance(item.get("low"), (int, float))
    ]
    recent_low20 = min(recent_lows) if len(recent_lows) == 20 else None
    suggested_stop = None
    suggested_stop_width = None
    if atr14 is not None and recent_low20 is not None and latest_close > 0:
        suggested_stop = min(latest_close, max(recent_low20, latest_close - atr14 * 2))
        suggested_stop_width = max(0.0, (latest_close - suggested_stop) / latest_close)
    if average_turnover >= 5_000_000_000:
        execution_ease = "高い"
    elif average_turnover >= 1_000_000_000:
        execution_ease = "標準"
    elif average_turnover >= 200_000_000:
        execution_ease = "やや低い"
    else:
        execution_ease = "低い"
    returns = [
        math.log(closes[index] / closes[index - 1])
        for index in range(max(1, len(closes) - 60), len(closes))
        if closes[index] > 0 and closes[index - 1] > 0
    ]
    annualized_volatility = pstdev(returns) * math.sqrt(252) if len(returns) >= 20 else 1
    risk_score = max(0, min(100, round(annualized_volatility * 200)))

    return {
        "asOf": latest_date.isoformat(),
        "latestClose": latest_close,
        "latestHigh": latest_high,
        "high52w": high_52w,
        "isNewHigh52w": latest_high >= float(prior_high_record["high"]),
        "priorHigh52w": float(prior_high_record["high"]),
        "priorHigh52wDate": prior_high_record["date"].isoformat(),
        "previousHighDate": high_record["date"].isoformat(),
        "previousHigh": high_52w,
        "monthsFromHigh": _months_between(high_record["date"], latest_date),
        "priceBasis": "adjusted-ohlc",
        "highLookbackDays": high_lookback_days,
        "averageTurnover20": round(average_turnover),
        "atr14": round(atr14, 4) if atr14 is not None else None,
        "recentLow20": round(recent_low20, 4) if recent_low20 is not None else None,
        "suggestedStopPrice": round(suggested_stop, 4) if suggested_stop is not None else None,
        "suggestedStopWidth": round(suggested_stop_width, 4) if suggested_stop_width is not None else None,
        "suggestedStopBasis": "20営業日安値と終値-2ATRの高い方（参考値）" if suggested_stop is not None else None,
        "executionEase": execution_ease,
        "onePercentTurnoverYen": round(average_turnover * 0.01),
        "liquidity": _liquidity_score(average_turnover),
        "technical": technical,
        "risk": risk_score,
        "annualizedVolatility": round(annualized_volatility, 4),
        "return20": round(return20, 4),
    }


def calculate_financial_metrics(rows: list[dict[str, object]]) -> dict[str, object]:
    if not rows:
        raise JQuantsError("財務情報がありません。")
    ordered = sorted(
        rows,
        key=lambda item: (
            str(item.get("DiscDate") or ""),
            str(item.get("DiscTime") or ""),
            str(item.get("DiscNo") or ""),
        ),
    )
    latest = ordered[-1]
    latest_forecast_op = _number(latest.get("FOP"))
    same_year_prior = [
        item for item in ordered[:-1]
        if item.get("CurFYEn") == latest.get("CurFYEn") and _number(item.get("FOP")) is not None
    ]
    prior_forecast_op = _number(same_year_prior[-1].get("FOP")) if same_year_prior else None
    revision_rate = (
        latest_forecast_op / prior_forecast_op - 1
        if latest_forecast_op is not None and prior_forecast_op not in (None, 0)
        else 0
    )

    fiscal_results = [
        item for item in ordered
        if item.get("CurPerType") == "FY" and _number(item.get("OP")) is not None
    ]
    actual_growth = 0.0
    if len(fiscal_results) >= 2:
        previous_op = _number(fiscal_results[-2].get("OP"))
        current_op = _number(fiscal_results[-1].get("OP"))
        if previous_op not in (None, 0) and current_op is not None:
            actual_growth = current_op / previous_op - 1

    earnings_score = 50
    earnings_score += max(-25, min(25, round(revision_rate * 200)))
    earnings_score += max(-20, min(20, round(actual_growth * 100)))
    earnings_score = max(0, min(100, earnings_score))

    return {
        "asOf": str(latest.get("DiscDate") or ""),
        "disclosureNumber": str(latest.get("DiscNo") or ""),
        "documentType": str(latest.get("DocType") or ""),
        "forecastOperatingProfit": latest_forecast_op,
        "forecastRevisionRate": round(revision_rate, 4),
        "actualOperatingProfitGrowth": round(actual_growth, 4),
        "earnings": earnings_score,
    }


def fetch_price_metrics(code: str, api_key: str, from_date: str, to_date: str) -> dict[str, object]:
    rows = fetch_all(
        "/equities/bars/daily",
        {"code": code, "from": from_date, "to": to_date},
        api_key,
    )
    return calculate_price_metrics(rows)


def fetch_financial_metrics(code: str, api_key: str) -> dict[str, object]:
    rows = fetch_all("/fins/summary", {"code": code}, api_key)
    return calculate_financial_metrics(rows)
