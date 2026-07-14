from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))
sys.modules.setdefault(
    "xlrd",
    types.SimpleNamespace(XLRDError=Exception, open_workbook=lambda *args, **kwargs: None),
)

import fetch_official_data as pipeline  # noqa: E402
from tdnet_connector import analyze_disclosures  # noqa: E402


class P1QualityTest(unittest.TestCase):
    def test_missing_valuation_metrics_contribute_zero(self) -> None:
        self.assertEqual(pipeline._valuation_score({}), 0)
        self.assertGreater(pipeline._valuation_score({"per": 12.0, "pbr": 1.0, "roe": 0.12}), 0)

    def test_stale_and_failed_sources_are_not_available(self) -> None:
        stale_date = (date.today() - timedelta(days=8)).isoformat()
        self.assertEqual(
            pipeline._source_status({"url": "https://example.test", "updatedAt": stale_date, "status": "available"}, max_age_days=7),
            "stale",
        )
        self.assertEqual(
            pipeline._source_status({"url": "https://example.test", "updatedAt": date.today().isoformat(), "status": "available", "refreshStatus": "error"}),
            "stale",
        )

    def test_peer_scores_blend_universe_and_industry_rank(self) -> None:
        rows = [
            {
                "code": f"A{index}",
                "industry": "A業種",
                "return20": value,
                "averageTurnover20": 100 + index,
                "per": 10 + index,
                "pbr": 1 + index / 10,
                "roe": 0.08 + index / 100,
            }
            for index, value in enumerate((0.01, 0.02, 0.03, 0.04, 0.05))
        ] + [
            {
                "code": f"B{index}",
                "industry": "B業種",
                "return20": value,
                "averageTurnover20": 1000 + index,
                "per": 20 + index,
                "pbr": 2 + index / 10,
                "roe": 0.12 + index / 100,
            }
            for index, value in enumerate((0.10, 0.20, 0.30, 0.40, 0.50))
        ]

        pipeline._apply_peer_factor_scores(rows)

        self.assertGreater(rows[4]["relative"], rows[5]["relative"])
        self.assertTrue(all(0 <= int(row["valuation"]) <= 100 for row in rows))
        self.assertTrue(all(row["factorBasis"]["supply"] == "JPX信用倍率のみ" for row in rows))

    def test_theme_score_does_not_depend_on_price_momentum(self) -> None:
        news = {"AI・生成AI": {"articleCount": 2, "asOf": date.today().isoformat(), "url": "https://example.test"}}
        _, positive, _ = analyze_disclosures({"9984"}, [], {"9984": {"return20": 0.2}}, news)
        _, negative, _ = analyze_disclosures({"9984"}, [], {"9984": {"return20": -0.2}}, news)

        self.assertEqual(positive, negative)
        self.assertNotIn("positiveMomentumShare", positive[0])

    def test_score_history_keeps_only_current_factor_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "score-history-v2.json"
            output.write_text(json.dumps({
                "schemaVersion": 2,
                "snapshots": [
                    {"date": "2026-07-01", "scoreVersion": "old", "factorVersion": "old", "rows": []},
                    {
                        "date": "2026-07-02",
                        "scoreVersion": pipeline.SCORE_VERSION,
                        "factorVersion": pipeline.FACTOR_VERSION,
                        "rows": [],
                    },
                ],
            }), encoding="utf-8")
            dataset = {"searchUniverse": [{"code": "TEST", "name": "fixture", "score": 50}]}
            with patch.object(pipeline, "SCORE_HISTORY_OUTPUT", output):
                updated = pipeline.update_score_history(dataset, "2026-07-14T16:10:00+09:00")

        self.assertEqual([item["date"] for item in updated["snapshots"]], ["2026-07-02", "2026-07-14"])
        self.assertTrue(all(item["scoreVersion"] == pipeline.SCORE_VERSION for item in updated["snapshots"]))

    def test_frontend_loads_versioned_history_only_on_demand(self) -> None:
        html = (ROOT / "outputs" / "investment-candidate-app.html").read_text(encoding="utf-8")
        loader = html[html.index("async function loadBundledDataset"):html.index("async function loadSelectedFile")]

        self.assertIn('fetch("data/score-history-v2.json"', html)
        self.assertIn("void ensureScoreHistoryLoaded()", html)
        self.assertNotIn("loadScoreHistory(),", loader)


if __name__ == "__main__":
    unittest.main()
