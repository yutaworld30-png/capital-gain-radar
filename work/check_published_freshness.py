from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PUBLIC_DATA_URL = (
    "https://yutaworld30-png.github.io/"
    "capital-gain-radar/data/latest-candidates.json"
)
PUBLIC_ANALYSIS_URL = (
    "https://yutaworld30-png.github.io/"
    "capital-gain-radar/data/nikkei225-analysis.json"
)
PUBLIC_SCORE_HISTORY_URL = (
    "https://yutaworld30-png.github.io/"
    "capital-gain-radar/data/score-history-v2.json"
)
try:
    JST = ZoneInfo("Asia/Tokyo")
except ZoneInfoNotFoundError:
    JST = timezone(timedelta(hours=9), name="JST")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _expected_price_date(current: datetime) -> date:
    expected = current.date()
    if current.weekday() >= 5 or current.time() < time(15, 30):
        expected -= timedelta(days=1)
    while expected.weekday() >= 5:
        expected -= timedelta(days=1)
    return expected


def freshness_issues(
    payload: object,
    *,
    now: datetime | None = None,
) -> list[str]:
    if not isinstance(payload, dict):
        return ["公開JSONがオブジェクトではありません。"]

    current = (now or datetime.now(tz=JST)).astimezone(JST)
    generated_at = _parse_timestamp(payload.get("generatedAt"))
    issues: list[str] = []
    if generated_at is None:
        issues.append("generatedAtが未設定またはタイムゾーンなしです。")
    elif generated_at.astimezone(JST).date() != current.date():
        issues.append(
            "generatedAtが当日ではありません: "
            f"{generated_at.astimezone(JST).date().isoformat()}"
        )

    sources = payload.get("sources")
    price_source = sources.get("priceHistory") if isinstance(sources, dict) else None
    if not isinstance(price_source, dict):
        issues.append("価格履歴の取得状態がありません。")
    else:
        if price_source.get("status") != "available":
            issues.append("価格履歴がavailableではありません。")
        price_as_of = _parse_date(price_source.get("asOf"))
        if price_as_of is None:
            issues.append("価格履歴の基準日がありません。")
        elif price_as_of != _expected_price_date(current):
            issues.append(
                "価格履歴の基準日が直近の確定取引日ではありません: "
                f"{price_as_of.isoformat()}"
            )

    rows = payload.get("searchUniverse")
    if not isinstance(rows, list) or not rows:
        issues.append("ランキング母集団が空です。")
    return issues


def analysis_freshness_issues(
    payload: object,
    *,
    candidate_payload: object,
    now: datetime | None = None,
) -> list[str]:
    if not isinstance(payload, dict):
        return ["日経225分析JSONがオブジェクトではありません。"]

    current = (now or datetime.now(tz=JST)).astimezone(JST)
    issues: list[str] = []
    generated_at = _parse_timestamp(payload.get("generatedAt"))
    if generated_at is None:
        issues.append("日経225分析のgeneratedAtが未設定またはタイムゾーンなしです。")
    elif generated_at.astimezone(JST).date() != current.date():
        issues.append(
            "日経225分析のgeneratedAtが当日ではありません: "
            f"{generated_at.astimezone(JST).date().isoformat()}"
        )

    price_source = payload.get("priceSource")
    analysis_as_of = None
    if not isinstance(price_source, dict):
        issues.append("日経225分析の価格取得状態がありません。")
    else:
        if price_source.get("status") != "available":
            issues.append("日経225分析の価格履歴がavailableではありません。")
        analysis_as_of = _parse_date(price_source.get("asOf"))
        if analysis_as_of is None:
            issues.append("日経225分析の基準日がありません。")

    rows = payload.get("rows")
    latest_row_date = None
    if not isinstance(rows, list) or not rows:
        issues.append("日経225分析の価格履歴が空です。")
    elif isinstance(rows[-1], dict):
        latest_row_date = _parse_date(rows[-1].get("date"))
        if latest_row_date is None:
            issues.append("日経225分析の最終行の日付が不正です。")
    else:
        issues.append("日経225分析の最終行が不正です。")

    expected_as_of = None
    if isinstance(candidate_payload, dict):
        sources = candidate_payload.get("sources")
        candidate_price = sources.get("priceHistory") if isinstance(sources, dict) else None
        if isinstance(candidate_price, dict):
            expected_as_of = _parse_date(candidate_price.get("asOf"))
    if expected_as_of is not None:
        if analysis_as_of != expected_as_of:
            issues.append(
                "日経225分析の基準日が候補データと一致しません: "
                f"analysis={analysis_as_of} candidates={expected_as_of}"
            )
        if latest_row_date != expected_as_of:
            issues.append(
                "日経225分析の最終足が候補データと一致しません: "
                f"analysis={latest_row_date} candidates={expected_as_of}"
            )
    return issues


def history_freshness_issues(
    payload: object,
    *,
    candidate_payload: object,
) -> list[str]:
    if not isinstance(payload, dict):
        return ["スコア履歴JSONがオブジェクトではありません。"]
    if not isinstance(candidate_payload, dict):
        return ["候補JSONがないためスコア履歴を照合できません。"]

    issues: list[str] = []
    if payload.get("scoreVersion") != candidate_payload.get("scoreVersion"):
        issues.append("スコア履歴のscoreVersionが候補データと一致しません。")
    if payload.get("factorVersion") != candidate_payload.get("factorVersion"):
        issues.append("スコア履歴のfactorVersionが候補データと一致しません。")

    snapshots = payload.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        issues.append("スコア履歴が空です。")
        return issues
    latest = snapshots[-1] if isinstance(snapshots[-1], dict) else None
    if latest is None:
        issues.append("スコア履歴の最終スナップショットが不正です。")
        return issues

    sources = candidate_payload.get("sources")
    price_source = sources.get("priceHistory") if isinstance(sources, dict) else None
    expected_date = _parse_date(price_source.get("asOf")) if isinstance(price_source, dict) else None
    history_date = _parse_date(latest.get("date"))
    if expected_date is not None and history_date != expected_date:
        issues.append(
            "スコア履歴の最新日が株価基準日と一致しません: "
            f"history={history_date} candidates={expected_date}"
        )

    current_rows = candidate_payload.get("searchUniverse")
    current_count = len(current_rows) if isinstance(current_rows, list) else 0
    history_count = latest.get("rowCount")
    if not isinstance(history_count, int) or history_count <= 0:
        issues.append("スコア履歴の最新スナップショットが空です。")
    elif current_count and history_count < int(current_count * 0.9):
        issues.append("スコア履歴の最新銘柄数が候補母集団の90%未満です。")
    return issues


def fetch_payload(url: str, *, timeout: float = 30.0) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "capital-gain-radar-freshness-check/1.0",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"公開JSONを確認できませんでした: {error}") from error
    if not isinstance(payload, dict):
        raise RuntimeError("公開JSONの形式が不正です。")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="公開済み株価データの当日鮮度を確認します。")
    parser.add_argument("--url", default=PUBLIC_DATA_URL)
    parser.add_argument("--analysis-url", default=PUBLIC_ANALYSIS_URL)
    parser.add_argument("--history-url", default=PUBLIC_SCORE_HISTORY_URL)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    try:
        payload = fetch_payload(args.url, timeout=args.timeout)
        analysis_payload = fetch_payload(args.analysis_url, timeout=args.timeout)
        history_payload = fetch_payload(args.history_url, timeout=args.timeout)
    except RuntimeError as error:
        print(f"STALE: {error}")
        return 1

    issues = freshness_issues(payload)
    issues.extend(
        analysis_freshness_issues(
            analysis_payload,
            candidate_payload=payload,
        )
    )
    issues.extend(history_freshness_issues(history_payload, candidate_payload=payload))
    if issues:
        print("STALE: " + " / ".join(issues))
        return 1
    print(
        "FRESH: "
        f"generatedAt={payload.get('generatedAt')} "
        f"priceAsOf={payload.get('sources', {}).get('priceHistory', {}).get('asOf')} "
        f"analysisAsOf={analysis_payload.get('priceSource', {}).get('asOf')}"
        f" historyLatest={history_payload.get('latestDate')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
