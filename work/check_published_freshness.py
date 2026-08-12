from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


PUBLIC_DATA_URL = (
    "https://yutaworld30-png.github.io/"
    "capital-gain-radar/data/latest-candidates.json"
)
JST = ZoneInfo("Asia/Tokyo")


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
        elif current.weekday() < 5 and price_as_of != current.date():
            issues.append(
                "価格履歴の基準日が当日ではありません: "
                f"{price_as_of.isoformat()}"
            )

    rows = payload.get("searchUniverse")
    if not isinstance(rows, list) or not rows:
        issues.append("ランキング母集団が空です。")
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
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    try:
        payload = fetch_payload(args.url, timeout=args.timeout)
    except RuntimeError as error:
        print(f"STALE: {error}")
        return 1

    issues = freshness_issues(payload)
    if issues:
        print("STALE: " + " / ".join(issues))
        return 1
    print(
        "FRESH: "
        f"generatedAt={payload.get('generatedAt')} "
        f"priceAsOf={payload.get('sources', {}).get('priceHistory', {}).get('asOf')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
