"""Listing membership shared by collection and release validation."""
from __future__ import annotations

import re
from typing import Any


UNIVERSE_ID = "topix-standard"
UNIVERSE_LABEL = "TOPIX・東証スタンダード"
TOPIX_MIN_COMPONENTS = 1500
TOPIX_MAX_COMPONENTS = 2000
STANDARD_MIN_COMPONENTS = 1000
STANDARD_MAX_COMPONENTS = 2000


def screening_components(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    universe = dataset.get("universe")
    if not isinstance(universe, dict):
        return []
    extended = universe.get("id") == UNIVERSE_ID
    rows = dataset.get("listedComponents" if extended else "topixComponents")
    return rows if isinstance(rows, list) else []


def listing_errors(dataset: dict[str, Any]) -> list[str]:
    universe = dataset.get("universe") or {}
    if not isinstance(universe, dict):
        return ["対象ユニバースの形式が不正です。"]
    extended = universe.get("id") == UNIVERSE_ID
    if not extended and universe.get("id") != "topix":
        return ["対象ユニバースが対応していません。"]
    topix = dataset.get("topixComponents")
    if not isinstance(topix, list) or not TOPIX_MIN_COMPONENTS <= len(topix) <= TOPIX_MAX_COMPONENTS:
        return ["TOPIX構成銘柄数がユニバース契約と一致しません。"]

    def codes(rows: list[dict[str, Any]]) -> set[str]:
        return {str(row.get("code") or "") for row in rows if isinstance(row, dict)}

    groups = [topix]
    rows = topix
    if extended:
        standard = dataset.get("standardComponents")
        if not isinstance(standard, list) or not STANDARD_MIN_COMPONENTS <= len(standard) <= STANDARD_MAX_COMPONENTS:
            return ["スタンダード構成銘柄数を確認できません。"]
        rows = screening_components(dataset)
        groups.extend([standard, rows])
        topix_codes, standard_codes = codes(topix), codes(standard)
        if codes(rows) != topix_codes | standard_codes:
            return ["対象銘柄一覧がTOPIXとスタンダードの重複除外後の集合と一致しません。"]
        if any(not isinstance(row, dict) or row.get("exchangeSection") != "スタンダード"
               or row.get("isStandard") is not True for row in standard):
            return ["スタンダード一覧の市場区分が一致しません。"]
        if any(not isinstance(row, dict) or row.get("isTopix") != (str(row.get("code")) in topix_codes)
               or row.get("isStandard") != (str(row.get("code")) in standard_codes) for row in rows):
            return ["対象銘柄の所属フラグが公式一覧と一致しません。"]
    if any(len(codes(group)) != len(group) or any(not re.fullmatch(r"[0-9A-Z]{4}", code)
           for code in codes(group)) for group in groups):
        return ["対象銘柄コードに重複・欠損・不正値があります。"]
    if not isinstance(universe.get("expectedCount"), int) or isinstance(universe.get("expectedCount"), bool) or universe.get("expectedCount") != len(rows):
        return ["対象銘柄数がユニバース契約と一致しません。"]
    return []


def price_coverage_by_group(dataset: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    verified = {str(row.get("code")) for row in records}
    groups = {"topix": dataset.get("topixComponents", [])}
    if (dataset.get("universe") or {}).get("id") == UNIVERSE_ID:
        groups["standard"] = dataset.get("standardComponents", [])
    result = {}
    for key, rows in groups.items():
        codes = {str(row["code"]) for row in rows}
        count = len(codes & verified)
        result[key] = {"expectedCount": len(codes), "verifiedCount": count,
                       "coverage": count / len(codes) if codes else 0}
    return result
