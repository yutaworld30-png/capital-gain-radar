from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from weekly_target import (  # noqa: E402
    WEEKLY_TARGET_VERSION,
    attach_weekly_targets,
    build_weekly_target,
)


def chart_history() -> list[dict[str, object]]:
    rows = []
    for index in range(100):
        close = 700 + index + (3 if index % 2 else -3)
        rows.append({
            "date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            "open": close - 1,
            "high": close + 4,
            "low": close - 4,
            "close": close,
            "volume": 2_000_000 if index == 99 else 1_000_000,
        })
    return rows


def qualified_row() -> dict[str, object]:
    return {
        "code": "9999",
        "latestClose": 802,
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


class WeeklyTargetTests(unittest.TestCase):
    def test_strict_candidate_is_generated_by_python(self) -> None:
        result = build_weekly_target(
            qualified_row(),
            {"chartHistory": chart_history()},
        )

        profile = result["profiles"]["single"]
        self.assertEqual(result["version"], WEEKLY_TARGET_VERSION)
        self.assertEqual(profile["status"], "qualified")
        self.assertTrue(profile["strictMatch"])
        self.assertGreaterEqual(profile["score"], 75)
        self.assertEqual(profile["estimatedCostYen"], 80_200)
        self.assertEqual(profile["targetProfitYen"], 4_010)
        self.assertEqual(profile["stopLossYen"], 2_005)

    def test_budget_mismatch_is_a_visible_blocker(self) -> None:
        row = qualified_row()
        row["latestClose"] = 1_500
        result = build_weekly_target(row, {"chartHistory": chart_history()})

        profile = result["profiles"]["double"]
        self.assertFalse(profile["budgetFit"])
        self.assertEqual(profile["status"], "not-eligible")
        self.assertTrue(any("900" in blocker for blocker in profile["blockers"]))

    def test_upcoming_earnings_prevents_strict_match(self) -> None:
        row = qualified_row()
        row["events"] = [{"type": "earnings", "daysFromNow": 2}]
        result = build_weekly_target(row, {"chartHistory": chart_history()})

        profile = result["profiles"]["single"]
        self.assertFalse(profile["strictMatch"])
        self.assertTrue(any("決算" in blocker for blocker in profile["blockers"]))

    def test_attach_adds_versioned_policy_and_both_row_sets(self) -> None:
        first = qualified_row()
        second = dict(first)
        dataset = {
            "nikkei225Prices": [{"code": "9999", "chartHistory": chart_history()}],
            "searchUniverse": [first],
            "candidates": [second],
        }

        attach_weekly_targets(dataset)

        self.assertEqual(dataset["weeklyTargetPolicy"]["version"], WEEKLY_TARGET_VERSION)
        self.assertEqual(first["weeklyTarget"]["version"], WEEKLY_TARGET_VERSION)
        self.assertEqual(second["weeklyTarget"]["version"], WEEKLY_TARGET_VERSION)


if __name__ == "__main__":
    unittest.main()
