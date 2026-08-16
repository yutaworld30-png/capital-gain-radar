from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "outputs" / "investment-candidate-app.html"
FETCH = ROOT / "work" / "fetch_official_data.py"
VENDOR = ROOT / "outputs" / "vendor" / "lightweight-charts.standalone.production.js"
LICENSE = ROOT / "outputs" / "vendor" / "lightweight-charts.LICENSE.txt"
NOTICE = ROOT / "outputs" / "vendor" / "lightweight-charts.NOTICE.txt"


class InteractiveChartUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = HTML.read_text(encoding="utf-8")
        cls.fetch_source = FETCH.read_text(encoding="utf-8")

    def test_lightweight_charts_is_vendored_with_license_files(self) -> None:
        self.assertTrue(VENDOR.exists())
        self.assertGreater(VENDOR.stat().st_size, 100_000)
        self.assertTrue(LICENSE.exists())
        self.assertTrue(NOTICE.exists())
        self.assertIn(
            '<script src="vendor/lightweight-charts.standalone.production.js"></script>',
            self.html,
        )

    def test_price_and_nikkei_charts_have_interactive_controls(self) -> None:
        for element_id in (
            "priceChartShell",
            "priceChartReadout",
            "priceChartLatest",
            "priceChartFit",
            "priceChartExpand",
            "nikkeiChartShell",
            "nikkeiChartReadout",
            "nikkeiChartLatest",
            "nikkeiChartFit",
            "nikkeiChartExpand",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("function toggleChartExpansion(shell, button)", self.html)
        self.assertIn("function closeExpandedCharts()", self.html)

    def test_mouse_and_touch_gestures_are_enabled_without_blocking_vertical_scroll(self) -> None:
        self.assertIn("mouseWheel: true", self.html)
        self.assertIn("pressedMouseMove: true", self.html)
        self.assertIn("horzTouchDrag: true", self.html)
        self.assertIn("vertTouchDrag: false", self.html)
        self.assertIn("pinch: true", self.html)
        self.assertIn("axisDoubleClickReset: true", self.html)

    def test_financial_series_and_panes_are_preserved(self) -> None:
        for expected in (
            "api.CandlestickSeries",
            "api.HistogramSeries",
            "setChartPaneFactors(chart, [5, 1.2, 1.5, 1.5])",
            "setChartPaneFactors(chart, [5, 1.6, 1.6])",
            'addIndexedChartLine(chart, allRows, ma5, "#0f8f6a"',
            'addIndexedChartLine(chart, allRows, rsiValues, "#246bce", 2',
            'addChartLine(chart, allRows, "psar", "#9b5c12"',
            "candleSeries.createPriceLine",
        ):
            self.assertIn(expected, self.html)

    def test_price_pipeline_keeps_three_year_chart_history(self) -> None:
        self.assertIn("CHART_HISTORY_ROWS = 780", self.fetch_source)
        self.assertIn("CHART_HISTORY_CALENDAR_DAYS = 1200", self.fetch_source)
        self.assertIn("primary_rows[-CHART_HISTORY_ROWS:]", self.fetch_source)
        self.assertIn('"maxRows": CHART_HISTORY_ROWS', self.fetch_source)
        self.assertNotIn("primary_rows[-260:]", self.fetch_source)


if __name__ == "__main__":
    unittest.main()
