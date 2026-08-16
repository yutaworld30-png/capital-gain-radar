from __future__ import annotations

import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "outputs" / "investment-candidate-app.html"


class AttributeCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.mobile_pages: list[str] = []
        self.mobile_ranking_views: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(str(values["id"]))
        if tag == "button" and values.get("data-mobile-page"):
            self.mobile_pages.append(str(values["data-mobile-page"]))
        if tag == "button" and values.get("data-mobile-ranking-view"):
            self.mobile_ranking_views.append(str(values["data-mobile-ranking-view"]))


class MobileNavigationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = HTML_PATH.read_text(encoding="utf-8")
        cls.parser = AttributeCollector()
        cls.parser.feed(cls.html)

    def test_mobile_navigation_has_five_distinct_pages(self) -> None:
        self.assertEqual(
            self.parser.mobile_pages,
            ["candidates", "market", "watch", "compare", "more"],
        )

    def test_mobile_ranking_tabs_cover_candidate_views(self) -> None:
        self.assertEqual(
            self.parser.mobile_ranking_views,
            ["overall", "weekly", "breakout", "theme", "industry"],
        )

    def test_mobile_controls_have_unique_ids(self) -> None:
        self.assertEqual(len(self.parser.ids), len(set(self.parser.ids)))
        for element_id in (
            "mobileBottomNav",
            "mobileFilterSheet",
            "mobileCandidateCards",
            "mobileDetailBack",
            "mobileMarketTabs",
            "mobileMoreMenu",
        ):
            self.assertIn(element_id, self.parser.ids)

    def test_mobile_layout_preserves_desktop_renderers(self) -> None:
        self.assertIn("@media (max-width: 720px)", self.html)
        self.assertIn("position: fixed;", self.html)
        self.assertIn("function renderMobileRows(rows)", self.html)
        self.assertIn("function renderRows(rows)", self.html)
        self.assertIn("function openMobileDetail(code)", self.html)
        self.assertIn("function closeMobileDetail(fromHistory = false)", self.html)
        self.assertIn('body.mobile-detail-open #stockDetailPanel', self.html)

    def test_mobile_detail_keeps_bottom_navigation_available(self) -> None:
        start = self.html.index("body.mobile-detail-open .mobile-bottom-nav")
        detail_nav_css = self.html[start:start + 180]

        self.assertIn("display: grid", detail_nav_css)
        self.assertIn("z-index: 120", detail_nav_css)
        self.assertNotIn("display: none", detail_nav_css)

    def test_mobile_page_change_clears_open_detail_immediately(self) -> None:
        self.assertIn("function clearMobileDetailState", self.html)
        set_page = self.html[
            self.html.index("function setMobilePage(page)"):
            self.html.index("function setMobileRankingView", self.html.index("function setMobilePage(page)"))
        ]
        close_detail = self.html[
            self.html.index("function closeMobileDetail"):
            self.html.index("function loadWatchlist", self.html.index("function closeMobileDetail"))
        ]

        self.assertIn("clearMobileDetailState()", set_page)
        self.assertIn("clearMobileDetailState({ rewindHistory: !fromHistory })", close_detail)


if __name__ == "__main__":
    unittest.main()
