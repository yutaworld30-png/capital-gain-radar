import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "work"))
from nikkei_margin_daily import build_nikkei_margin_daily


class NikkeiMarginDailyTests(unittest.TestCase):
    def setUp(self):
        self.components = [{"code": f"{index:04d}"} for index in range(1000, 1225)]
        self.history = {"schemaVersion": 1, "stocks": {}}

    def add_day(self, day, *, count=225, buy=2000, sell=1000):
        for item in self.components[:count]:
            code = item["code"]
            self.history["stocks"].setdefault(code, []).append({
                "date": day, "buy": buy, "sell": sell,
                "ratio": round(buy / sell, 4) if sell else None,
                "publishedAt": "2026-10-08",
            })

    def test_aggregates_only_covered_days_and_keeps_dates(self):
        self.add_day("2026-10-02", count=210)
        self.add_day("2026-10-06", count=225)
        self.add_day("2026-10-07", count=220, buy=3000, sell=0)
        result = build_nikkei_margin_daily(self.history, self.components, today=date(2026, 10, 8))
        self.assertEqual(result["status"], "available")
        self.assertEqual([row["date"] for row in result["rows"]], ["2026-10-06", "2026-10-07"])
        self.assertEqual(result["rows"][0]["marginRatio"], 2.0)
        self.assertEqual(result["rows"][1]["buyBalanceThousandShares"], 660.0)
        self.assertIsNone(result["rows"][1]["marginRatio"])
        self.assertEqual(result["rows"][1]["coveredCount"], 220)

    def test_stale_and_missing_membership_are_explicit(self):
        self.add_day("2026-09-25")
        result = build_nikkei_margin_daily(self.history, self.components, today=date(2026, 10, 8))
        self.assertEqual(result["status"], "stale-fallback")
        self.assertEqual(build_nikkei_margin_daily(self.history, self.components[:224])["status"], "unavailable")
