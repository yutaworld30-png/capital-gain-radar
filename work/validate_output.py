from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

from market_analysis import validate_analysis
from weekly_prediction import validate_accuracy_summary, validate_prediction_ledger


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "outputs" / "data" / "latest-candidates.json"
DEFAULT_HISTORY = ROOT / "outputs" / "data" / "score-history-v2.json"
DEFAULT_ANALYSIS = ROOT / "outputs" / "data" / "nikkei225-analysis.json"
DEFAULT_WEEKLY_PREDICTIONS = ROOT / "outputs" / "data" / "weekly-predictions-v1.json"
DEFAULT_WEEKLY_ACCURACY = ROOT / "outputs" / "data" / "weekly-accuracy-summary-v1.json"
REQUIRED_SOURCES = ("topix", "marginWeekly", "priceHistory", "themeNews", "fundamentals")
TOPIX_MIN_COMPONENTS = 1500
TOPIX_MAX_COMPONENTS = 2000


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
    expected_count = universe.get("expectedCount")
    if (
        universe.get("id") != "topix"
        or not isinstance(expected_count, int)
        or not (TOPIX_MIN_COMPONENTS <= expected_count <= TOPIX_MAX_COMPONENTS)
    ):
        errors.append("対象ユニバースが検証済みTOPIX構成として設定されていません。")
    if not payload.get("scoreVersion") or not payload.get("factorVersion"):
        errors.append("スコア計算版がありません。")
    if payload.get("priceBasis") != "adjusted-ohlc" or payload.get("highLookbackDays") != 252:
        errors.append("株価基準または52週高値の営業日数が契約と一致しません。")

    price_bundle = payload.get("priceHistoryBundle") if isinstance(payload.get("priceHistoryBundle"), dict) else {}
    price_shards = price_bundle.get("shards") if isinstance(price_bundle.get("shards"), list) else []
    minimum_price_count = round(int(expected_count or 0) * 0.95)
    if (
        price_bundle.get("status") != "available"
        or price_bundle.get("generatedAt") != payload.get("generatedAt")
        or price_bundle.get("scoreVersion") != payload.get("scoreVersion")
        or price_bundle.get("factorVersion") != payload.get("factorVersion")
        or price_bundle.get("priceBasis") != payload.get("priceBasis")
        or price_bundle.get("highLookbackDays") != payload.get("highLookbackDays")
        or not isinstance(price_bundle.get("recordCount"), int)
        or int(price_bundle.get("recordCount") or 0) < minimum_price_count
        or not price_shards
    ):
        errors.append("遅延読込用のTOPIX価格履歴契約が不完全です。")
    elif any(
        not isinstance(item, dict)
        or not re.fullmatch(r"data/price-history/topix-[0-9A-Z_]+\.json", str(item.get("url") or ""))
        or not isinstance(item.get("recordCount"), int)
        for item in price_shards
    ):
        errors.append("TOPIX価格履歴の分割ファイル一覧が不正です。")

    components = payload.get("topixComponents")
    if not isinstance(components, list) or len(components) != expected_count:
        errors.append("TOPIX構成銘柄数がユニバース契約と一致しません。")
        component_codes: set[str] = set()
    else:
        component_codes = {str(item.get("code")) for item in components if isinstance(item, dict)}
        if len(component_codes) != expected_count:
            errors.append("TOPIX構成銘柄コードに重複または欠損があります。")

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
            errors.append(f"{code or index + 1}: TOPIX構成銘柄外です。")
        if row.get("isTopix") is not True:
            errors.append(f"{code or index + 1}: TOPIX所属フラグがありません。")
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


def validate_price_history_files(dataset: dict[str, object], site_root: Path) -> list[str]:
    bundle = dataset.get("priceHistoryBundle") if isinstance(dataset.get("priceHistoryBundle"), dict) else {}
    shards = bundle.get("shards") if isinstance(bundle.get("shards"), list) else []
    errors: list[str] = []
    seen_codes: set[str] = set()
    total_rows = 0
    resolved_root = site_root.resolve()
    for entry in shards:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "")
        if not re.fullmatch(r"data/price-history/topix-[0-9A-Z_]+\.json", url):
            continue
        path = (site_root / Path(url)).resolve()
        if resolved_root not in path.parents:
            errors.append(f"価格履歴ファイルが公開フォルダー外を参照しています: {url}")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"価格履歴ファイルを読み込めません: {url} ({error})")
            continue
        prices = payload.get("prices") if isinstance(payload, dict) and isinstance(payload.get("prices"), list) else []
        if (
            payload.get("generatedAt") != dataset.get("generatedAt")
            or payload.get("scoreVersion") != dataset.get("scoreVersion")
            or payload.get("factorVersion") != dataset.get("factorVersion")
            or payload.get("priceBasis") != dataset.get("priceBasis")
            or payload.get("highLookbackDays") != dataset.get("highLookbackDays")
            or len(prices) != entry.get("recordCount")
        ):
            errors.append(f"価格履歴ファイルの生成版または件数が一致しません: {url}")
            continue
        for item in prices:
            code = str(item.get("code") or "") if isinstance(item, dict) else ""
            history = item.get("chartHistory") if isinstance(item, dict) else None
            if not code or code in seen_codes:
                errors.append(f"価格履歴の銘柄コードが欠損または重複しています: {url}")
                break
            if not isinstance(history, list) or len(history) < 50 or any(
                not isinstance(row, list) or len(row) != 6 for row in history
            ):
                errors.append(f"価格履歴のOHLCV配列が不完全です: {code}")
                break
            seen_codes.add(code)
        total_rows += len(prices)
    if total_rows != bundle.get("recordCount") or len(seen_codes) != total_rows:
        errors.append("価格履歴の分割ファイル合計が契約件数と一致しません。")
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
        return errors
    if any(
        not isinstance(item, dict)
        or item.get("scoreVersion") != dataset.get("scoreVersion")
        or item.get("factorVersion") != dataset.get("factorVersion")
        for item in snapshots
    ):
        errors.append("スコア履歴に異なる計算版のスナップショットが混在しています。")
    if not snapshots:
        errors.append("スコア履歴のスナップショットが空です。")
        return errors

    dates = [str(item.get("date") or "") for item in snapshots if isinstance(item, dict)]
    if not all(_as_date(item) for item in dates):
        errors.append("スコア履歴に不正な日付があります。")
    if dates != sorted(dates):
        errors.append("スコア履歴の日付が昇順ではありません。")
    if len(dates) != len(set(dates)):
        errors.append("スコア履歴に同一日付が重複しています。")

    if payload.get("snapshotCount") != len(snapshots):
        errors.append("スコア履歴のsnapshotCountが実データ件数と一致しません。")
    restored_count = payload.get("restoredSnapshotCount")
    if isinstance(restored_count, int) and len(snapshots) < restored_count:
        errors.append("スコア履歴が復元時より減少しています。")

    latest = snapshots[-1] if isinstance(snapshots[-1], dict) else {}
    latest_date = dates[-1] if dates else ""
    if payload.get("latestDate") != latest_date:
        errors.append("スコア履歴のlatestDateが最終スナップショットと一致しません。")
    sources = dataset.get("sources")
    price_source = sources.get("priceHistory") if isinstance(sources, dict) else None
    expected_date = price_source.get("asOf") if isinstance(price_source, dict) else None
    if expected_date and latest_date != expected_date:
        errors.append(
            "スコア履歴の最新日が候補データの株価基準日と一致しません: "
            f"history={latest_date} candidates={expected_date}"
        )

    current_rows = dataset.get("searchUniverse")
    current_count = len(current_rows) if isinstance(current_rows, list) else 0
    latest_count = latest.get("rowCount") if isinstance(latest, dict) else None
    if not isinstance(latest_count, int) or latest_count <= 0:
        errors.append("スコア履歴の最新スナップショットが空です。")
    elif current_count and latest_count < int(current_count * 0.9):
        errors.append("スコア履歴の最新銘柄数が現在母集団の90%未満です。")

    coverage = payload.get("coverage")
    if not isinstance(coverage, dict):
        errors.append("スコア履歴の取得率メタデータがありません。")
    else:
        available_count = coverage.get("availableCount")
        expected_count = coverage.get("expectedCount")
        if (
            not isinstance(available_count, int)
            or not isinstance(expected_count, int)
            or available_count < 0
            or expected_count < 0
            or available_count > expected_count
        ):
            errors.append("スコア履歴の取得率メタデータが不正です。")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub Pages公開前の生成JSON品質チェック")
    parser.add_argument("dataset", nargs="?", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--analysis", type=Path, default=DEFAULT_ANALYSIS)
    parser.add_argument("--weekly-predictions", type=Path, default=DEFAULT_WEEKLY_PREDICTIONS)
    parser.add_argument("--weekly-accuracy", type=Path, default=DEFAULT_WEEKLY_ACCURACY)
    args = parser.parse_args()
    try:
        dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"ERROR: 候補JSONを読み込めません: {error}")
        return 1
    errors = validate_dataset(dataset)
    errors.extend(validate_price_history_files(dataset, args.dataset.parent.parent))
    try:
        history = json.loads(args.history.read_text(encoding="utf-8"))
        errors.extend(validate_history(history, dataset))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"スコア履歴JSONを読み込めません: {error}")
    try:
        analysis = json.loads(args.analysis.read_text(encoding="utf-8"))
        errors.extend(
            f"日経225分析: {error}"
            for error in validate_analysis(analysis, public_only=True)
        )
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"日経225分析JSONを読み込めません: {error}")
    try:
        weekly_predictions = json.loads(args.weekly_predictions.read_text(encoding="utf-8"))
        errors.extend(validate_prediction_ledger(weekly_predictions, dataset))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"週5%予測台帳JSONを読み込めません: {error}")
        weekly_predictions = None
    try:
        weekly_accuracy = json.loads(args.weekly_accuracy.read_text(encoding="utf-8"))
        if isinstance(weekly_predictions, dict):
            errors.extend(validate_accuracy_summary(weekly_accuracy, weekly_predictions))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"週5%予測実績JSONを読み込めません: {error}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("OK: TOPIX候補・スコア履歴・週5%予測・日経225分析JSONの品質チェックに合格しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
