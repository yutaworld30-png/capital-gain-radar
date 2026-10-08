import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "work"))
from fetch_official_data import margin_pdf_publication_date, margin_pdf_date, parse_margin_rows
from fetch_official_data import fetch_margin_file_links, JPX_MARGIN_URL, JPX_MARGIN_INDEX_URL


class MarginPublicationTest(unittest.TestCase):
    def test_daily_pdf_accepts_all_trading_days(self):
        self.assertEqual(margin_pdf_date("https://www.jpx.co.jp/20261002_mtall.pdf"), "2026-10-02")
        self.assertEqual(margin_pdf_date("https://www.jpx.co.jp/20261001_mtall.pdf"), "2026-10-01")
        self.assertEqual(margin_pdf_date("https://www.jpx.co.jp/syumatsu2026091800.pdf"), "2026-09-18")

    def test_daily_pdf_balance_columns(self):
        class Page:
            def extract_text(self):
                return ("B test 13010 JP3257200000 Shs. 9,400 600 0.1% 160,100 2,400 1.3%\n"
                        "B test 13320 JP3718800000 Shs. 55,800 �� 8,900 0.0% 721,100 47,000 0.2%")
        rows, failures = parse_margin_rows(type("Reader", (), {"pages": [Page()]})())
        self.assertEqual(failures, 0)
        self.assertEqual([(row["outstandingSales"], row["outstandingPurchases"]) for row in rows],
                         [(9400, 160100), (55800, 721100)])

    def test_discovers_moved_official_page(self):
        moved = "https://www.jpx.co.jp/markets/statistics-equities/margin/99.html"
        pages = {
            JPX_MARGIN_INDEX_URL: '<a href="99.html">銘柄別信用取引残高</a>',
            moved: '<a href="syumatsu2026091800.pdf">信用残高</a>',
        }
        with patch("fetch_official_data.fetch_text", side_effect=pages.__getitem__) as fetch:
            links = fetch_margin_file_links()
        self.assertTrue(links[0]["url"].endswith("syumatsu2026091800.pdf"))
        self.assertEqual(fetch.call_count, 2)

    def test_fallback_and_no_dated_pdf_error(self):
        for html, valid in [('<a href="syumatsu2026091800.pdf">信用残高</a>', True),
                            ('<a href="totals.pdf">信用残高</a>', False)]:
            with patch("fetch_official_data.fetch_text", side_effect=lambda url: html if url == JPX_MARGIN_URL else ""):
                if valid:
                    self.assertEqual(len(fetch_margin_file_links()), 1)
                else:
                    with self.assertRaisesRegex(ValueError, "margin PDFs not found"):
                        fetch_margin_file_links()

    def test_header_dates(self):
        self.assertEqual(margin_pdf_publication_date("2026/9/18 application (Unit: 1 share) 2026/9/25\nAs of 2026/9/18", "2026-09-18"), "2026-09-25")
        self.assertEqual(margin_pdf_publication_date("Note\n2026/10/2 application 2026/10/5\nAs of 2026/10/2", "2026-10-02"), "2026-10-05")

    def test_unverified_dates_stay_missing(self):
        for text in ("2026/9/18", "", "2026/9/11 2026/9/25", "2026/9/18 2026/9/12", "2026/9/18 2026/99/25"):
            self.assertIsNone(margin_pdf_publication_date(text, "2026-09-18"))
