from __future__ import annotations

import copy
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from weekly_prediction import (  # noqa: E402
    build_accuracy_summary,
    evaluate_prediction,
    record_data_hash,
    update_prediction_ledger,
    validate_accuracy_summary,
    validate_prediction_ledger,
)
from weekly_target import attach_weekly_targets  # noqa: E402


def history_until_signal() -> list[dict[str, object]]:
    rows = []
    start = date(2026, 1, 1)
    for index in range(100):
        close = 700 + index + (3 if index % 2 else -3)
        rows.append({
            "date": (start + timedelta(days=index)).isoformat(),
            "open": close - 1,
            "high": close + 4,
            "low": close - 4,
            "close": close,
            "volume": 2_000_000 if index == 99 else 1_000_000,
        })
    return rows


def candidate_row() -> dict[str, object]:
    signal_date = history_until_signal()[-1]["date"]
    return {
        "code": "9999",
        "name": "fixture",
        "industry": "機械",
        "latestClose": 802,
        "priceAsOf": signal_date,
        "score": 80,
        "dataQuality": 90,
        "relative": 90,
        "atr14": 25,
        "averageTurnover20": 2_000_000_000,
        "high52wDistance": 0.01,
        "margin": 1.5,
        "salesGrowth": 0.12,
        "profitGrowth": 0.25,
        "roe": 0.15,
        "suggestedStopWidth": 0.02,
        "events": [],
    }


def prediction_record() -> dict[str, object]:
    return {
        "predictionId": "fixture",
        "signalDate": "2026-07-01",
        "state": "pending",
        "execution": {"costBps": 20},
        "outcome": None,
    }


def future_rows(values: list[tuple[float | None, float, float, float]]) -> list[dict[str, object]]:
    rows = [{"date": "2026-07-01", "open": 90, "high": 91, "low": 89, "close": 90, "volume": 1}]
    for index, (open_price, high, low, close) in enumerate(values, start=1):
        rows.append({
            "date": (date(2026, 7, 1) + timedelta(days=index)).isoformat(),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1,
        })
    return rows


class WeeklyPredictionTests(unittest.TestCase):
    def test_target_stop_expired_and_ambiguous_outcomes(self) -> None:
        target = evaluate_prediction(
            prediction_record(),
            future_rows([(100, 106, 99, 104)]),
            as_of=date(2026, 7, 2),
        )
        stop = evaluate_prediction(
            prediction_record(),
            future_rows([(100, 102, 97, 98)]),
            as_of=date(2026, 7, 2),
        )
        ambiguous = evaluate_prediction(
            prediction_record(),
            future_rows([(100, 106, 97, 101)]),
            as_of=date(2026, 7, 2),
        )
        expired = evaluate_prediction(
            prediction_record(),
            future_rows([
                (100, 103, 99, 101),
                (101, 104, 99, 102),
                (102, 104, 99, 103),
                (103, 104, 99, 102),
                (102, 104, 99, 103),
            ]),
            as_of=date(2026, 7, 6),
        )

        self.assertEqual(target["outcome"]["label"], "target-first")
        self.assertEqual(target["outcome"]["entryPrice"], 100)
        self.assertEqual(stop["outcome"]["label"], "stop-first")
        self.assertEqual(ambiguous["outcome"]["label"], "ambiguous")
        self.assertFalse(ambiguous["outcome"]["primaryEligible"])
        self.assertEqual(ambiguous["outcome"]["conservativeLabel"], "stop-first")
        self.assertEqual(expired["outcome"]["label"], "expired")
        self.assertEqual(expired["outcome"]["observedSessions"], 5)

    def test_four_sessions_stay_pending_and_missing_next_open_is_unavailable(self) -> None:
        pending = evaluate_prediction(
            prediction_record(),
            future_rows([(100, 104, 99, 101)] * 4),
            as_of=date(2026, 7, 5),
        )
        missing_open = evaluate_prediction(
            prediction_record(),
            future_rows([(None, 104, 99, 101)]),
            as_of=date(2026, 7, 2),
        )

        self.assertEqual(pending["state"], "pending")
        self.assertEqual(missing_open["state"], "unavailable")
        self.assertIn("始値", missing_open["unavailableReason"])

    def test_same_signal_is_idempotent_and_evaluated_record_is_immutable(self) -> None:
        row = candidate_row()
        dataset = {
            "scoreVersion": "3.0.0",
            "factorVersion": "topix-capital-gain-v3.0",
            "priceBasis": "adjusted-ohlc",
            "topixPrices": [{"code": "9999", "chartHistory": history_until_signal()}],
            "searchUniverse": [row],
            "candidates": [],
        }
        attach_weekly_targets(dataset)
        first = update_prediction_ledger(dataset, "2026-04-10T16:10:00+09:00")
        second = update_prediction_ledger(dataset, "2026-04-10T19:10:00+09:00", first)

        self.assertGreater(first["recordCount"], 0)
        self.assertEqual(second["recordCount"], first["recordCount"])
        self.assertEqual(
            {record["predictionId"] for record in second["records"]},
            {record["predictionId"] for record in first["records"]},
        )

        evaluated = prediction_record()
        evaluated = evaluate_prediction(
            evaluated,
            future_rows([(100, 106, 99, 104)]),
            as_of=date(2026, 7, 2),
        )
        frozen = copy.deepcopy(evaluated)
        evaluate_prediction(
            evaluated,
            future_rows([(100, 101, 96, 97)]),
            as_of=date(2026, 7, 2),
        )
        self.assertEqual(evaluated, frozen)

    def test_summary_excludes_ambiguous_from_primary_rate_and_builds_conditions(self) -> None:
        records = []
        for label in ("target-first", "stop-first", "expired", "ambiguous"):
            record = {
                "predictionId": label,
                "predictionVersion": "weekly-prediction-v1.0",
                "labelVersion": "next-open-5sessions-target-stop-v1.0",
                "weeklyTargetVersion": "weekly-5pct-v1.0",
                "scoreVersion": "3.0.0",
                "factorVersion": "topix-capital-gain-v3.0",
                "priceBasis": "adjusted-ohlc",
                "signalDate": "2026-07-01",
                "code": "9999",
                "name": "fixture",
                "industry": "機械",
                "profile": "single",
                "statusAtSignal": "watch",
                "sampleKind": "candidate",
                "weeklyScore": 75,
                "state": "evaluated",
                "featureSnapshot": {
                    "strictChecks": {"trend": True},
                    "marketEnvironment": {"label": "中立"},
                },
                "outcome": {
                    "label": label,
                    "outcomeDate": "2026-07-05",
                    "netReturn": None if label == "ambiguous" else 0.01,
                    "mfe": 0.05,
                    "mae": -0.02,
                },
            }
            records.append(record)
        ledger = {
            "predictionVersion": "weekly-prediction-v1.0",
            "labelVersion": "next-open-5sessions-target-stop-v1.0",
            "weeklyTargetVersion": "weekly-5pct-v1.0",
            "scoreVersion": "3.0.0",
            "factorVersion": "topix-capital-gain-v3.0",
            "priceBasis": "adjusted-ohlc",
            "execution": {},
            "records": records,
        }

        summary = build_accuracy_summary(ledger, "2026-07-06T16:10:00+09:00")
        single = summary["profiles"]["single"]

        self.assertEqual(single["primaryEvaluatedCount"], 3)
        self.assertAlmostEqual(single["targetFirstRate"], 1 / 3, places=4)
        self.assertAlmostEqual(single["conservativeTargetRate"], 1 / 4, places=4)
        self.assertTrue(single["conditions"]["scoreBands"])
        self.assertTrue(single["conditions"]["strictChecks"])

    def test_json_contract_detects_duplicate_hash_and_future_feature_date(self) -> None:
        row = candidate_row()
        dataset = {
            "scoreVersion": "3.0.0",
            "factorVersion": "topix-capital-gain-v3.0",
            "priceBasis": "adjusted-ohlc",
            "topixPrices": [{"code": "9999", "chartHistory": history_until_signal()}],
            "searchUniverse": [row],
            "candidates": [],
        }
        attach_weekly_targets(dataset)
        ledger = update_prediction_ledger(dataset, "2026-04-10T16:10:00+09:00")
        summary = build_accuracy_summary(ledger, "2026-04-10T16:10:00+09:00")

        self.assertEqual(validate_prediction_ledger(ledger, dataset), [])
        self.assertEqual(validate_accuracy_summary(summary, ledger), [])

        invalid = copy.deepcopy(ledger)
        invalid["records"].append(copy.deepcopy(invalid["records"][0]))
        invalid["recordCount"] += 1
        invalid["records"][0]["featureSnapshot"]["priceAsOf"] = "2099-01-01"
        invalid["records"][0]["dataHash"] = record_data_hash(invalid["records"][0])
        errors = validate_prediction_ledger(invalid, dataset)

        self.assertTrue(any("重複" in error for error in errors))
        self.assertTrue(any("未来" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
