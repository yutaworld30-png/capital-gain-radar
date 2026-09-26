import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "work"))
from fetch_official_data import margin_pdf_publication_date


class MarginPublicationTest(unittest.TestCase):
    def test_header_dates(self):
        self.assertEqual(margin_pdf_publication_date("2026/9/18 application (Unit: 1 share) 2026/9/25\nAs of 2026/9/18", "2026-09-18"), "2026-09-25")

    def test_unverified_dates_stay_missing(self):
        for text in ("2026/9/18", "", "2026/9/11 2026/9/25", "2026/9/18 2026/9/12", "2026/9/18 2026/99/25"):
            self.assertIsNone(margin_pdf_publication_date(text, "2026-09-18"))
