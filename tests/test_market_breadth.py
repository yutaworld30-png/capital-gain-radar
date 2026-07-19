from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from market_breadth import build_nikkei225_breadth  # noqa: E402


class MarketBreadthTests(unittest.TestCase):
    def test_daily_counts_and_rolling_ratio(self) -> None:
        records = [
            {
                "code": "1001",
                "chartHistory": [
                    {"date": "2026-01-01", "close": 100},
                    {"date": "2026-01-02", "close": 101},
                    {"date": "2026-01-03", "close": 102},
                ],
            },
            {
                "code": "1002",
                "chartHistory": [
                    {"date": "2026-01-01", "close": 100},
                    {"date": "2026-01-02", "close": 99},
                    {"date": "2026-01-03", "close": 98},
                ],
            },
            {
                "code": "1003",
                "chartHistory": [
                    {"date": "2026-01-01", "close": 100},
                    {"date": "2026-01-02", "close": 100},
                    {"date": "2026-01-03", "close": 101},
                ],
            },
        ]
        result = build_nikkei225_breadth(records, expected_count=3, ratio_period=2)
        latest = result["rows"][-1]
        self.assertEqual(result["status"], "available")
        self.assertEqual(latest["advances"], 2)
        self.assertEqual(latest["declines"], 1)
        self.assertEqual(latest["unchanged"], 0)
        self.assertEqual(latest["advanceDeclineRatio25"], 150.0)
        self.assertEqual(latest["coverageRate"], 1.0)

    def test_zero_declines_does_not_create_infinite_ratio(self) -> None:
        records = [{
            "code": "1001",
            "chartHistory": [
                {"date": "2026-01-01", "close": 100},
                {"date": "2026-01-02", "close": 101},
                {"date": "2026-01-03", "close": 102},
            ],
        }]
        result = build_nikkei225_breadth(records, expected_count=1, ratio_period=2)
        self.assertIsNone(result["rows"][-1]["advanceDeclineRatio25"])

    def test_missing_history_is_explicit(self) -> None:
        result = build_nikkei225_breadth([], expected_count=225)
        self.assertEqual(result["status"], "insufficient-data")
        self.assertEqual(result["rows"], [])


if __name__ == "__main__":
    unittest.main()
