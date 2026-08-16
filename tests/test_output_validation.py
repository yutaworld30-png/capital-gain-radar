from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from validate_output import validate_dataset, validate_history  # noqa: E402


class OutputValidationTest(unittest.TestCase):
    def valid_dataset(self) -> dict[str, object]:
        today = date(2026, 7, 14)
        components = [{"code": f"{1000 + index:04d}", "name": f"fixture-{index}"} for index in range(1500)]
        sources = {
            key: {"status": "available", "asOf": today.isoformat()}
            for key in ("topix", "marginWeekly", "priceHistory", "themeNews", "fundamentals")
        }
        return {
            "schemaVersion": 2,
            "generatedAt": "2026-07-14T16:10:00+09:00",
            "universe": {"id": "topix", "expectedCount": 1500},
            "scoreVersion": "3.0.0",
            "factorVersion": "topix-capital-gain-v3.0",
            "priceBasis": "adjusted-ohlc",
            "highLookbackDays": 252,
            "topixComponents": components,
            "priceHistoryBundle": {
                "status": "available",
                "generatedAt": "2026-07-14T16:10:00+09:00",
                "scoreVersion": "3.0.0",
                "factorVersion": "topix-capital-gain-v3.0",
                "priceBasis": "adjusted-ohlc",
                "highLookbackDays": 252,
                "recordCount": 1500,
                "shards": [{
                    "key": "1",
                    "url": "data/price-history/topix-1.json",
                    "recordCount": 1500,
                }],
            },
            "sources": sources,
            "searchUniverse": [{
                "code": "1000",
                "isTopix": True,
                "score": 70,
                "supply": 80,
                "valuation": 60,
                "dataQuality": 90,
                "rank": 1,
                "scoreReasons": {
                    "positive": ["a", "b", "c"],
                    "negative": ["a", "b", "c"],
                    "quality": ["a", "b", "c"],
                },
                "scoreVersion": "3.0.0",
                "factorVersion": "topix-capital-gain-v3.0",
                "priceBasis": "adjusted-ohlc",
                "highLookbackDays": 252,
            }],
        }

    def valid_history(self) -> dict[str, object]:
        return {
            "schemaVersion": 2,
            "scoreVersion": "3.0.0",
            "factorVersion": "topix-capital-gain-v3.0",
            "snapshotCount": 1,
            "restoredSnapshotCount": 0,
            "latestDate": "2026-07-14",
            "coverage": {
                "availableCount": 1,
                "expectedCount": 1,
                "coverageRate": 100.0,
            },
            "snapshots": [{
                "date": "2026-07-14",
                "scoreVersion": "3.0.0",
                "factorVersion": "topix-capital-gain-v3.0",
                "rowCount": 1,
                "rows": [{"code": "1000", "score": 70}],
            }],
        }

    def test_valid_dataset_passes(self) -> None:
        self.assertEqual(validate_dataset(self.valid_dataset(), today=date(2026, 7, 14)), [])

    def test_stale_source_and_wrong_component_count_fail(self) -> None:
        payload = self.valid_dataset()
        payload["topixComponents"] = payload["topixComponents"][:-1]
        payload["sources"]["priceHistory"]["asOf"] = (date(2026, 7, 14) - timedelta(days=8)).isoformat()

        errors = validate_dataset(payload, today=date(2026, 7, 14))

        self.assertTrue(any("ユニバース契約" in error for error in errors))
        self.assertTrue(any("7日超" in error for error in errors))

    def test_missing_persisted_score_fails(self) -> None:
        payload = self.valid_dataset()
        payload["searchUniverse"][0].pop("score")

        self.assertTrue(any("score" in error for error in validate_dataset(payload, today=date(2026, 7, 14))))

    def test_missing_price_history_bundle_fails(self) -> None:
        payload = self.valid_dataset()
        payload.pop("priceHistoryBundle")

        self.assertTrue(any("価格履歴契約" in error for error in validate_dataset(payload, today=date(2026, 7, 14))))

    def test_history_rejects_mixed_factor_versions(self) -> None:
        dataset = self.valid_dataset()
        history = {
            "schemaVersion": 2,
            "scoreVersion": dataset["scoreVersion"],
            "factorVersion": dataset["factorVersion"],
            "snapshots": [{"scoreVersion": "old", "factorVersion": "old"}],
        }

        self.assertTrue(validate_history(history, dataset))

    def test_valid_history_passes(self) -> None:
        self.assertEqual(validate_history(self.valid_history(), self.valid_dataset()), [])

    def test_history_rejects_snapshot_loss(self) -> None:
        history = self.valid_history()
        history["restoredSnapshotCount"] = 2

        errors = validate_history(history, self.valid_dataset())

        self.assertTrue(any("復元時より減少" in error for error in errors))

    def test_history_rejects_latest_date_mismatch(self) -> None:
        history = self.valid_history()
        history["latestDate"] = "2026-07-13"
        history["snapshots"][0]["date"] = "2026-07-13"

        errors = validate_history(history, self.valid_dataset())

        self.assertTrue(any("株価基準日と一致しません" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
