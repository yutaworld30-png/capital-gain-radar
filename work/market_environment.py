from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs" / "data" / "market-environment.json"

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
JPX_SHORT_SELLING_URL = "https://www.jpx.co.jp/markets/statistics-equities/short-selling/index.html"
JPX_INVESTOR_TYPE_URL = "https://www.jpx.co.jp/markets/statistics-equities/investor-type/index.html"


class MarketEnvironmentError(RuntimeError):
    pass


@dataclass
class MarketPoint:
    key: str
    label: str
    value: float | None
    previous: float | None
    as_of: str | None
    source: str
    url: str
    status: str = "available"
    unit: str = ""
    note: str = ""

    @property
    def change(self) -> float | None:
        if self.value is None or self.previous in (None, 0):
            return None
        return self.value - self.previous

    @property
    def change_rate(self) -> float | None:
        if self.value is None or self.previous in (None, 0):
            return None
        return self.value / self.previous - 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "previous": self.previous,
            "change": self.change,
            "changeRate": self.change_rate,
            "asOf": self.as_of,
            "source": self.source,
            "url": self.url,
            "status": self.status,
            "unit": self.unit,
            "note": self.note,
        }


def fetch_bytes(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 CapitalGainRadar/0.5",
            "Accept": "application/json,text/csv,text/html,*/*",
        },
    )
    try:
        with urlopen(request, timeout=45) as response:
            return response.read()
    except HTTPError as error:
        raise MarketEnvironmentError(f"HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise MarketEnvironmentError("市場環境データの取得に失敗しました。") from error


def unix_seconds(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp())


def latest_pair(rows: list[dict[str, Any]]) -> tuple[float | None, float | None, str | None]:
    valid = [
        row for row in rows
        if isinstance(row.get("value"), (int, float)) and math.isfinite(float(row["value"]))
    ]
    if not valid:
        return None, None, None
    latest = valid[-1]
    previous = valid[-2] if len(valid) >= 2 else {}
    return float(latest["value"]), (
        float(previous["value"]) if isinstance(previous.get("value"), (int, float)) else None
    ), str(latest.get("date") or "")


def fetch_yahoo_point(key: str, label: str, symbols: list[str], *, unit: str = "", scale: float = 1.0) -> MarketPoint:
    end = date.today()
    start = end - timedelta(days=180)
    last_error = ""
    for symbol in symbols:
        query = urlencode({
            "period1": unix_seconds(start),
            "period2": unix_seconds(end) + 86400,
            "interval": "1d",
            "events": "history",
        })
        url = f"{YAHOO_CHART_URL}/{symbol}?{query}"
        try:
            payload = json.loads(fetch_bytes(url).decode("utf-8"))
            result = payload["chart"]["result"][0]
            timestamps = result["timestamp"]
            quote = result["indicators"]["quote"][0]
            rows: list[dict[str, Any]] = []
            for index, timestamp in enumerate(timestamps):
                close = quote.get("close", [])[index]
                if close in (None, 0):
                    continue
                rows.append({
                    "date": datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(),
                    "value": float(close) * scale,
                })
            value, previous, as_of = latest_pair(rows)
            if value is None:
                raise MarketEnvironmentError("有効な終値がありません。")
            note = f"symbol={symbol}"
            if scale != 1.0:
                note += f", scale={scale}"
            return MarketPoint(key, label, value, previous, as_of, "Yahoo Finance chart", url, unit=unit, note=note)
        except (MarketEnvironmentError, KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            last_error = str(error)
            continue
    return MarketPoint(key, label, None, None, None, "Yahoo Finance chart", YAHOO_CHART_URL, "unavailable", unit, last_error)


def fetch_fred_point(key: str, label: str, series: str, *, unit: str = "") -> MarketPoint:
    query = urlencode({"id": series})
    url = f"{FRED_CSV_URL}?{query}"
    try:
        text = fetch_bytes(url).decode("utf-8-sig")
        reader = csv.DictReader(StringIO(text))
        rows: list[dict[str, Any]] = []
        for row in reader:
            raw_value = row.get(series)
            if not raw_value or raw_value == ".":
                continue
            try:
                rows.append({"date": row.get("observation_date"), "value": float(raw_value)})
            except ValueError:
                continue
        value, previous, as_of = latest_pair(rows)
        if value is None:
            raise MarketEnvironmentError("FREDに有効な値がありません。")
        return MarketPoint(key, label, value, previous, as_of, "FRED", url, unit=unit)
    except MarketEnvironmentError as error:
        return MarketPoint(key, label, None, None, None, "FRED", url, "unavailable", unit, str(error))


def fetch_jpx_page_status(key: str, label: str, url: str) -> MarketPoint:
    try:
        text = fetch_bytes(url).decode("utf-8", errors="replace")
        match = re.search(r"20\d{2}/\d{2}/\d{2}\s*更新", text)
        as_of = match.group(0).replace(" 更新", "") if match else None
        return MarketPoint(
            key,
            label,
            None,
            None,
            as_of,
            "JPX",
            url,
            "available" if as_of else "partial",
            note="JPX公開ページの更新確認です。数値取得は次段階でPDF/Excel解析を追加します。",
        )
    except MarketEnvironmentError as error:
        return MarketPoint(key, label, None, None, None, "JPX", url, "unavailable", note=str(error))


def score_trend(point: MarketPoint) -> int | None:
    change_rate = point.change_rate
    if change_rate is None:
        return None
    if change_rate >= 0.015:
        return 90
    if change_rate >= 0.005:
        return 75
    if change_rate > -0.005:
        return 55
    if change_rate > -0.015:
        return 35
    return 15


def score_vix(point: MarketPoint) -> int | None:
    value = point.value
    if value is None:
        return None
    if value <= 15:
        return 85
    if value <= 20:
        return 70
    if value <= 25:
        return 45
    if value <= 30:
        return 30
    return 15


def score_us10y(point: MarketPoint) -> int | None:
    value = point.value
    change = point.change
    if value is None:
        return None
    score = 60
    if value >= 5:
        score -= 25
    elif value >= 4.5:
        score -= 15
    elif value <= 3.8:
        score += 10
    if change is not None and change >= 0.08:
        score -= 15
    elif change is not None and change <= -0.08:
        score += 10
    return max(0, min(100, score))


def score_usd_jpy(point: MarketPoint) -> int | None:
    change_rate = point.change_rate
    if change_rate is None:
        return None
    if change_rate >= 0.01:
        return 75
    if change_rate >= 0:
        return 65
    if change_rate >= -0.01:
        return 50
    return 35


def score_crude(point: MarketPoint) -> int | None:
    change_rate = point.change_rate
    if change_rate is None:
        return None
    if change_rate >= 0.05:
        return 35
    if change_rate >= 0.01:
        return 60
    if change_rate >= -0.03:
        return 55
    return 35


def weighted_score(parts: list[tuple[int | None, float]]) -> int:
    valid = [(score, weight) for score, weight in parts if score is not None]
    if not valid:
        return 50
    total_weight = sum(weight for _score, weight in valid)
    return round(sum(float(score) * weight for score, weight in valid) / total_weight)


def label_for_score(score: int) -> str:
    if score >= 75:
        return "追い風"
    if score >= 60:
        return "やや追い風"
    if score >= 45:
        return "中立"
    if score >= 30:
        return "慎重"
    return "逆風"


def build_summary(score: int, indicators: dict[str, MarketPoint]) -> str:
    label = label_for_score(score)
    futures = indicators.get("nikkei225Futures")
    vix = indicators.get("vix")
    crude = indicators.get("wtiCrudeOil")
    notes: list[str] = [f"相場環境は{label}です。"]
    if futures and futures.change_rate is not None:
        notes.append("日経平均先物は" + ("プラス圏です。" if futures.change_rate >= 0 else "マイナス圏です。"))
    if vix and vix.value is not None and vix.value >= 25:
        notes.append("VIXが高く、リスク回避に注意します。")
    if crude and crude.change_rate is not None and crude.change_rate >= 0.05:
        notes.append("原油急騰は資源株には追い風ですが、インフレ懸念には注意します。")
    return "".join(notes)


def main() -> None:
    generated_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    points = [
        fetch_yahoo_point("nikkei225", "日経225", ["^N225"], unit="円"),
        fetch_yahoo_point("topix", "TOPIX", ["^TOPX", "1306.T"]),
        fetch_yahoo_point("growth250", "グロース250", ["2516.T", "1563.T"]),
        fetch_yahoo_point("nikkei225Futures", "日経平均先物", ["NIY=F", "NKD=F"], unit="円"),
        fetch_yahoo_point("sp500", "S&P500", ["^GSPC"]),
        fetch_yahoo_point("nasdaq", "NASDAQ", ["^IXIC"]),
        fetch_yahoo_point("sox", "SOX", ["^SOX"]),
        fetch_yahoo_point("vix", "VIX", ["^VIX"]),
        fetch_yahoo_point("us10y", "米10年金利", ["^TNX"], unit="%"),
        fetch_yahoo_point("usdJpy", "ドル円", ["JPY=X"], unit="円"),
        fetch_yahoo_point("wtiCrudeOil", "WTI原油", ["CL=F"], unit="ドル"),
        fetch_jpx_page_status("shortSelling", "空売り比率", JPX_SHORT_SELLING_URL),
        fetch_jpx_page_status("foreignInvestorFlow", "海外投資家売買", JPX_INVESTOR_TYPE_URL),
    ]
    indicators = {point.key: point for point in points}
    score = weighted_score([
        (weighted_score([
            (score_trend(indicators["nikkei225"]), 0.4),
            (score_trend(indicators["topix"]), 0.4),
            (score_trend(indicators["growth250"]), 0.2),
        ]), 0.25),
        (score_trend(indicators["nikkei225Futures"]), 0.15),
        (weighted_score([
            (score_trend(indicators["sp500"]), 0.35),
            (score_trend(indicators["nasdaq"]), 0.35),
            (score_trend(indicators["sox"]), 0.30),
        ]), 0.20),
        (weighted_score([
            (score_vix(indicators["vix"]), 0.35),
            (score_us10y(indicators["us10y"]), 0.35),
            (score_usd_jpy(indicators["usdJpy"]), 0.30),
        ]), 0.15),
        (score_crude(indicators["wtiCrudeOil"]), 0.05),
        (50, 0.20),
    ])
    available = [point for point in points if point.status == "available" and point.value is not None]
    quality_checks = [
        {
            "label": "数値取得",
            "status": "available" if len(available) >= 8 else "partial",
            "message": f"{len(available)}/11指標の数値を取得しました。JPX需給系は公開ページの更新確認を先に表示します。",
        },
        {
            "label": "日経平均先物",
            "status": indicators["nikkei225Futures"].status,
            "message": indicators["nikkei225Futures"].note or "無料データで日経平均先物を確認しました。",
        },
        {
            "label": "原油価格",
            "status": indicators["wtiCrudeOil"].status,
            "message": indicators["wtiCrudeOil"].note or "FREDからWTI原油価格を確認しました。",
        },
    ]
    payload = {
        "schemaVersion": 1,
        "generatedAt": generated_at,
        "marketEnvironment": {
            "score": score,
            "label": label_for_score(score),
            "summary": build_summary(score, indicators),
            "usage": "表示専用です。銘柄ランキングの総合スコアには反映していません。",
        },
        "indicators": {key: point.to_dict() for key, point in indicators.items()},
        "groups": {
            "japanTrend": ["nikkei225", "topix", "growth250"],
            "futures": ["nikkei225Futures"],
            "usMarket": ["sp500", "nasdaq", "sox"],
            "risk": ["vix", "us10y", "usdJpy"],
            "commodity": ["wtiCrudeOil"],
            "supply": ["shortSelling", "foreignInvestorFlow"],
        },
        "qualityChecks": quality_checks,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUTPUT}")
    print(f"Market environment score: {score} {label_for_score(score)}")


if __name__ == "__main__":
    main()
