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
from fetch_official_data import (  # noqa: E402
    _data_quality,
    _data_quality_details,
    _detect_anomalies,
    _metric_basis,
    _supply_score,
    _total_score,
)


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

    def test_anomaly_detection_flags_unrealistic_metrics(self) -> None:
        item = {
            **self.base_item(),
            "per": -1.0,
            "pbr": 0.0,
            "roe": 1.2,
            "dividendYield": 0.18,
            "dividendPayoutRatio": 3.5,
        }

        anomalies = _detect_anomalies(item)

        self.assertGreaterEqual(len(anomalies), 5)

    def test_quality_details_separate_missing_sources_from_anomalies(self) -> None:
        item = self.base_item()
        item.pop("dividendYield")
        item["sources"] = {}

        details, issues = _data_quality_details(item)

        self.assertEqual(len(details), 4)
        self.assertTrue(any("配当" in issue for issue in issues))
        self.assertFalse(any("15%" in issue for issue in issues))

    def test_metric_basis_labels_forecast_dividend(self) -> None:
        item = {
            **self.base_item(),
            "dividendYieldKind": "forecast",
            "dpsSource": "Yahoo Finance 1株配当（会社予想）",
            "dividendPayoutRatioStatus": "not-calculated-mixed-basis",
        }

        basis = _metric_basis(item)

        self.assertEqual(basis["dividendYield"], "Yahoo Finance会社予想")
        self.assertEqual(basis["dividendPayoutRatio"], "非算出: 予想DPSと実績EPSが混在")


if __name__ == "__main__":
    unittest.main()
