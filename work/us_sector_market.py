from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timezone
from math import isfinite
from typing import Any, Callable
from zoneinfo import ZoneInfo


SECTORS = {
    "XLK": "情報技術", "XLF": "金融", "XLV": "ヘルスケア", "XLY": "一般消費財",
    "XLP": "生活必需品", "XLE": "エネルギー", "XLI": "資本財・サービス",
    "XLB": "素材", "XLU": "公益事業", "XLRE": "不動産", "XLC": "通信サービス",
}
SECTOR_SOURCE = "https://www.ssga.com/us/en/individual/capabilities/equities/sector-investing/select-sector-etfs"


def build_us_sectors(points: dict[str, Any], generated_at: str) -> dict[str, Any]:
    current = datetime.fromisoformat(generated_at.replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York"))
    cutoff = current.date().isoformat()
    candidates = []
    for symbol, label in SECTORS.items():
        point = points.get(symbol)
        rows = []
        for row in getattr(point, "history", []):
            day, value = str(row.get("date") or ""), row.get("value")
            if not day or day > cutoff or (day == cutoff and current.time() < time(16, 15)):
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value > 0:
                rows.append({"date": day, "value": value})
        rows = sorted({row["date"]: row for row in rows}.values(), key=lambda row: row["date"])
        if len(rows) < 2:
            continue
        previous, latest = rows[-2:]
        candidates.append({"industry": label, "symbol": symbol, "asOf": latest["date"],
                           "previousAsOf": previous["date"], "close": latest["value"],
                           "changeRate": latest["value"] / previous["value"] - 1,
                           "sourceUrl": getattr(point, "url", "")})
    pairs = Counter((row["asOf"], row["previousAsOf"]) for row in candidates)
    pair = max(pairs, key=lambda key: (pairs[key], key)) if pairs else None
    rows = [row for row in candidates if (row["asOf"], row["previousAsOf"]) == pair] if pair else []
    # Do not rank a handful of fresh ETFs against a different session's returns.
    if len(rows) < 8:
        rows = []
    rows.sort(key=lambda row: (-row["changeRate"], row["symbol"]))
    age = (current.date() - datetime.fromisoformat(pair[0]).date()).days if pair else None
    return {"schemaVersion": 1, "generatedAt": generated_at, "asOf": pair[0] if rows else None,
            "status": "available" if rows and age is not None and age <= 7 else "stale" if rows else "unavailable",
            "market": "US", "source": "Yahoo Finance / Select Sector SPDR ETF", "sourceUrl": SECTOR_SOURCE,
            "basis": "S&P500の11セクターに対応する米国上場ETFの確定終値比。米国株全体の業種平均・公式業種指数ではありません。",
            "coveredCount": len(rows), "sectorCount": len(SECTORS),
            "missingSymbols": [symbol for symbol in SECTORS if symbol not in {row["symbol"] for row in rows}],
            "rows": rows}


def collect_us_sectors(fetch_point: Callable[..., Any], generated_at: str) -> dict[str, Any]:
    def collect(symbol: str) -> tuple[str, Any]:
        try:
            return symbol, fetch_point(f"usSector{symbol}", SECTORS[symbol], [symbol], unit="ドル")
        except (ValueError, TypeError, RuntimeError, OSError):
            return symbol, None
    with ThreadPoolExecutor(max_workers=4) as pool:
        points = dict(pool.map(collect, SECTORS))
    return build_us_sectors(points, generated_at)
