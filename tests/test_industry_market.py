import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "work"))
from fetch_official_data import build_industry_market, _compact_score_row


class IndustryMarketTests(unittest.TestCase):
    def test_equal_weight_same_date_and_coverage(self):
        dataset = {
            "topixComponents": [
                {"code": "1001", "industry": "機械"},
                {"code": "1002", "industry": "機械"},
                {"code": "1003", "industry": "化学"},
                {"code": "1004", "industry": "化学"},
            ],
            "topixPrices": [
                {"code": "1001", "asOf": "2026-10-15", "chartHistory": [
                    {"date": "2026-10-14", "close": 100}, {"date": "2026-10-15", "close": 110}]},
                {"code": "1002", "asOf": "2026-10-15", "chartHistory": [
                    {"date": "2026-10-14", "close": 100}, {"date": "2026-10-15", "close": 90}]},
                {"code": "1003", "asOf": "2026-10-14", "chartHistory": [
                    {"date": "2026-10-13", "close": 100}, {"date": "2026-10-14", "close": 120}]},
                {"code": "1004", "asOf": "2026-10-15", "chartHistory": [
                    {"date": "2026-10-14", "close": 100}, {"date": "2026-10-15", "close": 95}]},
            ],
            "sources": {"priceHistory": {"provider": "test"}},
        }
        result = build_industry_market(dataset, "2026-10-15T16:00:00+09:00")
        self.assertEqual(result["asOf"], "2026-10-15")
        self.assertEqual(result["rows"], [{
            "industry": "機械", "changeRate": 0.0, "coveredCount": 2, "componentCount": 2
        }])
        self.assertIn("東証業種別株価指数ではありません", result["basis"])

    def test_valuation_snapshot_omits_zero_and_nonfinite(self):
        row = _compact_score_row({
            "code": "1001", "score": 70, "per": 0, "pbr": float("nan"),
            "dividendYield": 0.03, "dividendYieldKind": "forecast",
        })
        self.assertNotIn("per", row)
        self.assertNotIn("pbr", row)
        self.assertEqual(row["dividendYield"], 0.03)
        self.assertEqual(row["dividendYieldKind"], "forecast")


if __name__ == "__main__":
    unittest.main()
