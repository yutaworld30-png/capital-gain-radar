from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jpx_weekly_connector import (
    INVESTOR_ARCHIVE_BASE,
    JpxWeeklyError,
    MARGIN_HISTORY_PAGE,
    fetch_investor_history,
    fetch_margin_history,
)
from market_breadth import build_nikkei225_breadth
from market_technical import (
    TECHNICAL_VERSION,
    build_technical_rows,
    indicator_parameters,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs" / "data" / "nikkei225-analysis.json"
LOCAL_DATA_DIR = ROOT / ".local-data"
LOCAL_OUTPUT = LOCAL_DATA_DIR / "nikkei225-analysis.json"
LOCAL_CANDIDATE_OUTPUT = LOCAL_DATA_DIR / "latest-candidates.json"
CANDIDATE_OUTPUT = ROOT / "outputs" / "data" / "latest-candidates.json"
PUBLISHED_CANDIDATE_URL = (
    "https://yutaworld30-png.github.io/capital-gain-radar/"
    "data/latest-candidates.json"
)
PUBLISHED_ANALYSIS_URL = (
    "https://yutaworld30-png.github.io/capital-gain-radar/"
    "data/nikkei225-analysis.json"
)
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EN225"
NIKKEI_PER_URL = "https://indexes.nikkei.co.jp/nkave/archives/data?list=per"
SCHEMA_VERSION = 1
ANALYSIS_VERSION = "nikkei225-analysis-v1"
PUBLIC_DISTRIBUTION_MODE = "public"
LOCAL_PRIVATE_DISTRIBUTION_MODE = "local-private"
PER_MULTIPLIER_MIN = 12
PER_MULTIPLIER_MAX = 24


class MarketAnalysisError(RuntimeError):
    pass


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def fetch_bytes(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 CapitalGainRadar/0.6",
            "Accept": "application/json,text/html,*/*",
        },
    )
    try:
        with urlopen(request, timeout=45) as response:
            return response.read()
    except HTTPError as error:
        raise MarketAnalysisError(f"HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise MarketAnalysisError("日経225分析データを取得できませんでした。") from error


def _unix_seconds(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp())


def _market_timezone(timezone_name: object):
    name = str(timezone_name or "UTC")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Tokyo":
            return timezone(timedelta(hours=9))
        return timezone.utc


def _timestamp_date(timestamp: object, timezone_name: object) -> str | None:
    if not isinstance(timestamp, (int, float)):
        return None
    zone = _market_timezone(timezone_name)
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).astimezone(zone).date().isoformat()


def fetch_nikkei225_ohlc(*, today: date | None = None) -> tuple[list[dict[str, Any]], str]:
    current = today or date.today()
    start = current - timedelta(days=1_500)
    query = urlencode({
        "period1": _unix_seconds(start),
        "period2": _unix_seconds(current) + 86_400,
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    })
    url = f"{YAHOO_CHART_URL}?{query}"
    try:
        payload = json.loads(fetch_bytes(url).decode("utf-8"))
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        meta = result.get("meta", {})
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise MarketAnalysisError("日経225 OHLCの応答形式が不正です。") from error
    timezone_name = meta.get("exchangeTimezoneName") or meta.get("timezone")
    meta_market_date = None
    meta_market_closed = False
    meta_close_date = None
    meta_close_price = None
    try:
        zone = _market_timezone(timezone_name)
        meta_close_at = datetime.fromtimestamp(
            float(meta["regularMarketTime"]),
            tz=timezone.utc,
        ).astimezone(zone)
        meta_market_date = meta_close_at.date().isoformat()
        meta_market_closed = meta_close_at.time() >= time(15, 30)
        candidate_price = float(meta["regularMarketPrice"])
        if (
            meta_market_closed
            and math.isfinite(candidate_price)
            and candidate_price > 0
        ):
            meta_close_date = meta_close_at.date().isoformat()
            meta_close_price = candidate_price
    except (KeyError, TypeError, ValueError, OSError):
        pass
    rows: list[dict[str, Any]] = []
    for index, timestamp in enumerate(timestamps):
        row_date = _timestamp_date(timestamp, timezone_name)
        if not row_date:
            continue
        if row_date == meta_market_date and not meta_market_closed:
            continue
        try:
            values = {
                key: float(quote[key][index])
                for key in ("open", "high", "low")
            }
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        try:
            close = float(quote["close"][index])
        except (KeyError, IndexError, TypeError, ValueError):
            close = math.nan
        if (
            (not math.isfinite(close) or close <= 0)
            and row_date == meta_close_date
            and meta_close_price is not None
        ):
            close = meta_close_price
        values["close"] = close
        if any(not math.isfinite(value) or value <= 0 for value in values.values()):
            continue
        rows.append({"date": row_date, **values})
    by_date = {str(row["date"]): row for row in rows}
    ordered = [by_date[key] for key in sorted(by_date)]
    if len(ordered) < 100:
        raise MarketAnalysisError("日経225 OHLCの有効行が不足しています。")
    return ordered, url


def parse_weighted_per_html(html: str) -> list[dict[str, Any]]:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    rows = []
    for match in re.finditer(
        r"(20\d{2})[./](\d{1,2})[./](\d{1,2})\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)",
        text,
    ):
        year, month, day = (int(match.group(index)) for index in range(1, 4))
        weighted_per = float(match.group(4))
        index_per = float(match.group(5))
        if weighted_per <= 0 or index_per <= 0:
            continue
        rows.append({
            "date": date(year, month, day).isoformat(),
            "weightedPer": weighted_per,
            "indexPer": index_per,
        })
    by_date = {str(row["date"]): row for row in rows}
    return [by_date[key] for key in sorted(by_date)]


def _load_previous(path: Path = OUTPUT) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_published_previous() -> dict[str, Any]:
    try:
        payload = json.loads(fetch_bytes(PUBLISHED_ANALYSIS_URL).decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (MarketAnalysisError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _valid_price_row(row: object) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    try:
        row_date = date.fromisoformat(str(row.get("date"))).isoformat()
        values = {
            key: float(row[key])
            for key in ("open", "high", "low", "close")
        }
    except (KeyError, TypeError, ValueError):
        return None
    if any(not math.isfinite(value) or value <= 0 for value in values.values()):
        return None
    if values["high"] < max(values["open"], values["close"]):
        return None
    if values["low"] > min(values["open"], values["close"]):
        return None
    return {"date": row_date, **values}


def merge_price_rows(
    *row_sets: object,
    max_rows: int = 1_100,
) -> list[dict[str, Any]]:
    """Keep prior confirmed candles when the current provider omits a trading day."""
    by_date: dict[str, dict[str, Any]] = {}
    for rows in row_sets:
        if not isinstance(rows, list):
            continue
        for row in rows:
            normalized = _valid_price_row(row)
            if normalized is not None:
                by_date[normalized["date"]] = normalized
    ordered = [by_date[key] for key in sorted(by_date)]
    return ordered[-max_rows:]


def _load_breadth(
    path: Path = CANDIDATE_OUTPUT,
    *,
    allow_published_fallback: bool = True,
) -> dict[str, Any]:
    payload: object
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        if not allow_published_fallback:
            return {
                "status": "unavailable",
                "rows": [],
                "note": "ローカル候補JSONを読み込めませんでした。",
            }
        try:
            payload = json.loads(fetch_bytes(PUBLISHED_CANDIDATE_URL).decode("utf-8"))
        except (MarketAnalysisError, json.JSONDecodeError):
            return {
                "status": "unavailable",
                "rows": [],
                "note": "latest-candidates.jsonを読み込めませんでした。",
            }
    breadth = payload.get("nikkei225Breadth") if isinstance(payload, dict) else None
    if isinstance(breadth, dict):
        return breadth
    prices = payload.get("nikkei225Prices") if isinstance(payload, dict) else None
    if not isinstance(prices, list) and isinstance(payload, dict):
        topix_prices = payload.get("topixPrices")
        prices = [
            item for item in topix_prices
            if isinstance(item, dict) and item.get("isNikkei225") is True
        ] if isinstance(topix_prices, list) else None
    if isinstance(prices, list):
        return build_nikkei225_breadth(prices)
    return {
        "status": "unavailable",
        "rows": [],
        "note": "日経225構成銘柄の騰落データがまだ生成されていません。",
    }


def _permission_required_source(url: str, note: str) -> dict[str, Any]:
    return {
        "status": "permission-required",
        "url": url,
        "asOf": None,
        "note": note,
    }


def _weekly_recency_status(
    as_of: object,
    *,
    max_age_days: int = 21,
    today: date | None = None,
) -> tuple[str, str | None]:
    try:
        source_date = date.fromisoformat(str(as_of))
    except ValueError:
        return "unavailable", "週次データの基準日を確認できません。"
    current = today or date.today()
    age_days = (current - source_date).days
    if age_days < -2:
        return "stale-fallback", "週次データの基準日が未来日になっています。"
    if age_days > max_age_days:
        return (
            "stale-fallback",
            f"週次データの基準日が{age_days}日前のため、最新値として扱いません。",
        )
    return "available", None


def _per_data(
    previous: dict[str, Any],
    *,
    distribution_mode: str = PUBLIC_DISTRIBUTION_MODE,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prior_rows = previous.get("per", {}).get("rows", []) if isinstance(previous.get("per"), dict) else []
    local_private = distribution_mode == LOCAL_PRIVATE_DISTRIBUTION_MODE
    if not local_private and not _env_enabled("NIKKEI_INDEX_DATA_USE_CONFIRMED"):
        return [], _permission_required_source(
            NIKKEI_PER_URL,
            "日経指数データのウェブ表示・演算利用条件を確認後に有効化します。",
        )
    try:
        refreshed = parse_weighted_per_html(fetch_bytes(NIKKEI_PER_URL).decode("utf-8", errors="replace"))
        merged = {
            str(row.get("date")): row
            for row in prior_rows
            if isinstance(row, dict) and row.get("date")
        }
        merged.update({str(row["date"]): row for row in refreshed})
        rows = [merged[key] for key in sorted(merged)[-1_100:]]
        return rows, {
            "status": "available" if rows else "unavailable",
            "url": NIKKEI_PER_URL,
            "asOf": rows[-1]["date"] if rows else None,
            "accessMode": distribution_mode,
            "note": (
                "日経平均プロフィルの加重平均PER。ローカル個人利用モードで取得しています。"
                if local_private
                else "日経平均プロフィルの加重平均PER。利用条件確認済みフラグで取得しています。"
            ),
        }
    except MarketAnalysisError as error:
        if isinstance(prior_rows, list) and prior_rows:
            return prior_rows, {
                "status": "stale-fallback",
                "url": NIKKEI_PER_URL,
                "asOf": prior_rows[-1].get("date"),
                "note": f"更新失敗のため前回値を維持: {error}",
            }
        return [], {"status": "unavailable", "url": NIKKEI_PER_URL, "asOf": None, "note": str(error)}


def _weekly_data(
    previous: dict[str, Any],
    *,
    distribution_mode: str = PUBLIC_DISTRIBUTION_MODE,
    today: date | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    local_private = distribution_mode == LOCAL_PRIVATE_DISTRIBUTION_MODE
    if not local_private and not _env_enabled("JPX_PUBLIC_DATA_USE_CONFIRMED"):
        margin = {
            **_permission_required_source(
                MARGIN_HISTORY_PAGE,
                "JPX公開データの二次利用条件を確認後に有効化します。",
            ),
            "rows": [],
        }
        investor = {
            **_permission_required_source(
                INVESTOR_ARCHIVE_BASE.format(index=0),
                "JPX公開データの二次利用条件を確認後に有効化します。",
            ),
            "rows": [],
        }
        return margin, investor

    previous_margin = previous.get("margin", {}).get("rows", []) if isinstance(previous.get("margin"), dict) else []
    previous_investor = (
        previous.get("investorFlows", {}).get("rows", [])
        if isinstance(previous.get("investorFlows"), dict)
        else []
    )
    try:
        margin_rows, margin_url = fetch_margin_history(previous_margin)
        margin_as_of = margin_rows[-1]["weekEnd"] if margin_rows else None
        margin_status, margin_note = _weekly_recency_status(margin_as_of, today=today)
        margin = {
            "status": margin_status,
            "url": margin_url,
            "asOf": margin_as_of,
            "unit": "thousand-shares",
            "scope": "東京・名古屋二市場合計",
            "accessMode": distribution_mode,
            "note": margin_note,
            "rows": margin_rows,
        }
    except JpxWeeklyError as error:
        margin = {
            "status": "stale-fallback" if previous_margin else "unavailable",
            "url": MARGIN_HISTORY_PAGE,
            "asOf": previous_margin[-1].get("weekEnd") if previous_margin else None,
            "note": str(error),
            "rows": previous_margin,
        }
    try:
        investor_rows, source_urls = fetch_investor_history(previous_investor)
        investor_as_of = investor_rows[-1]["periodEnd"] if investor_rows else None
        investor_status, investor_note = _weekly_recency_status(investor_as_of, today=today)
        if not source_urls:
            investor_status = "stale-fallback" if investor_rows else "unavailable"
            investor_note = "直近の投資主体別ファイルを更新できず、前回履歴を維持しています。"
        investor = {
            "status": investor_status,
            "url": INVESTOR_ARCHIVE_BASE.format(index=0),
            "asOf": investor_as_of,
            "unit": "100m-yen",
            "scope": "東京・名古屋二市場合計（金額）",
            "accessMode": distribution_mode,
            "note": investor_note,
            "sourceFiles": source_urls[-8:],
            "rows": investor_rows,
        }
    except JpxWeeklyError as error:
        investor = {
            "status": "stale-fallback" if previous_investor else "unavailable",
            "url": INVESTOR_ARCHIVE_BASE.format(index=0),
            "asOf": previous_investor[-1].get("periodEnd") if previous_investor else None,
            "note": str(error),
            "rows": previous_investor,
        }
    return margin, investor


def _per_reference(
    technical_rows: list[dict[str, Any]],
    per_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not technical_rows or not per_rows:
        return None
    close_by_date = {str(row["date"]): float(row["close"]) for row in technical_rows}
    match = next(
        (
            row for row in reversed(per_rows)
            if str(row.get("date")) in close_by_date
            and isinstance(row.get("weightedPer"), (int, float))
            and float(row["weightedPer"]) > 0
        ),
        None,
    )
    if not match:
        return None
    weighted_per = float(match["weightedPer"])
    close = close_by_date[str(match["date"])]
    implied_eps = close / weighted_per
    return {
        "date": match["date"],
        "weightedPer": round(weighted_per, 2),
        "close": round(close, 2),
        "impliedEps": round(implied_eps, 4),
        "bandLevels": {
            str(multiplier): round(implied_eps * multiplier, 2)
            for multiplier in range(PER_MULTIPLIER_MIN, PER_MULTIPLIER_MAX + 1)
        },
        "basis": "基準日の指数値÷加重平均PERで算出したEPSを固定した参考ライン",
    }


def validate_analysis(payload: object, *, public_only: bool = False) -> list[str]:
    if not isinstance(payload, dict):
        return ["ルートがJSONオブジェクトではありません。"]
    errors: list[str] = []
    distribution_mode = payload.get("distributionMode", PUBLIC_DISTRIBUTION_MODE)
    if distribution_mode not in {PUBLIC_DISTRIBUTION_MODE, LOCAL_PRIVATE_DISTRIBUTION_MODE}:
        errors.append("distributionModeが不正です。")
    if public_only and distribution_mode != PUBLIC_DISTRIBUTION_MODE:
        errors.append("ローカル個人利用データは公開成果物に含められません。")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        errors.append("schemaVersionが一致しません。")
    if payload.get("analysisVersion") != ANALYSIS_VERSION:
        errors.append("analysisVersionが一致しません。")
    if payload.get("technicalVersion") != TECHNICAL_VERSION:
        errors.append("technicalVersionが一致しません。")
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) < 100:
        errors.append("日経225テクニカル行が100件未満です。")
    else:
        required = ("date", "open", "high", "low", "close", "ma5", "ma25", "psar")
        if any(not isinstance(row, dict) or any(key not in row for key in required) for row in rows):
            errors.append("日経225テクニカル行の必須項目が不足しています。")
        if [str(row.get("date")) for row in rows] != sorted(str(row.get("date")) for row in rows):
            errors.append("日経225テクニカル行が日付順ではありません。")
    for key in ("margin", "investorFlows", "breadth", "per"):
        if not isinstance(payload.get(key), dict) or not payload[key].get("status"):
            errors.append(f"{key}の状態がありません。")
    return errors


def build_analysis_payload(
    raw_rows: list[dict[str, Any]],
    *,
    generated_at: str,
    price_url: str,
    previous: dict[str, Any] | None = None,
    per_rows: list[dict[str, Any]] | None = None,
    per_source: dict[str, Any] | None = None,
    margin: dict[str, Any] | None = None,
    investor: dict[str, Any] | None = None,
    breadth: dict[str, Any] | None = None,
    distribution_mode: str = PUBLIC_DISTRIBUTION_MODE,
) -> dict[str, Any]:
    previous = previous or {}
    if per_rows is None or per_source is None:
        per_rows, per_source = _per_data(previous, distribution_mode=distribution_mode)
    if margin is None or investor is None:
        margin, investor = _weekly_data(previous, distribution_mode=distribution_mode)
    technical_rows = build_technical_rows(raw_rows, weighted_per_rows=per_rows)
    local_private = distribution_mode == LOCAL_PRIVATE_DISTRIBUTION_MODE
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "analysisVersion": ANALYSIS_VERSION,
        "technicalVersion": TECHNICAL_VERSION,
        "generatedAt": generated_at,
        "scope": "日経225",
        "distributionMode": distribution_mode,
        "usage": (
            "ローカル個人利用専用です。外部公開・共有・再配布をしないでください。"
            if local_private
            else "相場環境の確認用です。個別銘柄ランキングの総合スコアには反映しません。"
        ),
        "parameters": indicator_parameters(),
        "priceSource": {
            "status": "available",
            "provider": "Yahoo Finance chart",
            "symbol": "^N225",
            "url": price_url,
            "asOf": technical_rows[-1]["date"] if technical_rows else None,
            "priceBasis": "index-ohlc",
        },
        "rows": technical_rows,
        "latest": technical_rows[-1] if technical_rows else None,
        "per": {
            **per_source,
            "rows": per_rows,
            "reference": _per_reference(technical_rows, per_rows),
            "multipliers": list(range(PER_MULTIPLIER_MIN, PER_MULTIPLIER_MAX + 1)),
        },
        "margin": margin,
        "investorFlows": investor,
        "breadth": breadth if breadth is not None else _load_breadth(),
    }
    quality_issues = validate_analysis(payload)
    if local_private:
        for key, label in (
            ("per", "日経PER"),
            ("margin", "信用残"),
            ("investorFlows", "投資主体別"),
        ):
            section = payload.get(key) if isinstance(payload.get(key), dict) else {}
            if section.get("status") != "available":
                quality_issues.append(f"{label}が最新の利用可能状態ではありません。")
    payload["quality"] = {
        "status": "available" if not quality_issues else "partial",
        "issues": quality_issues,
    }
    return payload


def resolve_output_path(*, local_private: bool, requested: Path | None = None) -> Path:
    output = requested or (LOCAL_OUTPUT if local_private else OUTPUT)
    if not output.is_absolute():
        output = ROOT / output
    output = output.resolve()
    if local_private and not output.is_relative_to(LOCAL_DATA_DIR.resolve()):
        raise MarketAnalysisError(
            "ローカル個人利用データは .local-data 配下にのみ保存できます。"
        )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="日経225テクニカル分析JSONを生成")
    parser.add_argument(
        "--local-private",
        action="store_true",
        help="日経PER・JPX週次統計をローカル個人利用専用として取得する",
    )
    parser.add_argument("--output", type=Path, help="JSON出力先")
    args = parser.parse_args(argv)
    try:
        output = resolve_output_path(local_private=args.local_private, requested=args.output)
    except MarketAnalysisError as error:
        print(f"ERROR: {error}")
        return 2
    distribution_mode = (
        LOCAL_PRIVATE_DISTRIBUTION_MODE
        if args.local_private
        else PUBLIC_DISTRIBUTION_MODE
    )
    generated_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    local_previous = _load_previous(output)
    published_previous = (
        {} if args.local_private else _load_published_previous()
    )
    previous = published_previous or local_previous
    try:
        refreshed_rows, price_url = fetch_nikkei225_ohlc()
    except MarketAnalysisError as error:
        print(f"ERROR: {error}")
        return 1
    raw_rows = merge_price_rows(
        local_previous.get("rows"),
        published_previous.get("rows"),
        refreshed_rows,
    )
    payload = build_analysis_payload(
        raw_rows,
        generated_at=generated_at,
        price_url=price_url,
        previous=previous,
        breadth=(
            _load_breadth(LOCAL_CANDIDATE_OUTPUT, allow_published_fallback=False)
            if args.local_private
            else None
        ),
        distribution_mode=distribution_mode,
    )
    errors = validate_analysis(payload)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(
        "OK: 日経225分析JSONを生成しました "
        f"({len(payload['rows'])}日, {payload['priceSource']['asOf']}, {distribution_mode})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
