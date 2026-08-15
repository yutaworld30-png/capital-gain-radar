from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "outputs" / "investment-candidate-app.html"


class WeeklyPredictionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = HTML.read_text(encoding="utf-8")

    def test_weekly_candidate_and_accuracy_tabs_are_present(self) -> None:
        self.assertIn('data-weekly-section="candidates"', self.source)
        self.assertIn('data-weekly-section="accuracy"', self.source)
        self.assertIn('id="weeklyAccuracyPanel"', self.source)

    def test_accuracy_json_and_all_condition_views_are_connected(self) -> None:
        self.assertIn('fetch("data/weekly-accuracy-summary-v1.json"', self.source)
        for condition in ("status", "scoreBands", "marketRegimes", "industries", "strictChecks"):
            self.assertIn(f'<option value="{condition}">', self.source)

    def test_outcome_assumptions_are_visible(self) -> None:
        self.assertIn("翌営業日始値から5営業日を評価", self.source)
        self.assertIn("同日に両方へ到達した記録は主的中率から除外", self.source)
        self.assertIn("対照銘柄との差", self.source)


if __name__ == "__main__":
    unittest.main()
