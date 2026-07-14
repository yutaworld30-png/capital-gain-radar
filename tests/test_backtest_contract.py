from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from backtest_score import backtest_verification_issues  # noqa: E402


class BacktestContractTest(unittest.TestCase):
    def valid_payload(self) -> dict[str, object]:
        return {
            "status": "verified",
            "provenance": {
                "generator": "backtest225.run_backtest",
                "version": "1.0.0",
                "dataHash": "sha256:fixture",
            },
            "data": {
                "periodStart": "2025-01-01",
                "periodEnd": "2025-12-31",
            },
            "strategy": {
                "parameters": {
                    "buyScore": 75,
                    "sellScore": 65,
                },
            },
            "artifacts": {
                "trades": {
                    "path": "outputs/backtests/trades.csv",
                    "sha256": "sha256:trades",
                    "count": 10,
                },
            },
        }

    def test_complete_backtest_contract_has_no_missing_evidence(self) -> None:
        self.assertEqual(backtest_verification_issues(self.valid_payload()), [])

    def test_missing_trade_log_is_unverified(self) -> None:
        payload = self.valid_payload()
        payload["artifacts"] = {}

        issues = backtest_verification_issues(payload)

        self.assertIn("取引明細パス", issues)
        self.assertIn("取引明細ハッシュ", issues)
        self.assertIn("取引明細件数", issues)

    def test_published_summary_is_explicitly_unverified(self) -> None:
        payload = json.loads(
            (ROOT / "outputs" / "data" / "backtest-summary.json").read_text(encoding="utf-8")
        )

        self.assertEqual(payload["status"], "unverified")
        self.assertFalse(payload["verification"]["verified"])
        self.assertEqual(payload["kpis"], [])
        self.assertTrue(backtest_verification_issues(payload))

    def test_frontend_hides_unverified_kpis(self) -> None:
        html = (ROOT / "outputs" / "investment-candidate-app.html").read_text(encoding="utf-8")

        self.assertIn("function backtestVerificationIssues", html)
        self.assertIn("成績数値は表示しません", html)
        self.assertIn("検証結果</div>", html)


if __name__ == "__main__":
    unittest.main()
