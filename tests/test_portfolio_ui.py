from __future__ import annotations

import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "outputs" / "investment-candidate-app.html"
FETCH_PATH = ROOT / "work" / "fetch_official_data.py"


class PortfolioElementCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.portfolio_views: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(str(values["id"]))
        if tag == "button" and values.get("data-mobile-portfolio-view"):
            self.portfolio_views.append(str(values["data-mobile-portfolio-view"]))


class PortfolioUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = HTML_PATH.read_text(encoding="utf-8")
        cls.fetch_source = FETCH_PATH.read_text(encoding="utf-8")
        cls.parser = PortfolioElementCollector()
        cls.parser.feed(cls.html)

    def test_portfolio_page_has_required_controls(self) -> None:
        for element_id in (
            "portfolioView",
            "portfolioPanel",
            "portfolioSummary",
            "portfolioRows",
            "portfolioMobileList",
            "portfolioAddButton",
            "holdingDialog",
            "holdingCode",
            "holdingShares",
            "holdingAverageCost",
            "holdingManualDps",
        ):
            self.assertIn(element_id, self.parser.ids)

    def test_mobile_holdings_and_watchlist_are_separate_views(self) -> None:
        self.assertEqual(self.parser.portfolio_views, ["holdings", "watchlist"])
        self.assertIn('data-mobile-portfolio-view="holdings"', self.html)
        self.assertIn("function setMobilePortfolioView(view)", self.html)

    def test_holdings_are_saved_only_in_browser_storage(self) -> None:
        self.assertIn('const PORTFOLIO_STORAGE_KEY = "capitalGainRadar.portfolio.v1";', self.html)
        self.assertIn("localStorage.setItem(PORTFOLIO_STORAGE_KEY", self.html)
        self.assertNotIn("portfolioHoldings", self.fetch_source)

    def test_current_and_book_yield_share_forecast_dps(self) -> None:
        self.assertIn("const currentYield = forecastDps && Number.isFinite(latestPrice) ? forecastDps.value / latestPrice : null;", self.html)
        self.assertIn("const bookYield = forecastDps ? forecastDps.value / holding.averageCost : null;", self.html)
        self.assertIn("return officialForecastDps(item);", self.html)
        self.assertIn("予想DPS未取得", self.html)

    def test_forecast_dps_kind_is_preserved_in_output_merge(self) -> None:
        self.assertGreaterEqual(self.fetch_source.count('"dpsKind"'), 2)


if __name__ == "__main__":
    unittest.main()
