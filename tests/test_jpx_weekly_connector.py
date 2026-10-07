from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from jpx_weekly_connector import (  # noqa: E402
    JpxWeeklyError,
    MARGIN_CURRENT_PAGE,
    MARGIN_HISTORY_PAGE,
    extract_xls_links,
    fetch_margin_history,
    parse_current_margin_sheet,
    parse_investor_sheet,
    parse_margin_sheet,
    parse_margin_workbook,
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
    @staticmethod
    def margin_xlsx(*, include_unit: bool = True) -> bytes:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "信用取引現在高"
        sheet["N4"] = "合計 Total"
        sheet["N6"] = "売り残 Shares Sold Short"
        sheet["P6"] = "買い残 Shares Bought on Margin"
        if include_unit:
            sheet["N9"] = "株数 thous.shs."
            sheet["P9"] = "株数 thous.shs."
        sheet["A10"] = "東京・名古屋 Tokyo & Nagoya"
        sheet["A11"] = datetime(2026, 10, 2)
        sheet["J11"] = 77_387  # Other margin column, not the two-market total.
        sheet["L11"] = 1_981_537
        sheet["N11"] = 354_386
        sheet["P11"] = 3_765_572
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()

    def test_current_xlsx_uses_total_columns_and_thousand_share_unit(self) -> None:
        rows = parse_margin_workbook(self.margin_xlsx())
        self.assertEqual(rows, [{
            "weekEnd": "2026-10-02",
            "sellBalanceThousandShares": 354386.0,
            "buyBalanceThousandShares": 3765572.0,
            "marginRatio": round(3765572 / 354386, 4),
        }])

    def test_current_xlsx_rejects_unknown_unit(self) -> None:
        with self.assertRaises(JpxWeeklyError):
            parse_margin_workbook(self.margin_xlsx(include_unit=False))

    def test_history_survives_current_page_error(self) -> None:
        xlsx_url = "https://www.jpx.co.jp/markets/statistics-equities/margin/current.xlsx"
        html = f'<a href="{xlsx_url}">Excel</a>'.encode()

        def fetch(url: str) -> bytes:
            if url == MARGIN_HISTORY_PAGE:
                return html
            if url == xlsx_url:
                return self.margin_xlsx()
            raise JpxWeeklyError(f"HTTP 404: {url}")

        with patch("jpx_weekly_connector.fetch_bytes", side_effect=fetch):
            rows, source = fetch_margin_history()
        self.assertEqual(source, xlsx_url)
        self.assertEqual(rows[-1]["weekEnd"], "2026-10-02")
        self.assertEqual(rows[-1]["sellBalanceThousandShares"], 354386.0)

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
        <a href="/a/stock_val_1_260709.xlsx">new amount</a>
        """
        links = extract_xls_links(html, "https://www.jpx.co.jp/page", contains="stock_val_1_")
        self.assertEqual(
            links,
            [
                "https://www.jpx.co.jp/a/stock_val_1_260702.xls",
                "https://www.jpx.co.jp/a/stock_val_1_260709.xlsx",
            ],
        )


if __name__ == "__main__":
    unittest.main()
