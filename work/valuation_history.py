from __future__ import annotations

import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError


BASIS = "observed-edinet-valuation-v1"
FIELDS = ["date", "per", "pbr", "dividendYield", "dividendYieldKind", "fundamentalsAsOf", "observedAt"]
RETENTION = 1260


def _value(value: object, *, maximum: float, zero: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), 6) if (0 <= value if zero else 0 < value) and value <= maximum else None


def _record(row: object, fields: list[str] = FIELDS) -> list[Any] | None:
    if isinstance(row, list):
        row = dict(zip(fields, row))
    if not isinstance(row, dict):
        return None
    try:
        day = date.fromisoformat(str(row.get("date"))).isoformat()
    except ValueError:
        return None
    per = _value(row.get("per"), maximum=1000)
    pbr = _value(row.get("pbr"), maximum=100)
    forecast = row.get("dividendYieldKind") == "forecast"
    dividend = _value(row.get("dividendYield"), maximum=.25, zero=True) if forecast else None
    if per is None and pbr is None and dividend is None:
        return None
    return [day, per, pbr, dividend, "forecast" if dividend is not None else None, row.get("fundamentalsAsOf"), row.get("observedAt")]


def update_valuation_history(
    data_dir: Path, history: dict[str, Any], generated_at: str, *,
    remote_base: str = "", loader: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    records: dict[str, dict[str, list[Any]]] = {}

    def merge_shard(payload: dict[str, Any]) -> None:
        if payload.get("schemaVersion") != 1 or payload.get("basis") != BASIS or payload.get("rowFormat") != FIELDS:
            raise ValueError("バリュエーション履歴の形式・算出基準が一致しません。")
        stocks = payload.get("stocks")
        if not isinstance(stocks, dict):
            raise ValueError("バリュエーション履歴の銘柄一覧が不正です。")
        for code, rows in stocks.items():
            if not re.fullmatch(r"[0-9A-Z]{4}", code) or not isinstance(rows, list):
                raise ValueError("バリュエーション履歴の銘柄コードが不正です。")
            for raw in rows:
                row = _record(raw)
                if row is None:
                    raise ValueError("保存済みバリュエーション履歴に不正な観測値があります。")
                records.setdefault(code, {})[row[0]] = row

    manifest_path = data_dir / "valuation-history-v1.json"
    sources: list[tuple[dict[str, Any], bool]] = []
    if manifest_path.exists():
        sources.append((json.loads(manifest_path.read_text(encoding="utf-8")), False))
    if remote_base and loader:
        try:
            published = loader(f"{remote_base}/latest-candidates.json")
            if not isinstance(published, dict):
                raise ValueError("公開データの形式を確認できません。")
            expected = bool(published.get("valuationHistorySummary"))
            try:
                restored = loader(f"{remote_base}/valuation-history-v1.json")
            except (HTTPError, ValueError) as error:
                # A first-release Pages SPA can return HTML rather than a 404.
                if expected or (isinstance(error, HTTPError) and error.code != 404):
                    raise RuntimeError("公開済みの長期履歴を復元できません。") from None
            else:
                sources.append((restored, True))
        except HTTPError as error:
            raise RuntimeError("過去のバリュエーション履歴を復元できないため更新を停止しました。") from None
        except (OSError, ValueError, RuntimeError):
            raise RuntimeError("過去のバリュエーション履歴を復元できないため更新を停止しました。") from None
    for manifest, remote in sources:
        if manifest.get("basis") != BASIS or manifest.get("schemaVersion") != 1:
            raise ValueError("バリュエーション履歴の基準が不明です。")
        paths = [entry.get("url", "") for entry in manifest.get("shards", [])]
        if not all(re.fullmatch(r"valuation-history/[0-9A-Z]{2}\.json", path) for path in paths):
            raise ValueError("バリュエーション履歴の保存先が不正です。")

        def load(path: str) -> dict[str, Any]:
            return loader(f"{remote_base}/{path}") if remote and loader else json.loads((data_dir / path).read_text(encoding="utf-8"))

        with ThreadPoolExecutor(max_workers=4) as pool:
            for payload in pool.map(load, paths):
                merge_shard(payload)

    fields = history.get("rowFormat") or []
    for snapshot in history.get("snapshots", []):
        # Reconstructed scores are not evidence of historical financial ratios.
        if snapshot.get("historySource") != "daily-pipeline":
            continue
        for raw in snapshot.get("rows", []):
            decoded = dict(zip(fields, raw)) if isinstance(raw, list) else raw
            if not isinstance(decoded, dict):
                continue
            code = str(decoded.get("code") or "")
            row = _record({**decoded, "date": snapshot.get("date"), "observedAt": snapshot.get("generatedAt")})
            if row and re.fullmatch(r"[0-9A-Z]{4}", code):
                records.setdefault(code, {})[row[0]] = row

    shards: dict[str, dict[str, list[list[Any]]]] = {}
    for code, by_date in records.items():
        retained = [by_date[day] for day in sorted(by_date)][-RETENTION:]
        shards.setdefault(code[:2], {})[code] = retained
    manifest = {"schemaVersion": 1, "basis": BASIS, "generatedAt": generated_at,
                "retentionObservations": RETENTION, "stockCount": len(records), "shards": [],
                "note": "実際に保存された指標の観測履歴のみ。未保存の過去財務や当時の予想値は復元しません。"}
    (data_dir / "valuation-history").mkdir(parents=True, exist_ok=True)

    def write(path: Path, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(text.encode("utf-8")) > 20_000_000:
            raise ValueError("バリュエーション履歴が配信上限を超えるため保存を停止しました。")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)

    for prefix, stocks in sorted(shards.items()):
        url = f"valuation-history/{prefix}.json"
        write(data_dir / url, {"schemaVersion": 1, "basis": BASIS, "rowFormat": FIELDS, "stocks": stocks})
        manifest["shards"].append({"url": url, "stockCount": len(stocks),
                                    "pointCount": sum(len(rows) for rows in stocks.values())})
    write(manifest_path, manifest)
    return manifest


def validate_valuation_bundle(data_dir: Path) -> list[str]:
    errors = []
    try:
        manifest = json.loads((data_dir / "valuation-history-v1.json").read_text(encoding="utf-8"))
        if manifest.get("schemaVersion") != 1 or manifest.get("basis") != BASIS:
            return ["バリュエーション履歴の基準・形式が不正です。"]
        codes = set()
        for entry in manifest.get("shards", []):
            url = entry.get("url", "")
            if not re.fullmatch(r"valuation-history/[0-9A-Z]{2}\.json", url):
                return ["バリュエーション履歴の参照先が不正です。"]
            payload = json.loads((data_dir / url).read_text(encoding="utf-8"))
            if payload.get("rowFormat") != FIELDS or payload.get("basis") != BASIS or payload.get("schemaVersion") != 1:
                errors.append("バリュエーション履歴の列・基準が一致しません。")
            stocks = payload.get("stocks", {})
            for code, rows in stocks.items():
                if not re.fullmatch(r"[0-9A-Z]{4}", code) or not code.startswith(Path(url).stem) or code in codes:
                    errors.append("バリュエーション履歴の銘柄コード・保存先が一致しません。")
                codes.add(code)
                if not rows or len(rows) > RETENTION or any(_record(row) != row for row in rows):
                    errors.append("バリュエーション履歴の数値・件数が不正です。")
                dates = [row[0] for row in rows]
                if dates != sorted(set(dates)):
                    errors.append("バリュエーション履歴の日付が重複または逆順です。")
            if len(stocks) != entry.get("stockCount") or sum(map(len, stocks.values())) != entry.get("pointCount"):
                errors.append("バリュエーション履歴の件数が一致しません。")
        if len(codes) != manifest.get("stockCount"):
            errors.append("バリュエーション履歴の銘柄総数が一致しません。")
    except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError):
        errors.append("バリュエーション履歴JSONを検証できません。")
    return errors
