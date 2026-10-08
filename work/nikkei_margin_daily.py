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
            entry = grouped.setdefault(day, {"buy": 0.0, "sell": 0.0, "codes": set(), "publishedAt": set()})
            entry["buy"] += buy
            entry["sell"] += sell
            entry["codes"].add(code)
            if observation.get("publishedAt"):
                entry["publishedAt"].add(str(observation["publishedAt"]))
    rows = []
    for day, entry in sorted(grouped.items()):
        covered = len(entry["codes"])
        if covered / len(codes) < 0.95:
            continue
        buy = entry["buy"] / 1000
        sell = entry["sell"] / 1000
        rows.append({
            "date": day,
            "buyBalanceThousandShares": round(buy, 3),
            "sellBalanceThousandShares": round(sell, 3),
            "marginRatio": round(buy / sell, 4) if sell > 0 else None,
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
        "unit": "thousand-shares",
        "frequency": "daily-with-earlier-weekly-history",
        "note": (
            "過去も現在の構成225銘柄で集計した参考値です。2026年9月以前など日次未蓄積の期間は週次観測のみです。"
            if status == "available"
            else "最新の日次集計がありません。過去の観測値を最新値として扱わないでください。"
        ),
        "rows": rows[-260:],
    }


def load_nikkei_margin_daily(data_dir: Path) -> dict[str, Any]:
    try:
        dataset = json.loads((data_dir / "latest-candidates.json").read_text(encoding="utf-8"))
        history = json.loads((data_dir / "margin-history-v1.json").read_text(encoding="utf-8"))
        return build_nikkei_margin_daily(history, dataset.get("nikkei225Components", []))
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {"status": "unavailable", "rows": [], "note": f"日次信用残高を集計できません（{type(error).__name__}）。"}
