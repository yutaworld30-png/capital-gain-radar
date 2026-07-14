from __future__ import annotations

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
    SCORE_VERSION,
    _attach_event_summaries,
    _attach_history_changes,
    _score_explanation,
)
from free_market_connector import _normalize_event_date, fetch_yahoo_dividend_forecast  # noqa: E402
from jquants_connector import calculate_price_metrics  # noqa: E402


class DecisionSupportTest(unittest.TestCase):
    def test_atr_recent_low_and_stop_are_generated_from_adjusted_ohlc(self) -> None:
        rows = [
            {
                "Date": (date(2025, 1, 1) + timedelta(days=index)).isoformat(),
                "AdjH": 102.0,
                "AdjL": 98.0,
                "AdjC": 100.0,
                "Va": 2_000_000_000,
            }
            for index in range(252)
        ]

        metrics = calculate_price_metrics(rows)

        self.assertEqual(metrics["atr14"], 4.0)
        self.assertEqual(metrics["recentLow20"], 98.0)
        self.assertEqual(metrics["suggestedStopPrice"], 98.0)
        self.assertEqual(metrics["suggestedStopWidth"], 0.02)
        self.assertEqual(metrics["executionEase"], "標準")

    def test_yahoo_company_page_parses_events_with_dividend(self) -> None:
        page = """
        <div>1株配当 （会社予想） 用語 120.00 円 （2027/03）</div>
        <div>配当利回り （会社予想） 用語 2.50 % （2026/07/15）</div>
        <div>次回決算発表予定日 2026/07/30</div>
        <div>権利落ち日 9/28</div>
        """
        with patch("free_market_connector._fetch", return_value=page.encode("utf-8")):
            result = fetch_yahoo_dividend_forecast("9999")

        self.assertEqual(result["earningsAnnouncementDate"], "2026-07-30")
        self.assertEqual(result["exDividendDate"], _normalize_event_date("9/28"))
        self.assertEqual(result["dividendYield"], 0.025)

    def test_score_explanation_returns_three_items_per_group(self) -> None:
        item = {
            "theme": 80,
            "supply": 70,
            "technical": 60,
            "relative": 90,
            "earnings": 55,
            "liquidity": 75,
            "valuation": 40,
            "risk": 30,
            "dataQuality": 85,
            "dataWarnings": ["PER未取得"],
            "dataAnomalies": [],
            "priceAsOf": "2026-07-15",
        }

        reasons = _score_explanation(item)

        self.assertEqual(len(reasons["positive"]), 3)
        self.assertEqual(len(reasons["negative"]), 3)
        self.assertEqual(len(reasons["quality"]), 3)
        self.assertIn("総合へ", reasons["positive"][0]["text"])

    def test_history_change_detects_rank_score_high_and_quality(self) -> None:
        previous_rows = [
            {"code": "A", "score": 70, "dataQuality": 95, "isNewHigh52w": False, "theme": 60},
            {"code": "B", "score": 75, "dataQuality": 90, "isNewHigh52w": False, "theme": 60},
        ]
        current_rows = [
            {"code": "A", "score": 80, "dataQuality": 80, "isNewHigh52w": True, "theme": 75},
            {"code": "B", "score": 70, "dataQuality": 90, "isNewHigh52w": False, "theme": 60},
        ]
        snapshots = [{
            "date": "2026-07-14",
            "scoreVersion": SCORE_VERSION,
            "factorVersion": "fixture",
            "rows": previous_rows,
        }]

        _attach_history_changes(current_rows, snapshots, "2026-07-15")

        first = current_rows[0]
        self.assertEqual(first["rank"], 1)
        self.assertEqual(first["previousRank"], 2)
        self.assertEqual(first["rankChange"], 1)
        self.assertEqual(first["scoreChange"], 10.0)
        self.assertIn("スコア急上昇 +10点", first["changeAlerts"])
        self.assertIn("52週高値を更新", first["changeAlerts"])
        self.assertTrue(any("データ品質" in alert for alert in first["changeAlerts"]))

    def test_event_summary_calculates_proximity_days(self) -> None:
        dataset = {"searchUniverse": [{
            "code": "A",
            "earningsAnnouncementDate": "2026-07-18",
            "importantDisclosures": [{"date": "2026-07-14", "title": "業績予想の修正", "url": "https://example.test"}],
        }]}

        _attach_event_summaries(dataset, "2026-07-15T16:10:00+09:00")

        row = dataset["searchUniverse"][0]
        self.assertEqual(row["nextEventDays"], 3)
        self.assertTrue(row["eventWarning"])
        self.assertEqual(row["events"][0]["daysFromNow"], -1)

    def test_frontend_contains_saved_filters_and_decision_panels(self) -> None:
        html = (ROOT / "outputs" / "investment-candidate-app.html").read_text(encoding="utf-8")

        self.assertIn("capitalGainRadar.filters.v1", html)
        self.assertIn('id="positiveReasons"', html)
        self.assertIn('id="eventList"', html)
        self.assertIn('id="tradeRiskSummary"', html)
        self.assertIn("function renderWatchAlerts", html)


if __name__ == "__main__":
    unittest.main()
