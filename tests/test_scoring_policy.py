from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))
sys.modules.setdefault(
    "xlrd",
    types.SimpleNamespace(XLRDError=Exception, open_workbook=lambda *args, **kwargs: None),
)

from edinet_connector import calculate_valuation_metrics  # noqa: E402
from fetch_official_data import _data_quality, _supply_score, _total_score  # noqa: E402


class ScoringPolicyTest(unittest.TestCase):
    def base_item(self) -> dict[str, object]:
        return {
            "theme": 60,
            "margin": 1.0,
            "monthsFromHigh": 0.1,
            "high52wDistance": 0.0,
            "technical": 80,
            "liquidity": 80,
            "relative": 80,
            "earnings": 70,
            "risk": 40,
            "latestClose": 1000.0,
            "per": 15.0,
            "pbr": 1.2,
            "roe": 0.12,
            "dividendYield": 0.03,
        }

    def test_supply_score_rewards_near_52_week_high(self) -> None:
        near_high = self.base_item()
        far_from_high = {**near_high, "monthsFromHigh": 18.0, "high52wDistance": 0.30}

        self.assertGreater(_supply_score(near_high), _supply_score(far_from_high))

    def test_missing_fundamentals_lower_data_quality_and_total_score(self) -> None:
        complete = self.base_item()
        incomplete = self.base_item()
        for key in ("per", "pbr", "roe", "dividendYield"):
            incomplete.pop(key)

        complete_quality, _ = _data_quality(complete)
        incomplete_quality, warnings = _data_quality(incomplete)

        self.assertGreater(complete_quality, incomplete_quality)
        self.assertTrue(warnings)
        self.assertGreater(_total_score(complete), _total_score(incomplete))

    def test_forecast_dps_does_not_mix_into_payout_ratio(self) -> None:
        metrics = calculate_valuation_metrics(
            {
                "eps": 100.0,
                "bps": 1000.0,
                "dps": 50.0,
                "dpsSource": "Yahoo Finance 1株配当（会社予想）",
                "dividendYield": 0.025,
                "dividendYieldKind": "forecast",
            },
            2000.0,
        )

        self.assertEqual(metrics["dividendYield"], 0.025)
        self.assertIsNone(metrics["dividendPayoutRatio"])
        self.assertEqual(metrics["dividendPayoutRatioStatus"], "not-calculated-mixed-basis")


if __name__ == "__main__":
    unittest.main()
