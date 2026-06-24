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
        close = _number(row.get("AdjC"))
        turnover = _number(row.get("Va"))
        if high is None or close is None:
            continue
        normalized.append({
            "date": row_date,
            "high": high,
            "close": close,
            "turnover": turnover or 0,
        })

    normalized.sort(key=lambda item: item["date"])
    if len(normalized) < 120:
        raise JQuantsError("価格履歴が120営業日未満です。")

    latest = normalized[-1]
    latest_date = latest["date"]
    lookback_start = date(latest_date.year - 2, latest_date.month, min(latest_date.day, 28))
    lookback = [item for item in normalized if item["date"] >= lookback_start]
    high_record = max(lookback, key=lambda item: item["high"])
    closes = [float(item["close"]) for item in normalized]
    latest_close = closes[-1]
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
        "previousHighDate": high_record["date"].isoformat(),
        "previousHigh": high_record["high"],
        "monthsFromHigh": _months_between(high_record["date"], latest_date),
        "averageTurnover20": round(average_turnover),
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
