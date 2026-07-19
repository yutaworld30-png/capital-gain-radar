from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "outputs" / "investment-candidate-app.html"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-pages.yml"


class NikkeiAnalysisUiTests(unittest.TestCase):
    def test_frontend_contains_analysis_controls_and_charts(self) -> None:
        source = HTML.read_text(encoding="utf-8")
        for expected in (
            'fetch("data/nikkei225-analysis.json"',
            'id="nikkeiAnalysisChart"',
            'id="nikkeiMarginChart"',
            'id="nikkeiInvestorChart"',
            'id="nikkeiBreadthChart"',
            'data-nikkei-indicator="ma"',
            'data-nikkei-indicator="psar"',
            'data-nikkei-indicator="bb"',
            'data-nikkei-indicator="ichimoku"',
            'data-nikkei-indicator="per"',
            'id="nikkeiRange3y"',
            "row.macdHistogram",
            '"rsi14"',
            "row.spanA",
            "row.psar",
        ):
            self.assertIn(expected, source)

    def test_frontend_keeps_permission_required_state_explicit(self) -> None:
        source = HTML.read_text(encoding="utf-8")
        self.assertIn("利用条件確認中", source)
        self.assertIn("PER整数倍ラインは利用条件とデータを確認できるまで表示しません。", source)
        self.assertNotIn("サンプル信用倍率", source)

    def test_workflow_generates_analysis_before_validation(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        analysis_position = source.index("python work/market_analysis.py")
        validation_position = source.index("python work/validate_output.py")
        self.assertLess(analysis_position, validation_position)


if __name__ == "__main__":
    unittest.main()
