from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "outputs" / "investment-candidate-app.html"


class DatetimeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = HTML.read_text(encoding="utf-8")

    def test_timestamp_display_converts_offset_timestamp_to_jst(self) -> None:
        self.assertIn('timeZone: "Asia/Tokyo"', self.source)
        self.assertIn('hourCycle: "h23"', self.source)
        self.assertIn("new Intl.DateTimeFormat", self.source)

    def test_date_only_values_are_not_shifted(self) -> None:
        self.assertIn('if (!time) return date.replaceAll("-", "/");', self.source)


if __name__ == "__main__":
    unittest.main()
