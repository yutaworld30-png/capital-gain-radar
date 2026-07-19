from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from jpx_weekly_connector import (  # noqa: E402
    extract_xls_links,
    parse_current_margin_sheet,
    parse_investor_sheet,
    parse_margin_sheet,
)


class FakeSheet:
    def __init__(self, rows: int, columns: int) -> None:
        self.values = [[None for _ in range(columns)] for _ in range(rows)]
        self.nrows = rows
        self.ncols = columns

    def set(self, row: int, column: int, value: object) -> None:
        self.values[row][column] = value

    def cell_value(self, row: int, column: int) -> object:
        return self.values[row][column]


class JpxWeeklyConnectorTests(unittest.TestCase):
    def test_margin_columns_and_zero_sell_balance(self) -> None:
        sheet = FakeSheet(12, 22)
        excel_epoch = date(1899, 12, 30)
        first_date = (date(2026, 7, 10) - excel_epoch).days
        second_date = (date(2026, 7, 17) - excel_epoch).days
        sheet.set(10, 0, first_date)
        sheet.set(10, 9, 100)
        sheet.set(10, 11, 250)
        sheet.set(11, 0, second_date)
        sheet.set(11, 9, 0)
        sheet.set(11, 11, 100)
        rows = parse_margin_sheet(sheet)
        self.assertEqual(rows[0]["weekEnd"], date(2026, 7, 10).isoformat())
        self.assertEqual(rows[0]["marginRatio"], 2.5)
        self.assertIsNone(rows[1]["marginRatio"])

    def test_investor_flows_sign_and_unit(self) -> None:
        sheet = FakeSheet(70, 11)
        sheet.set(3, 0, "2026年7月第2週 2026/7 week2 ( 7/6 - 7/10 )")
        categories = {
            26: "個　人",
            29: "海外投資家",
            37: "投資信託",
            40: "事業法人",
            57: "信託銀行",
        }
        for row, label in categories.items():
            sheet.set(row, 0, label)
            sheet.set(row, 8, 300_000)
            sheet.set(row + 1, 8, 500_000)
        result = parse_investor_sheet(sheet)
        self.assertEqual(result["periodEnd"], "2026-07-10")
        self.assertEqual(result["flows"]["individual"]["sales100mYen"], 3.0)
        self.assertEqual(result["flows"]["individual"]["purchases100mYen"], 5.0)
        self.assertEqual(result["flows"]["individual"]["net100mYen"], 2.0)

    def test_current_margin_sheet_adds_latest_week(self) -> None:
        sheet = FakeSheet(8, 15)
        sheet.set(0, 0, "信用取引現在高（2026/7/10申込み現在）")
        sheet.set(6, 1, "二市場計 Total")
        sheet.set(6, 2, "株数Shs.")
        sheet.set(6, 11, 400_000)
        sheet.set(6, 13, 3_800_000)
        row = parse_current_margin_sheet(sheet)
        self.assertEqual(row["weekEnd"], "2026-07-10")
        self.assertEqual(row["marginRatio"], 9.5)

    def test_extract_xls_links_filters_amount_files(self) -> None:
        html = """
        <a href="/a/stock_val_1_260702.xls">amount</a>
        <a href="/a/stock_1_260702.xls">shares</a>
        """
        links = extract_xls_links(html, "https://www.jpx.co.jp/page", contains="stock_val_1_")
        self.assertEqual(
            links,
            ["https://www.jpx.co.jp/a/stock_val_1_260702.xls"],
        )


if __name__ == "__main__":
    unittest.main()
