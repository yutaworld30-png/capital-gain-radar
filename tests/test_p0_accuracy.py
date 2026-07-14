from __future__ import annotations

import json
import sys
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))
sys.modules.setdefault(
    "xlrd",
    types.SimpleNamespace(XLRDError=Exception, open_workbook=lambda *args, **kwargs: None),
)

from fetch_official_data import (  # noqa: E402
    FACTOR_VERSION,
    HIGH_LOOKBACK_DAYS,
    PRICE_BASIS,
    SCORE_VERSION,
    _attach_scores,
    _data_quality,
    _supply_score,
    _total_score,
    _valuation_score,
    scoring_contract_metadata,
)
from free_market_connector import fetch_yahoo_spark_histories  # noqa: E402
from jquants_connector import JQuantsError, calculate_price_metrics  # noqa: E402


FIXTURE_PATH = ROOT / "tests" / "fixtures" / "p0_representative.json"


def load_fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def build_price_rows(recipe: dict[str, object]) -> list[dict[str, object]]:
    count = int(recipe["days"])
    start = date(2025, 1, 1)
    rows: list[dict[str, object]] = []
    for index in range(count):
        high = float(recipe["baseHigh"])
        close = float(recipe["baseClose"])
        if index == 0:
            high = float(recipe["excludedHigh"])
        if index == count - 1:
            high = float(recipe["latestHigh"])
            close = float(recipe["latestClose"])
        rows.append({
            "Date": (start + timedelta(days=index)).isoformat(),
            "AdjH": high,
            "AdjC": close,
            "Va": 1_000_000_000,
        })
    return rows


def legacy_total_score(item: dict[str, object]) -> int:
    liquidity = int(item.get("liquidity") or 60)
    relative = int(item.get("relative") or 60)
    earnings = int(item.get("earnings") or 50)
    risk = int(item.get("risk") or 50)
    valuation = int(item.get("valuation") or _valuation_score(item))
    base_score = round(
        int(item.get("theme") or 0) * 0.20
        + _supply_score(item) * 0.20
        + int(item.get("technical") or 0) * 0.15
        + relative * 0.13
        + earnings * 0.13
        + liquidity * 0.07
        + valuation * 0.08
        + (100 - risk) * 0.04
    )
    quality_score, _warnings = _data_quality(item)
    quality_penalty = max(0, 80 - quality_score) * 0.20
    return max(0, min(100, round(base_score - quality_penalty)))


def representative_score_report() -> list[dict[str, object]]:
    report: list[dict[str, object]] = []
    for stock in load_fixture()["stocks"]:
        rows = build_price_rows(stock["priceRecipe"])
        metrics = calculate_price_metrics(rows)
        latest_close = float(metrics["latestClose"])
        new_item = {
            **stock["factors"],
            "monthsFromHigh": metrics["monthsFromHigh"],
            "high52wDistance": (float(metrics["high52w"]) - latest_close) / float(metrics["high52w"]),
            "latestClose": latest_close,
        }
        legacy_high = max(float(row["AdjH"]) for row in rows)
        legacy_item = {
            **new_item,
            "high52wDistance": (legacy_high - latest_close) / legacy_high,
        }
        report.append({
            "code": stock["code"],
            "name": stock["name"],
            "legacyScore": legacy_total_score(legacy_item),
            "newScore": _total_score(new_item),
            "legacyHigh": legacy_high,
            "newHigh": metrics["high52w"],
        })
    legacy_order = {
        str(item["code"]): index + 1
        for index, item in enumerate(sorted(report, key=lambda item: int(item["legacyScore"]), reverse=True))
    }
    new_order = {
        str(item["code"]): index + 1
        for index, item in enumerate(sorted(report, key=lambda item: int(item["newScore"]), reverse=True))
    }
    for item in report:
        item["legacyRank"] = legacy_order[str(item["code"])]
        item["newRank"] = new_order[str(item["code"])]
    return report


class P0AccuracyTest(unittest.TestCase):
    def test_52_week_high_excludes_253rd_trading_day(self) -> None:
        stock = load_fixture()["stocks"][0]
        metrics = calculate_price_metrics(build_price_rows(stock["priceRecipe"]))

        self.assertEqual(metrics["highLookbackDays"], 252)
        self.assertEqual(metrics["high52w"], 120.0)
        self.assertEqual(metrics["latestHigh"], 120.0)
        self.assertTrue(metrics["isNewHigh52w"])

    def test_intraday_high_not_close_controls_breakout(self) -> None:
        stock = load_fixture()["stocks"][0]
        metrics = calculate_price_metrics(build_price_rows(stock["priceRecipe"]))

        self.assertEqual(metrics["latestClose"], 118.0)
        self.assertGreater(metrics["latestHigh"], metrics["latestClose"])
        self.assertTrue(metrics["isNewHigh52w"])

    def test_price_metrics_require_full_252_trading_days(self) -> None:
        stock = load_fixture()["stocks"][0]
        rows = build_price_rows(stock["priceRecipe"])[-251:]

        with self.assertRaises(JQuantsError):
            calculate_price_metrics(rows)

    def test_spark_close_only_rows_do_not_claim_intraday_high(self) -> None:
        payload = {
            "spark": {
                "result": [{
                    "symbol": "TEST.T",
                    "response": [{
                        "timestamp": [1735689600],
                        "indicators": {"quote": [{"close": [100.0]}]},
                        "meta": {"regularMarketVolume": 1000},
                    }],
                }],
            },
        }
        with patch("free_market_connector._fetch", return_value=json.dumps(payload).encode("utf-8")):
            rows = fetch_yahoo_spark_histories(["TEST"])["TEST"]["rows"]

        self.assertNotIn("AdjH", rows[0])
        self.assertNotIn("H", rows[0])

    def test_zero_factor_values_are_not_replaced_by_neutral_defaults(self) -> None:
        stock = load_fixture()["stocks"][2]
        metrics = calculate_price_metrics(build_price_rows(stock["priceRecipe"]))
        item = {
            **stock["factors"],
            "monthsFromHigh": metrics["monthsFromHigh"],
            "high52wDistance": (metrics["high52w"] - metrics["latestClose"]) / metrics["high52w"],
            "latestClose": metrics["latestClose"],
        }

        self.assertLess(_total_score(item), 30)

    def test_generated_rows_carry_versioned_scoring_contract(self) -> None:
        stock = load_fixture()["stocks"][1]
        metrics = calculate_price_metrics(build_price_rows(stock["priceRecipe"]))
        row = {
            **stock["factors"],
            "code": stock["code"],
            "name": stock["name"],
            "monthsFromHigh": metrics["monthsFromHigh"],
            "high52wDistance": (metrics["high52w"] - metrics["latestClose"]) / metrics["high52w"],
            "latestClose": metrics["latestClose"],
        }
        dataset = {"searchUniverse": [row], "candidates": []}

        _attach_scores(dataset)

        self.assertEqual(row["scoreVersion"], SCORE_VERSION)
        self.assertEqual(row["factorVersion"], FACTOR_VERSION)
        self.assertEqual(row["priceBasis"], PRICE_BASIS)
        self.assertEqual(row["highLookbackDays"], HIGH_LOOKBACK_DAYS)
        self.assertIsInstance(row["score"], int)

    def test_dataset_contract_is_nikkei225_only(self) -> None:
        contract = scoring_contract_metadata()

        self.assertEqual(contract["schemaVersion"], 2)
        self.assertEqual(contract["universe"]["id"], "nikkei225")
        self.assertEqual(contract["universe"]["expectedCount"], 225)
        self.assertEqual(contract["priceBasis"], "adjusted-ohlc")
        self.assertEqual(contract["highLookbackDays"], 252)

    def test_frontend_uses_only_persisted_scores(self) -> None:
        html = (ROOT / "outputs" / "investment-candidate-app.html").read_text(encoding="utf-8")

        self.assertIn('return persistedScore(item, "score")', html)
        self.assertIn('return persistedScore(item, "supply")', html)
        self.assertIn('return persistedScore(item, "valuation")', html)
        self.assertNotIn("item.theme * 0.20", html)
        self.assertNotIn('state.universe === "prime"', html)

    def test_representative_fixture_high_boundary_is_deterministic(self) -> None:
        report = {str(item["code"]): item for item in representative_score_report()}

        self.assertEqual(report["TEST-A"]["legacyHigh"], 180.0)
        self.assertEqual(report["TEST-A"]["newHigh"], 120.0)
        self.assertEqual(report["TEST-A"]["newScore"], report["TEST-A"]["legacyScore"])
        self.assertLess(report["TEST-C"]["newScore"], report["TEST-C"]["legacyScore"])


if __name__ == "__main__":
    unittest.main()
