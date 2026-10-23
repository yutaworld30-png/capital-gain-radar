from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from market_environment import fetch_bytes, fetch_yahoo_point
from market_technical import build_technical_rows
from private_us_release import private_base, remote_json
from us_sector_market import collect_us_sectors


FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
FEAR_GREED_PAGE = "https://www.cnn.com/markets/fear-and-greed"
FEAR_GREED_PROVIDER = "cnn"
US_KEYS = ("sp500", "nasdaq", "sox", "vix", "us10y", "usdJpy", "wtiCrudeOil")
OUTPUT = Path(__file__).resolve().parents[1] / "outputs" / "data" / "us-market.json"


def parse_fear_greed(raw: object, *, generated_at: str, market_date: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "unavailable", "score": None, "asOf": None,
        "observedAt": None, "providerId": FEAR_GREED_PROVIDER,
        "source": "CNN Business Fear & Greed Index", "url": FEAR_GREED_PAGE,
        "history": [],
    }
    if not isinstance(raw, dict) or not isinstance(raw.get("fear_and_greed"), dict):
        return result
    reading = raw["fear_and_greed"]
    score = reading.get("score")
    stamp = reading.get("timestamp")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
        return result
    if not 0 <= score <= 100 or not isinstance(stamp, str):
        return result
    try:
        observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if observed.tzinfo is None or generated.tzinfo is None:
            return result
        observed = observed.astimezone(timezone.utc)
        generated = generated.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return result
    if observed.astimezone(ZoneInfo("America/New_York")).date().isoformat() != market_date:
        return result
    if observed > generated + timedelta(minutes=5) or generated - observed > timedelta(hours=12):
        return result
    value = round(float(score))
    labels = {"extreme fear": "強い恐怖", "fear": "恐怖", "neutral": "中立",
              "greed": "強欲", "extreme greed": "強い強欲"}
    label = labels.get(str(reading.get("rating") or "").lower())
    if label is None:
        return result
    history: dict[str, dict[str, Any]] = {}
    historical = raw.get("fear_and_greed_historical")
    historical_rows = historical.get("data") if isinstance(historical, dict) else None
    for row in historical_rows if isinstance(historical_rows, list) else []:
        if not isinstance(row, dict):
            continue
        x, y = row.get("x"), row.get("y")
        if (isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
                or isinstance(y, bool) or not isinstance(y, (int, float)) or not math.isfinite(y)
                or not 0 <= y <= 100):
            continue
        try:
            day = datetime.fromtimestamp(x / 1000, tz=timezone.utc).date().isoformat()
        except (OverflowError, ValueError, OSError):
            continue
        if day <= market_date:
            history[day] = {"date": day, "score": round(float(y))}
    result.update({"status": "available", "score": value, "label": label,
                   "asOf": market_date, "observedAt": observed.isoformat(),
                   "history": [history[day] for day in sorted(history)[-93:]]})
    return result


def build_us_market(*, points: dict[str, Any], sectors: dict[str, Any],
                    fear_greed: dict[str, Any], previous: dict[str, Any],
                    generated_at: str) -> dict[str, Any]:
    core_dates = [points[key].as_of for key in ("sp500", "nasdaq", "vix")]
    if any(not day for day in core_dates) or len(set(core_dates)) != 1:
        raise ValueError(f"米国主要指数の確定日が揃っていません: {core_dates}")
    market_date = core_dates[0]
    previous_date = str(previous.get("asOf") or "")
    if previous_date and market_date < previous_date:
        raise ValueError(f"米国市場データの日付が逆戻りしました: {market_date} < {previous_date}")
    if sectors.get("status") != "available" or sectors.get("asOf") != market_date:
        raise ValueError("米国業種ランキングが主要指数と同じ取引日で揃っていません。")

    generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    vix_history = points["vix"].history
    ny_time = generated.astimezone(ZoneInfo("America/New_York"))
    if ny_time.time() < time(16, 15):
        vix_history = [row for row in vix_history if row.get("date") != ny_time.date().isoformat()]
    vix_rows = build_technical_rows(vix_history)
    if not vix_rows or vix_rows[-1]["date"] != market_date:
        raise ValueError("VIXの日足が米国主要指数の確定日と一致しません。")

    previous_fear = previous.get("fearGreed")
    previous_history = previous.get("fearGreedHistory", []) if (
        isinstance(previous_fear, dict) and previous_fear.get("providerId") == FEAR_GREED_PROVIDER
    ) else []
    if not isinstance(previous_history, list):
        previous_history = []
    history = {str(row.get("date")): row for row in previous_history
               if isinstance(row, dict) and isinstance(row.get("date"), str)
               and isinstance(row.get("score"), int) and 0 <= row["score"] <= 100}
    for row in fear_greed.get("history", []):
        if isinstance(row, dict) and isinstance(row.get("date"), str) and row["date"] <= market_date:
            history[row["date"]] = row
    fear_greed = {key: value for key, value in fear_greed.items() if key != "history"}
    if fear_greed.get("status") == "available" and fear_greed.get("asOf") == market_date:
        history[market_date] = {"date": market_date, "score": fear_greed["score"]}
    else:
        fear_greed = {**fear_greed, "status": "unavailable", "score": None,
                      "note": "CNNの同じ米国取引日の値を確認できませんでした。"}
    return {
        "schemaVersion": 1, "generatedAt": generated_at, "asOf": market_date,
        "indicators": {key: points[key].to_dict() for key in US_KEYS},
        "vixTechnical": {
            "status": "available" if len(vix_rows) >= 35 else "partial",
            "asOf": market_date, "source": points["vix"].source,
            "url": points["vix"].url,
            "note": "確定済みの日足OHLCからMACD(12,26,9)を計算。",
            "rows": vix_rows[-260:],
        },
        "usSectors": sectors,
        "fearGreed": fear_greed,
        "fearGreedHistory": [history[key] for key in sorted(history)[-93:]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="米国引け後の相場データを生成")
    parser.add_argument("--previous-url", default=os.getenv("PRIVATE_US_MARKET_URL", ""))
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    previous: dict[str, Any] = {}
    if args.previous_url:
        parsed = urlparse(args.previous_url)
        base = private_base(f"{parsed.scheme}://{parsed.netloc}")
        if parsed.path != "/data/us-market.json" or parsed.query or parsed.fragment:
            raise ValueError("前回米国市場データのURLが想定と異なります。")
        response = remote_json(base, "data/us-market.json", optional=True)
        if response is None:
            print("No previous US market data; starting history.")
        else:
            previous = response[1]
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    points = {
        "sp500": fetch_yahoo_point("sp500", "S&P500", ["^GSPC"]),
        "nasdaq": fetch_yahoo_point("nasdaq", "NASDAQ", ["^IXIC"]),
        "sox": fetch_yahoo_point("sox", "SOX", ["^SOX"]),
        "vix": fetch_yahoo_point("vix", "VIX", ["^VIX"]),
        "us10y": fetch_yahoo_point("us10y", "米10年金利", ["^TNX"], unit="%"),
        "usdJpy": fetch_yahoo_point("usdJpy", "ドル円", ["JPY=X"], unit="円"),
        "wtiCrudeOil": fetch_yahoo_point("wtiCrudeOil", "WTI原油", ["CL=F"], unit="ドル"),
    }
    sectors = collect_us_sectors(fetch_yahoo_point, generated_at)
    try:
        raw_fear = json.loads(fetch_bytes(FEAR_GREED_URL).decode("utf-8"))
    except (ValueError, OSError, RuntimeError):
        raw_fear = None
    market_date = str(points["sp500"].as_of or "")
    fear_greed = parse_fear_greed(raw_fear, generated_at=generated_at, market_date=market_date)
    payload = build_us_market(points=points, sectors=sectors, fear_greed=fear_greed,
                              previous=previous, generated_at=generated_at)
    old_fear = previous.get("fearGreed") if isinstance(previous.get("fearGreed"), dict) else {}
    new_fear = payload["fearGreed"]
    changed = (previous.get("asOf") != payload["asOf"]
               or old_fear.get("status") != new_fear.get("status")
               or old_fear.get("score") != new_fear.get("score")
               or old_fear.get("providerId") != new_fear.get("providerId"))
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as output:
            output.write(f"should_deploy={'true' if changed else 'false'}\n")
    if not changed:
        print(f"US market already current: {payload['asOf']}")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"US market: {payload['asOf']}, sectors={sectors.get('coveredCount')}, fear-greed={fear_greed['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
