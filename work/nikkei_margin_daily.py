"""Aggregate verified per-issue margin observations for current Nikkei 225 members."""

import json
import math
from datetime import date
from pathlib import Path
from typing import Any

from margin_history import validate


JPX_MARGIN_URL = "https://www.jpx.co.jp/markets/statistics-equities/margin/01.html"


def _nonnegative(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def build_nikkei_margin_daily(
    history: dict[str, Any],
    components: list[dict[str, Any]],
    *,
    price_closes: dict[str, dict[str, float]] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    validate(history)
    codes = {str(item.get("code", "")) for item in components if isinstance(item, dict)}
    if len(codes) != 225:
        return {"status": "unavailable", "rows": [], "note": "日経225構成225銘柄を確認できません。"}
    grouped: dict[str, dict[str, Any]] = {}
    for code in codes:
        for observation in history["stocks"].get(code, []):
            buy = _nonnegative(observation.get("buy"))
            sell = _nonnegative(observation.get("sell"))
            if buy is None or sell is None:
                continue
            day = str(observation["date"])
            entry = grouped.setdefault(day, {
                "buy": 0.0, "sell": 0.0, "buyYen": 0.0, "sellYen": 0.0,
                "codes": set(), "pricedCodes": set(), "publishedAt": set(),
            })
            entry["buy"] += buy
            entry["sell"] += sell
            entry["codes"].add(code)
            close = _nonnegative((price_closes or {}).get(code, {}).get(day))
            if close is not None and close > 0:
                entry["buyYen"] += buy * close
                entry["sellYen"] += sell * close
                entry["pricedCodes"].add(code)
            if observation.get("publishedAt"):
                entry["publishedAt"].add(str(observation["publishedAt"]))
    rows = []
    for day, entry in sorted(grouped.items()):
        covered = len(entry["codes"])
        if covered / len(codes) < 0.95:
            continue
        buy = entry["buy"] / 1000
        sell = entry["sell"] / 1000
        value_covered = len(entry["pricedCodes"])
        enough_prices = value_covered / len(codes) >= 0.95
        rows.append({
            "date": day,
            "buyBalanceThousandShares": round(buy, 3),
            "sellBalanceThousandShares": round(sell, 3),
            "marginRatio": round(buy / sell, 4) if sell > 0 else None,
            "buyBalance100mYen": round(entry["buyYen"] / 100_000_000, 2) if enough_prices else None,
            "sellBalance100mYen": round(entry["sellYen"] / 100_000_000, 2) if enough_prices else None,
            "valueCoveredCount": value_covered,
            "coveredCount": covered,
            "componentCount": len(codes),
            "publishedAt": max(entry["publishedAt"]) if entry["publishedAt"] else None,
        })
    latest = rows[-1] if rows else None
    current = today or date.today()
    age = (current - date.fromisoformat(latest["date"])).days if latest else None
    status = "available" if age is not None and 0 <= age <= 7 else "stale-fallback" if latest else "unavailable"
    return {
        "status": status,
        "asOf": latest["date"] if latest else None,
        "url": JPX_MARGIN_URL,
        "scope": "日経225現行構成銘柄の銘柄別信用残高合計",
        "unit": "thousand-shares-and-100m-yen",
        "valuePriceBasis": "same-day-unadjusted-close",
        "frequency": "daily-with-earlier-weekly-history",
        "note": (
            "過去も現在の構成225銘柄で集計した参考値です。金額は同日終値×株数、価格確認率95%未満の日は非算出。2026年9月以前など日次未蓄積の期間は週次観測のみです。"
            if status == "available"
            else "最新の日次集計がありません。過去の観測値を最新値として扱わないでください。"
        ),
        "rows": rows[-260:],
    }


def load_nikkei_margin_daily(data_dir: Path) -> dict[str, Any]:
    try:
        dataset = json.loads((data_dir / "latest-candidates.json").read_text(encoding="utf-8"))
        history = json.loads((data_dir / "margin-history-v1.json").read_text(encoding="utf-8"))
        closes: dict[str, dict[str, float]] = {}
        codes = {str(item.get("code")) for item in dataset.get("nikkei225Components", []) if isinstance(item, dict)}
        for shard_path in sorted((data_dir / "price-history").glob("topix-*.json")):
            shard = json.loads(shard_path.read_text(encoding="utf-8"))
            if shard.get("generatedAt") != dataset.get("generatedAt"):
                continue
            for price in shard.get("prices", []):
                code = str(price.get("code"))
                if code not in codes:
                    continue
                closes[code] = {
                    str(row[0]): float(row[4])
                    for row in price.get("chartHistory", [])
                    if isinstance(row, list) and len(row) >= 5 and _nonnegative(row[4]) is not None
                }
        return build_nikkei_margin_daily(history, dataset.get("nikkei225Components", []), price_closes=closes)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {"status": "unavailable", "rows": [], "note": f"日次信用残高を集計できません（{type(error).__name__}）。"}
