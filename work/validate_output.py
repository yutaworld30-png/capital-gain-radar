from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "outputs" / "data" / "latest-candidates.json"
DEFAULT_HISTORY = ROOT / "outputs" / "data" / "score-history-v2.json"
REQUIRED_SOURCES = ("nikkei225", "primeMarket", "marginWeekly", "priceHistory", "themeNews", "fundamentals")


def _as_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10].replace("/", "-"))
    except ValueError:
        return None


def validate_dataset(payload: object, *, today: date | None = None) -> list[str]:
    if not isinstance(payload, dict):
        return ["ルートがJSONオブジェクトではありません。"]
    today = today or date.today()
    errors: list[str] = []
    universe = payload.get("universe") if isinstance(payload.get("universe"), dict) else {}
    if payload.get("schemaVersion") != 2:
        errors.append("schemaVersionが2ではありません。")
    if universe.get("id") != "nikkei225" or universe.get("expectedCount") != 225:
        errors.append("対象ユニバースが日経225として固定されていません。")
    if not payload.get("scoreVersion") or not payload.get("factorVersion"):
        errors.append("スコア計算版がありません。")
    if payload.get("priceBasis") != "adjusted-ohlc" or payload.get("highLookbackDays") != 252:
        errors.append("株価基準または52週高値の営業日数が契約と一致しません。")

    components = payload.get("nikkei225Components")
    if not isinstance(components, list) or len(components) != 225:
        errors.append("日経225構成銘柄が225件ではありません。")
        component_codes: set[str] = set()
    else:
        component_codes = {str(item.get("code")) for item in components if isinstance(item, dict)}
        if len(component_codes) != 225:
            errors.append("日経225構成銘柄コードに重複または欠損があります。")

    sources = payload.get("sources") if isinstance(payload.get("sources"), dict) else {}
    for key in REQUIRED_SOURCES:
        source = sources.get(key) if isinstance(sources.get(key), dict) else {}
        if source.get("status") != "available" or source.get("refreshStatus") == "error":
            errors.append(f"必須取得元 {key} が最新の確認済み状態ではありません。")
    for key, max_age in (("priceHistory", 7), ("marginWeekly", 14)):
        source = sources.get(key) if isinstance(sources.get(key), dict) else {}
        source_date = _as_date(source.get("asOf") or source.get("updatedAt"))
        if source_date is None or (today - source_date).days > max_age:
            errors.append(f"必須取得元 {key} の基準日が不明または{max_age}日超です。")

    rows = payload.get("searchUniverse")
    if not isinstance(rows, list) or not rows:
        errors.append("ランキング対象銘柄がありません。")
        rows = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"ランキング{index + 1}行目がオブジェクトではありません。")
            continue
        code = str(row.get("code") or "")
        if component_codes and code not in component_codes:
            errors.append(f"{code or index + 1}: 日経225構成銘柄外です。")
        for key in ("score", "supply", "valuation", "dataQuality"):
            value = row.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"{code}: {key}がPython生成済み数値ではありません。")
        reasons = row.get("scoreReasons") if isinstance(row.get("scoreReasons"), dict) else {}
        if any(not isinstance(reasons.get(key), list) or len(reasons[key]) != 3 for key in ("positive", "negative", "quality")):
            errors.append(f"{code}: 加点・減点・品質理由が各3件生成されていません。")
        if not isinstance(row.get("rank"), int):
            errors.append(f"{code}: 現在順位がPythonで生成されていません。")
        if row.get("scoreVersion") != payload.get("scoreVersion") or row.get("factorVersion") != payload.get("factorVersion"):
            errors.append(f"{code}: スコア計算版がデータセットと一致しません。")
        if row.get("priceBasis") != "adjusted-ohlc" or row.get("highLookbackDays") != 252:
            errors.append(f"{code}: 52週高値の計算基準が一致しません。")
    return errors


def validate_history(payload: object, dataset: dict[str, object]) -> list[str]:
    if not isinstance(payload, dict):
        return ["スコア履歴のルートがJSONオブジェクトではありません。"]
    errors: list[str] = []
    if payload.get("schemaVersion") != 2:
        errors.append("スコア履歴のschemaVersionが2ではありません。")
    if payload.get("scoreVersion") != dataset.get("scoreVersion") or payload.get("factorVersion") != dataset.get("factorVersion"):
        errors.append("スコア履歴の計算版が最新候補データと一致しません。")
    snapshots = payload.get("snapshots")
    if not isinstance(snapshots, list):
        errors.append("スコア履歴のsnapshotsが配列ではありません。")
    elif any(
        not isinstance(item, dict)
        or item.get("scoreVersion") != dataset.get("scoreVersion")
        or item.get("factorVersion") != dataset.get("factorVersion")
        for item in snapshots
    ):
        errors.append("スコア履歴に異なる計算版のスナップショットが混在しています。")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub Pages公開前の生成JSON品質チェック")
    parser.add_argument("dataset", nargs="?", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    args = parser.parse_args()
    try:
        dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"ERROR: 候補JSONを読み込めません: {error}")
        return 1
    errors = validate_dataset(dataset)
    try:
        history = json.loads(args.history.read_text(encoding="utf-8"))
        errors.extend(validate_history(history, dataset))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"スコア履歴JSONを読み込めません: {error}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("OK: 日経225候補JSONとスコア履歴JSONの品質チェックに合格しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
