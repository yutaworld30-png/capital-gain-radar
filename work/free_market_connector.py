from __future__ import annotations

import csv
import html
import json
import re
from datetime import date, datetime, time, timezone
from io import StringIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from jquants_connector import JQuantsError, calculate_price_metrics


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
YAHOO_MIRROR_URL = "https://query2.finance.yahoo.com/v8/finance/chart"
YAHOO_SPARK_URL = "https://query1.finance.yahoo.com/v7/finance/spark"
YAHOO_SPARK_MIRROR_URL = "https://query2.finance.yahoo.com/v7/finance/spark"
STOOQ_DAILY_URL = "https://stooq.com/q/d/l/"
YAHOO_INFO_URL = "https://finance.yahoo.co.jp/quote"
STOOQ_INFO_URL = "https://stooq.com"
GOOGLE_FINANCE_URL = "https://www.google.com/finance/quote"


class FreeMarketDataError(RuntimeError):
    pass


def _fetch(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 CapitalGainRadar/0.3",
            "Accept": "application/json,text/csv,text/plain,*/*",
        },
    )
    try:
        with urlopen(request, timeout=45) as response:
            return response.read()
    except HTTPError as error:
        raise FreeMarketDataError(f"HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise FreeMarketDataError("無料株価データの取得に失敗しました。") from error


def _unix_seconds(value: date) -> int:
    point = datetime.combine(value, time.min, tzinfo=timezone.utc)
    return int(point.timestamp())


def fetch_yahoo_history(
    code: str,
    start: date,
    end: date,
    base_url: str = YAHOO_CHART_URL,
) -> tuple[list[dict[str, object]], str]:
    symbol = f"{code}.T"
    query = urlencode({
        "period1": _unix_seconds(start),
        "period2": _unix_seconds(end) + 86400,
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    })
    url = f"{base_url}/{symbol}?{query}"
    try:
        payload = json.loads(_fetch(url).decode("utf-8"))
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        adjusted = result["indicators"].get("adjclose", [{}])[0].get("adjclose", [])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise FreeMarketDataError("Yahoo Financeの株価データを解析できません。") from error

    rows: list[dict[str, object]] = []
    for index, timestamp in enumerate(timestamps):
        open_price = quote.get("open", [])[index]
        close = quote.get("close", [])[index]
        high = quote.get("high", [])[index]
        low = quote.get("low", [])[index]
        volume = quote.get("volume", [])[index]
        if close in (None, 0) or open_price is None or high is None or low is None:
            continue
        adjusted_close = adjusted[index] if index < len(adjusted) and adjusted[index] is not None else close
        factor = float(adjusted_close) / float(close)
        rows.append({
            "Date": datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(),
            "O": float(open_price),
            "H": float(high),
            "L": float(low),
            "C": float(close),
            "V": float(volume or 0),
            "AdjC": float(adjusted_close),
            "AdjH": float(high) * factor,
            "Va": float(close) * float(volume or 0),
        })
    return rows, url


def fetch_yahoo_mirror_latest(codes: list[str]) -> dict[str, dict[str, object]]:
    symbols = [f"{code}.T" for code in codes]
    query = urlencode({
        "symbols": ",".join(symbols),
        "range": "1mo",
        "interval": "1d",
    })
    url = f"{YAHOO_SPARK_MIRROR_URL}?{query}"
    try:
        payload = json.loads(_fetch(url).decode("utf-8"))
        results = payload["spark"]["result"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise FreeMarketDataError("Yahoo Financeの一括照合データを解析できません。") from error

    validated: dict[str, dict[str, object]] = {}
    for item in results:
        try:
            code = str(item["symbol"]).removesuffix(".T")
            response = item["response"][0]
            timestamps = response["timestamp"]
            closes = response["indicators"]["quote"][0]["close"]
            latest_index = next(
                index for index in range(len(closes) - 1, -1, -1)
                if closes[index] not in (None, 0)
            )
            validated[code] = {
                "date": datetime.fromtimestamp(timestamps[latest_index], tz=timezone.utc).date().isoformat(),
                "close": float(closes[latest_index]),
                "url": url,
            }
        except (KeyError, IndexError, StopIteration, TypeError, ValueError):
            continue
    return validated


def fetch_yahoo_spark_histories(
    codes: list[str],
    base_url: str = YAHOO_SPARK_URL,
) -> dict[str, dict[str, object]]:
    symbols = [f"{code}.T" for code in codes]
    query = urlencode({
        "symbols": ",".join(symbols),
        "range": "2y",
        "interval": "1d",
    })
    url = f"{base_url}?{query}"
    try:
        payload = json.loads(_fetch(url).decode("utf-8"))
        results = payload["spark"]["result"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise FreeMarketDataError("Yahoo Financeの一括価格履歴を解析できません。") from error

    histories: dict[str, dict[str, object]] = {}
    for item in results:
        try:
            code = str(item["symbol"]).removesuffix(".T")
            response = item["response"][0]
            timestamps = response["timestamp"]
            closes = response["indicators"]["quote"][0]["close"]
            meta = response.get("meta", {})
            latest_volume = float(meta.get("regularMarketVolume") or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            continue

        rows: list[dict[str, object]] = []
        for index, timestamp in enumerate(timestamps):
            close = closes[index] if index < len(closes) else None
            if close in (None, 0):
                continue
            close_value = float(close)
            rows.append({
                "Date": datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(),
                "C": close_value,
                "AdjC": close_value,
                "AdjH": close_value,
                "Va": close_value * latest_volume,
            })
        if rows:
            histories[code] = {"rows": rows, "url": url}
    return histories


def fetch_price_metrics_with_mirror(
    code: str,
    start: date,
    end: date,
    mirror: dict[str, object],
) -> dict[str, object]:
    yahoo_rows, yahoo_url = fetch_yahoo_history(code, start, end)
    if len(yahoo_rows) < 120:
        raise FreeMarketDataError("Yahoo Financeの価格履歴が120営業日未満です。")

    validation_date = str(mirror.get("date") or "")
    yahoo_by_date = {str(item["Date"]): item for item in yahoo_rows}
    yahoo_match = yahoo_by_date.get(validation_date)
    if not yahoo_match:
        raise FreeMarketDataError("価格履歴と照合経路の最新取引日が一致しません。")
    yahoo_close = float(yahoo_match["C"])
    mirror_close = float(mirror.get("close") or 0)
    close_difference = abs(yahoo_close - mirror_close) / max(yahoo_close, mirror_close)
    if close_difference > 0.001:
        raise FreeMarketDataError(f"終値の差が許容範囲を超えています（{close_difference:.2%}）。")

    try:
        metrics = calculate_price_metrics(yahoo_rows)
    except JQuantsError as error:
        raise FreeMarketDataError(str(error)) from error
    latest_date = datetime.strptime(str(yahoo_rows[-1]["Date"]), "%Y-%m-%d").date()
    if (end - latest_date).days > 7:
        raise FreeMarketDataError("最新株価が7日以上更新されていません。")

    mirror_query = urlencode({
        "period1": _unix_seconds(start),
        "period2": _unix_seconds(end) + 86400,
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    })
    return {
        **metrics,
        "validationDate": validation_date,
        "validationCloseYahoo": yahoo_close,
        "validationCloseMirror": mirror_close,
        "validationDifference": round(close_difference, 6),
        "sources": {
            "priceHistory": {
                "url": yahoo_url,
                "updatedAt": str(metrics["asOf"]),
                "provider": "Yahoo Finance",
            },
            "priceValidation": {
                "url": f"{YAHOO_MIRROR_URL}/{code}.T?{mirror_query}",
                "updatedAt": validation_date,
                "provider": "Yahoo Finance mirror",
            },
        },
    }


def fetch_stooq_history(code: str, start: date, end: date) -> tuple[list[dict[str, object]], str]:
    query = urlencode({
        "s": f"{code.lower()}.jp",
        "d1": start.strftime("%Y%m%d"),
        "d2": end.strftime("%Y%m%d"),
        "i": "d",
    })
    url = f"{STOOQ_DAILY_URL}?{query}"
    text = _fetch(url).decode("utf-8", errors="replace")
    rows: list[dict[str, object]] = []
    for row in csv.DictReader(StringIO(text)):
        try:
            rows.append({
                "Date": row["Date"],
                "C": float(row["Close"]),
                "H": float(row["High"]),
                "Vo": float(row.get("Volume") or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return rows, url


def fetch_google_previous_close(code: str) -> tuple[float, str]:
    url = f"{GOOGLE_FINANCE_URL}/{code}:TYO?hl=ja"
    page = html.unescape(_fetch(url).decode("utf-8", errors="replace"))
    match = re.search(
        r"前日終値[\s\S]{0,800}?¥\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        page,
    )
    if not match:
        raise FreeMarketDataError("Google Financeの前日終値を確認できません。")
    return float(match.group(1).replace(",", "")), url


def fetch_yahoo_dividend_forecast(code: str) -> dict[str, object]:
    url = f"{YAHOO_INFO_URL}/{code}.T"
    page = html.unescape(_fetch(url).decode("utf-8", errors="replace"))
    text = re.sub(r"<[^>]+>", " ", page)
    text = re.sub(r"\s+", " ", text)
    yield_match = re.search(
        r"配当利回り\s*（会社予想）\s*用語\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*%\s*\(\s*([0-9]{1,2}/[0-9]{1,2}|--)\s*\)",
        text,
    )
    dps_match = re.search(
        r"1株配当\s*（会社予想）\s*用語\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*円\s*\(\s*([0-9]{4}/[0-9]{1,2}|[0-9]{1,2}/[0-9]{1,2}|--)\s*\)",
        text,
    )
    if not dps_match:
        raise FreeMarketDataError("Yahoo Financeの1株配当（会社予想）を確認できません。")
    dps = float(dps_match.group(1).replace(",", ""))
    if dps <= 0 or dps > 2000:
        raise FreeMarketDataError("Yahoo Financeの1株配当（会社予想）が許容範囲外です。")
    forecast: dict[str, object] = {
        "dps": dps,
        "dpsAsOf": dps_match.group(2),
        "dpsSource": "Yahoo Finance 1株配当（会社予想）",
        "dpsUrl": url,
    }
    if yield_match:
        dividend_yield = float(yield_match.group(1).replace(",", "")) / 100
        if 0 < dividend_yield <= 0.25:
            forecast.update({
                "dividendYield": dividend_yield,
                "dividendYieldAsOf": yield_match.group(2),
                "dividendYieldSource": "Yahoo Finance 配当利回り（会社予想）",
                "dividendYieldKind": "forecast",
            })
    return forecast


def fetch_validated_price_metrics(code: str, start: date, end: date) -> dict[str, object]:
    yahoo_rows, yahoo_url = fetch_yahoo_history(code, start, end)
    mirror_rows, mirror_url = fetch_yahoo_history(code, start, end, YAHOO_MIRROR_URL)
    if len(yahoo_rows) < 120:
        raise FreeMarketDataError("Yahoo Financeの価格履歴が120営業日未満です。")
    if len(mirror_rows) < 120:
        raise FreeMarketDataError("Yahoo Financeの検証経路が120営業日未満です。")

    mirror_by_date = {str(item["Date"]): item for item in mirror_rows}
    common_dates = [
        str(item["Date"]) for item in reversed(yahoo_rows)
        if str(item["Date"]) in mirror_by_date
    ]
    if not common_dates:
        raise FreeMarketDataError("2つの配信経路で共通する取引日がありません。")
    validation_date = common_dates[0]
    yahoo_match = next(item for item in reversed(yahoo_rows) if item["Date"] == validation_date)
    mirror_match = mirror_by_date[validation_date]
    yahoo_close = float(yahoo_match["C"])
    mirror_close = float(mirror_match["C"])
    close_difference = abs(yahoo_close - mirror_close) / max(yahoo_close, mirror_close)
    if close_difference > 0.001:
        raise FreeMarketDataError(f"終値の差が許容範囲を超えています（{close_difference:.2%}）。")

    try:
        metrics = calculate_price_metrics(yahoo_rows)
    except JQuantsError as error:
        raise FreeMarketDataError(str(error)) from error
    latest_date = datetime.strptime(str(yahoo_rows[-1]["Date"]), "%Y-%m-%d").date()
    if (end - latest_date).days > 7:
        raise FreeMarketDataError("最新株価が7日以上更新されていません。")

    return {
        **metrics,
        "validationDate": validation_date,
        "validationCloseYahoo": yahoo_close,
        "validationCloseMirror": mirror_close,
        "validationDifference": round(close_difference, 6),
        "sources": {
            "priceHistory": {
                "url": yahoo_url,
                "updatedAt": str(metrics["asOf"]),
                "provider": "Yahoo Finance",
            },
            "priceValidation": {
                "url": mirror_url,
                "updatedAt": validation_date,
                "provider": "Yahoo Finance mirror",
            },
        },
    }
