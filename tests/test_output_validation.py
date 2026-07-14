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
        components = [{"code": f"{1000 + index:04d}", "name": f"fixture-{index}"} for index in range(225)]
        sources = {
            key: {"status": "available", "asOf": today.isoformat()}
            for key in ("nikkei225", "primeMarket", "marginWeekly", "priceHistory", "themeNews", "fundamentals")
        }
        return {
            "schemaVersion": 2,
            "universe": {"id": "nikkei225", "expectedCount": 225},
            "scoreVersion": "2.1.0",
            "factorVersion": "nikkei225-capital-gain-v2.1",
            "priceBasis": "adjusted-ohlc",
            "highLookbackDays": 252,
            "nikkei225Components": components,
            "sources": sources,
            "searchUniverse": [{
                "code": "1000",
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
                "scoreVersion": "2.1.0",
                "factorVersion": "nikkei225-capital-gain-v2.1",
                "priceBasis": "adjusted-ohlc",
                "highLookbackDays": 252,
            }],
        }

    def test_valid_dataset_passes(self) -> None:
        self.assertEqual(validate_dataset(self.valid_dataset(), today=date(2026, 7, 14)), [])

    def test_stale_source_and_wrong_component_count_fail(self) -> None:
        payload = self.valid_dataset()
        payload["nikkei225Components"] = payload["nikkei225Components"][:-1]
        payload["sources"]["priceHistory"]["asOf"] = (date(2026, 7, 14) - timedelta(days=8)).isoformat()

        errors = validate_dataset(payload, today=date(2026, 7, 14))

        self.assertTrue(any("225件" in error for error in errors))
        self.assertTrue(any("7日超" in error for error in errors))

    def test_missing_persisted_score_fails(self) -> None:
        payload = self.valid_dataset()
        payload["searchUniverse"][0].pop("score")

        self.assertTrue(any("score" in error for error in validate_dataset(payload, today=date(2026, 7, 14))))

    def test_history_rejects_mixed_factor_versions(self) -> None:
        dataset = self.valid_dataset()
        history = {
            "schemaVersion": 2,
            "scoreVersion": dataset["scoreVersion"],
            "factorVersion": dataset["factorVersion"],
            "snapshots": [{"scoreVersion": "old", "factorVersion": "old"}],
        }

        self.assertTrue(validate_history(history, dataset))


if __name__ == "__main__":
    unittest.main()
