from __future__ import annotations

import csv
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from market_technical import build_technical_rows


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs" / "data" / "market-environment.json"
HISTORY_OUTPUT = ROOT / "outputs" / "data" / "market-environment-history.json"
HISTORY_URL = "https://yutaworld30-png.github.io/capital-gain-radar/data/market-environment-history.json"
HISTORY_MAX_DAYS = max(93, int(os.getenv("MARKET_HISTORY_MAX_DAYS", "400")))

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
JPX_SHORT_SELLING_URL = "https://www.jpx.co.jp/markets/statistics-equities/short-selling/index.html"
JPX_INVESTOR_TYPE_URL = "https://www.jpx.co.jp/markets/statistics-equities/investor-type/index.html"
JPX_SETTLEMENT_URL = "https://www.jpx.co.jp/markets/derivatives/settlement-price/index.html"


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
    history: list[dict[str, Any]] = field(default_factory=list, repr=False)
    contract: str = ""

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
        result = {
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
        if self.contract:
            result.update({"contract": self.contract, "priceBasis": "ose-settlement",
                           "settlementHistory": self.history})
        return result


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


class SettlementLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        href = dict(attrs).get("href") or ""
        if tag == "a" and re.search(r"/rb\d{8}\.csv$", href):
            url = urljoin(JPX_SETTLEMENT_URL, href)
            if urlparse(url).hostname == "www.jpx.co.jp":
                self.links.append(url)


def parse_topix_settlement(data: bytes, *, contract: str | None = None) -> tuple[str, float]:
    rows = list(csv.reader(StringIO(data.decode("cp932"))))
    header_index = next((i for i, row in enumerate(rows) if row and row[0] == "銘柄コード"), None)
    if header_index is None:
        raise MarketEnvironmentError("清算値CSVの列を確認できません。")
    records = (dict(zip(rows[header_index], row)) for row in rows[header_index + 1:])
    futures = []
    for row in records:
        if not re.fullmatch(r"FUT_TPX_\d{6}", row.get("銘柄名称", "")):
            continue
        month = row.get("限月", "")
        if not re.fullmatch(r"\d{6}", month) or (contract and month != contract):
            continue
        try:
            value = float(row.get("清算価格", ""))
        except (ValueError, TypeError):
            continue
        if math.isfinite(value) and value > 0:
            futures.append((month, value))
    if not futures:
        raise MarketEnvironmentError("TOPIX先物の清算値を確認できません。")
    return min(futures)


def fetch_topix_futures(previous_point: dict[str, Any] | None = None) -> MarketPoint:
    point = MarketPoint("topixFutures", "TOPIX先物", None, None, None,
                        "JPX / 大阪取引所", JPX_SETTLEMENT_URL, status="unavailable")
    try:
        links = SettlementLinks()
        links.feed(fetch_bytes(JPX_SETTLEMENT_URL).decode("utf-8"))
        if not links.links:
            raise MarketEnvironmentError("清算値CSVが見つかりません。")
        url = max(links.links)
        stamp = re.search(r"rb(\d{8})\.csv$", url).group(1)
        day = datetime.strptime(stamp, "%Y%m%d").date()
        contract, value = parse_topix_settlement(fetch_bytes(url))
        point.contract = contract
        point.value, point.as_of, point.url = value, day.isoformat(), url
        point.status = "available"
        point.note = f"大阪取引所 / {contract[:4]}年{contract[4:]}月限 / 清算値（リアルタイム価格ではありません）"
        previous_point = previous_point or {}
        saved_rows = previous_point.get("settlementHistory") or []
        # Only observations of the same contract and price basis are comparable.
        if previous_point.get("contract") != contract or previous_point.get("priceBasis") != "ose-settlement":
            saved_rows = []
        history = {}
        for row in saved_rows:
            stamp_date = str(row.get("date") or "")
            saved_value = row.get("value")
            if (re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp_date) and stamp_date < point.as_of
                    and isinstance(saved_value, (int, float)) and math.isfinite(saved_value) and saved_value > 0):
                history[stamp_date] = {"date": stamp_date, "value": saved_value}
        point.history = [history[key] for key in sorted(history)][-HISTORY_MAX_DAYS:]
        if point.history:
            previous = point.history[-1]
            point.previous = previous["value"]
            point.note += f" / 比較日 {previous['date']}（同一限月・前回記録比）"
        point.history.append({"date": point.as_of, "value": value})
        if point.previous is None:
            point.note += " / 比較日の清算値は未取得"
        if (date.today() - day).days > 4:
            point.status = "stale"
        return point
    except (MarketEnvironmentError, ValueError, UnicodeError) as error:
        point.note = str(error)
        return point


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


def timestamp_date(timestamp: object, timezone_name: object) -> str | None:
    if not isinstance(timestamp, (int, float)):
        return None
    zone_name = str(timezone_name or "UTC")
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).astimezone(zone).date().isoformat()


def apply_meta_quote(
    value: float | None,
    previous: float | None,
    as_of: str | None,
    meta: dict[str, Any],
    *,
    scale: float = 1.0,
) -> tuple[float | None, float | None, str | None, bool]:
    raw_price = meta.get("regularMarketPrice")
    raw_time = meta.get("regularMarketTime")
    if not isinstance(raw_price, (int, float)) or not math.isfinite(float(raw_price)) or float(raw_price) <= 0:
        return value, previous, as_of, False
    meta_as_of = timestamp_date(raw_time, meta.get("exchangeTimezoneName") or meta.get("timezone"))
    if not meta_as_of:
        return value, previous, as_of, False
    if as_of and meta_as_of < as_of:
        return value, previous, as_of, False
    meta_previous = meta.get("regularMarketPreviousClose")
    if not isinstance(meta_previous, (int, float)) or not math.isfinite(float(meta_previous)) or float(meta_previous) <= 0:
        meta_previous = value if as_of and meta_as_of > as_of else previous
    return (
        float(raw_price) * scale,
        float(meta_previous) * scale if isinstance(meta_previous, (int, float)) and float(meta_previous) > 0 else previous,
        meta_as_of,
        True,
    )


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
            meta = result.get("meta", {})
            timestamps = result["timestamp"]
            quote = result["indicators"]["quote"][0]
            rows: list[dict[str, Any]] = []
            for index, timestamp in enumerate(timestamps):
                close = quote.get("close", [])[index]
                if close in (None, 0):
                    continue
                row = {
                    "date": timestamp_date(
                        timestamp,
                        meta.get("exchangeTimezoneName") or meta.get("timezone"),
                    ),
                    "value": float(close) * scale,
                }
                if key == "vix":
                    for field_name in ("open", "high", "low"):
                        values = quote.get(field_name, [])
                        raw = values[index] if index < len(values) else None
                        if isinstance(raw, (int, float)) and math.isfinite(raw) and raw > 0:
                            row[field_name] = float(raw) * scale
                    row["close"] = float(close) * scale
                rows.append(row)
            value, previous, as_of = latest_pair(rows)
            value, previous, as_of, used_meta = apply_meta_quote(value, previous, as_of, meta, scale=scale)
            if value is None:
                raise MarketEnvironmentError("有効な終値がありません。")
            if used_meta and as_of and key != "vix":
                rows = [row for row in rows if row.get("date") != as_of]
                rows.append({"date": as_of, "value": value})
                rows.sort(key=lambda row: str(row.get("date") or ""))
            note = f"symbol={symbol}"
            if scale != 1.0:
                note += f", scale={scale}"
            if used_meta:
                note += ", latest=meta"
            return MarketPoint(
                key,
                label,
                value,
                previous,
                as_of,
                "Yahoo Finance chart",
                url,
                unit=unit,
                note=note,
                history=rows,
            )
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
        return MarketPoint(key, label, value, previous, as_of, "FRED", url, unit=unit, history=rows)
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


def market_environment_score(indicators: dict[str, MarketPoint]) -> int:
    return weighted_score([
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


def point_at_date(point: MarketPoint, target_date: str) -> MarketPoint:
    rows = [
        row for row in point.history
        if str(row.get("date") or "") <= target_date and isinstance(row.get("value"), (int, float))
    ]
    if not rows:
        return MarketPoint(
            point.key,
            point.label,
            None,
            None,
            None,
            point.source,
            point.url,
            "unavailable",
            point.unit,
            "履歴値なし",
        )
    latest = rows[-1]
    previous = rows[-2] if len(rows) >= 2 else {}
    return MarketPoint(
        point.key,
        point.label,
        float(latest["value"]),
        float(previous["value"]) if isinstance(previous.get("value"), (int, float)) else None,
        str(latest.get("date") or ""),
        point.source,
        point.url,
        unit=point.unit,
    )


def build_historical_snapshots(points: list[MarketPoint], generated_at: str) -> list[dict[str, Any]]:
    point_map = {point.key: point for point in points}
    primary = point_map.get("nikkei225")
    if not primary:
        return []
    cutoff = (date.today() - timedelta(days=HISTORY_MAX_DAYS)).isoformat()
    target_dates = sorted({
        str(row.get("date") or "")
        for row in primary.history
        if str(row.get("date") or "") >= cutoff
    })
    snapshots: list[dict[str, Any]] = []
    for target_date in target_dates:
        historical = {key: point_at_date(point, target_date) for key, point in point_map.items()}
        score = market_environment_score(historical)
        snapshots.append({
            "date": target_date,
            "generatedAt": generated_at,
            "score": score,
            "label": label_for_score(score),
            "source": "historical-provider",
            "indicators": {
                key: {
                    "label": point.label,
                    "value": point.value,
                    "previous": point.previous,
                    "change": point.change,
                    "changeRate": point.change_rate,
                    "asOf": point.as_of,
                    "status": point.status,
                    "unit": point.unit,
                }
                for key, point in historical.items()
                if point.value is not None
            },
        })
    return snapshots


def merge_history_snapshots(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    max_days: int = HISTORY_MAX_DAYS,
) -> list[dict[str, Any]]:
    by_date = {
        str(snapshot.get("date") or ""): snapshot
        for snapshot in [*existing, *incoming]
        if str(snapshot.get("date") or "")
    }
    if not by_date:
        return []
    latest_date = max(by_date)
    try:
        cutoff = (date.fromisoformat(latest_date) - timedelta(days=max_days)).isoformat()
    except ValueError:
        cutoff = ""
    return [by_date[key] for key in sorted(by_date) if key >= cutoff]


def read_history_payload() -> dict[str, Any]:
    snapshots: list[dict[str, Any]] = []
    if HISTORY_OUTPUT.exists():
        try:
            local = json.loads(HISTORY_OUTPUT.read_text(encoding="utf-8"))
            snapshots.extend(local.get("snapshots") or [])
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    try:
        public = json.loads(fetch_bytes(HISTORY_URL).decode("utf-8"))
        snapshots.extend(public.get("snapshots") or [])
    except (MarketEnvironmentError, json.JSONDecodeError, AttributeError):
        pass
    return {"schemaVersion": 1, "snapshots": merge_history_snapshots([], snapshots)}


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
    previous_market: dict[str, Any] = {}
    previous_url = os.environ.get("PRIVATE_MARKET_ENVIRONMENT_URL", "").strip()
    try:
        if previous_url:
            from check_published_freshness import fetch_payload
            previous_market = fetch_payload(previous_url)
        elif OUTPUT.exists():
            previous_market = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError):
        print("Previous market observations unavailable; comparisons remain unknown.")
    points = [
        fetch_yahoo_point("nikkei225", "日経225", ["^N225"], unit="円"),
        fetch_yahoo_point("topix", "TOPIX連動ETF", ["1306.T"]),
        fetch_yahoo_point("growth250", "グロース250連動ETF", ["2516.T", "1563.T"]),
        fetch_yahoo_point("nikkei225Futures", "CME日経平均先物", ["NIY=F", "NKD=F"], unit="円"),
        fetch_topix_futures((previous_market.get("indicators") or {}).get("topixFutures")),
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
    score = market_environment_score(indicators)
    available = [point for point in points if point.status == "available" and point.value is not None]
    quality_checks = [
        {
            "label": "数値取得",
            "status": "available" if len(available) >= 8 else "partial",
            "message": f"{len(available)}/12指標の数値を取得しました。JPX需給系は公開ページの更新確認を先に表示します。",
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
    existing_history = read_history_payload()
    snapshots = merge_history_snapshots(
        existing_history.get("snapshots") or [],
        build_historical_snapshots(points, generated_at),
    )
    history_payload = {
        "schemaVersion": 1,
        "generatedAt": generated_at,
        "retentionDays": HISTORY_MAX_DAYS,
        "snapshots": snapshots,
    }
    vix_history = indicators["vix"].history
    new_york_now = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    if new_york_now.time() < time(16, 15):
        vix_history = [row for row in vix_history if row.get("date") != new_york_now.date().isoformat()]
    vix_rows = build_technical_rows(vix_history)
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
        "vixTechnical": {
            "status": "available" if len(vix_rows) >= 35 else "unavailable",
            "asOf": vix_rows[-1]["date"] if vix_rows else None,
            "source": indicators["vix"].source,
            "url": indicators["vix"].url,
            "note": "確定済みの日足OHLCからMACD(12,26,9)を計算。場中値はローソク足に混ぜません。",
            "rows": vix_rows[-260:],
        },
        "groups": {
            "japanTrend": ["nikkei225", "topix", "growth250"],
            "futures": ["nikkei225Futures", "topixFutures"],
            "usMarket": ["sp500", "nasdaq", "sox"],
            "risk": ["vix", "us10y", "usdJpy"],
            "commodity": ["wtiCrudeOil"],
            "supply": ["shortSelling", "foreignInvestorFlow"],
        },
        "qualityChecks": quality_checks,
        "marketHistory": {
            "status": "available" if len(snapshots) >= 2 else "partial",
            "snapshotCount": len(snapshots),
            "oldestDate": snapshots[0]["date"] if snapshots else None,
            "latestDate": snapshots[-1]["date"] if snapshots else None,
            "url": "data/market-environment-history.json",
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_OUTPUT.write_text(json.dumps(history_payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {HISTORY_OUTPUT} ({len(snapshots)} snapshots)")
    print(f"Wrote {OUTPUT}")
    print(f"Market environment score: {score} {label_for_score(score)}")


if __name__ == "__main__":
    main()
