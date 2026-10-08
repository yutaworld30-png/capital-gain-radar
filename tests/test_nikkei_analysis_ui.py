from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "outputs" / "investment-candidate-app.html"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-pages.yml"


class NikkeiAnalysisUiTests(unittest.TestCase):
    def test_frontend_contains_analysis_controls_charts_and_investor_matrix(self) -> None:
        source = HTML.read_text(encoding="utf-8")
        for expected in (
            'fetch("data/nikkei225-analysis.json"',
            'id="nikkeiAnalysisChart"',
            'id="nikkeiMarginChart"',
            'id="nikkeiInvestorTable"',
            'id="nikkeiInvestorTableWrap"',
            'id="nikkeiBreadthChart"',
            'data-nikkei-indicator="ma"',
            'data-nikkei-indicator="psar"',
            'data-nikkei-indicator="bb"',
            'data-nikkei-indicator="ichimoku"',
            'id="nikkeiValuationRows"',
            'id="nikkeiEpsChart"',
            'id="nikkeiBpsChart"',
            'id="nikkeiRange3y"',
            "row.macdHistogram",
            '"rsi14"',
            'addChartLine(chart, allRows, "spanA"',
            'addChartLine(chart, allRows, "psar"',
            'id="nikkeiLocalPrivateNotice"',
            'payload.distributionMode === "local-private"',
            'payload.distributionMode === "private-cloud"',
            'function renderNikkeiValuation(payload = nikkeiAnalysis)',
            '直下の整数倍',
            '直上の整数倍',
        ):
            self.assertIn(expected, source)
        self.assertNotIn('data-nikkei-indicator="per"', source)
        self.assertNotIn('PER整数倍ライン', source)

    def test_frontend_keeps_permission_required_state_explicit(self) -> None:
        source = HTML.read_text(encoding="utf-8")
        self.assertIn("利用条件確認中", source)
        self.assertIn("日経公式のPER・PBR・終値は本人限定版で取得します。", source)
        self.assertNotIn("サンプル信用倍率", source)

    def test_workflow_generates_analysis_before_validation(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        analysis_position = source.index("python work/market_analysis.py")
        validation_position = source.index("python work/validate_output.py")
        self.assertLess(analysis_position, validation_position)


if __name__ == "__main__":
    unittest.main()
