from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from market_analysis import (  # noqa: E402
    ANALYSIS_VERSION,
    LOCAL_OUTPUT,
    LOCAL_PRIVATE_DISTRIBUTION_MODE,
    MarketAnalysisError,
    _per_data,
    _weekly_data,
    build_analysis_payload,
    parse_weighted_per_html,
    resolve_output_path,
    validate_analysis,
    _weekly_recency_status,
)


def sample_rows(count: int = 140) -> list[dict[str, float | str]]:
    start = date(2026, 1, 1)
    return [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "open": 40_000 + index,
            "high": 40_100 + index,
            "low": 39_900 + index,
            "close": 40_050 + index,
        }
        for index in range(count)
    ]


class MarketAnalysisTests(unittest.TestCase):
    def test_parse_weighted_per_html(self) -> None:
        html = """
        <table>
          <tr><td>2026.07.16</td><td>17.99</td><td>23.96</td></tr>
          <tr><td>2026.07.17</td><td>17.42</td><td>22.99</td></tr>
        </table>
        """
        rows = parse_weighted_per_html(html)
        self.assertEqual(rows[-1]["date"], "2026-07-17")
        self.assertEqual(rows[-1]["weightedPer"], 17.42)

    def test_payload_uses_explicit_data_states_and_per_reference(self) -> None:
        rows = sample_rows()
        latest_date = rows[-1]["date"]
        payload = build_analysis_payload(
            rows,
            generated_at="2026-07-19T10:00:00+09:00",
            price_url="https://example.test/chart",
            per_rows=[{"date": latest_date, "weightedPer": 20.0, "indexPer": 25.0}],
            per_source={"status": "available", "url": "https://example.test/per", "asOf": latest_date},
            margin={"status": "permission-required", "rows": []},
            investor={"status": "permission-required", "rows": []},
            breadth={"status": "available", "rows": []},
        )
        self.assertEqual(payload["analysisVersion"], ANALYSIS_VERSION)
        self.assertEqual(validate_analysis(payload), [])
        self.assertEqual(payload["per"]["reference"]["weightedPer"], 20.0)
        self.assertEqual(
            payload["per"]["reference"]["bandLevels"]["20"],
            round(float(rows[-1]["close"]), 2),
        )

    def test_validation_rejects_short_price_history(self) -> None:
        payload = {
            "schemaVersion": 1,
            "analysisVersion": ANALYSIS_VERSION,
            "technicalVersion": "nikkei225-technical-v1",
            "rows": [],
            "margin": {"status": "unavailable"},
            "investorFlows": {"status": "unavailable"},
            "breadth": {"status": "unavailable"},
            "per": {"status": "unavailable"},
        }
        self.assertIn("日経225テクニカル行が100件未満です。", validate_analysis(payload))

    def test_restricted_sources_are_gated_without_confirmation(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "NIKKEI_INDEX_DATA_USE_CONFIRMED": "",
                "JPX_PUBLIC_DATA_USE_CONFIRMED": "",
            },
            clear=False,
        ):
            per_rows, per_source = _per_data({})
            margin, investor = _weekly_data({})
        self.assertEqual(per_rows, [])
        self.assertEqual(per_source["status"], "permission-required")
        self.assertEqual(margin["status"], "permission-required")
        self.assertEqual(investor["status"], "permission-required")
        self.assertEqual(margin["rows"], [])
        self.assertEqual(investor["rows"], [])

    def test_local_private_mode_fetches_restricted_sources(self) -> None:
        per_html = b"""
        <table><tr><td>2026.07.17</td><td>17.42</td><td>22.99</td></tr></table>
        """
        margin_rows = [
            {
                "weekEnd": "2026-07-17",
                "sellBalance": 100,
                "buyBalance": 900,
                "ratio": 9.0,
            }
        ]
        investor_rows = [
            {
                "periodEnd": "2026-07-17",
                "foreign": {"net": 120.0},
            }
        ]
        with (
            patch("market_analysis.fetch_bytes", return_value=per_html),
            patch(
                "market_analysis.fetch_margin_history",
                return_value=(margin_rows, "https://example.test/margin"),
            ),
            patch(
                "market_analysis.fetch_investor_history",
                return_value=(investor_rows, ["https://example.test/investor.xlsx"]),
            ),
        ):
            per_rows, per_source = _per_data(
                {},
                distribution_mode=LOCAL_PRIVATE_DISTRIBUTION_MODE,
            )
            margin, investor = _weekly_data(
                {},
                distribution_mode=LOCAL_PRIVATE_DISTRIBUTION_MODE,
                today=date(2026, 7, 20),
            )
        self.assertEqual(per_rows[-1]["weightedPer"], 17.42)
        self.assertEqual(per_source["status"], "available")
        self.assertEqual(per_source["accessMode"], LOCAL_PRIVATE_DISTRIBUTION_MODE)
        self.assertEqual(margin["status"], "available")
        self.assertEqual(margin["accessMode"], LOCAL_PRIVATE_DISTRIBUTION_MODE)
        self.assertEqual(investor["status"], "available")
        self.assertEqual(investor["accessMode"], LOCAL_PRIVATE_DISTRIBUTION_MODE)

    def test_local_private_payload_is_rejected_by_public_validation(self) -> None:
        rows = sample_rows()
        payload = build_analysis_payload(
            rows,
            generated_at="2026-07-20T10:00:00+09:00",
            price_url="https://example.test/chart",
            per_rows=[],
            per_source={"status": "unavailable"},
            margin={"status": "unavailable", "rows": []},
            investor={"status": "unavailable", "rows": []},
            breadth={"status": "available", "rows": []},
            distribution_mode=LOCAL_PRIVATE_DISTRIBUTION_MODE,
        )
        self.assertEqual(validate_analysis(payload), [])
        self.assertIn(
            "ローカル個人利用データは公開成果物に含められません。",
            validate_analysis(payload, public_only=True),
        )

    def test_local_private_output_is_confined_to_local_data_directory(self) -> None:
        self.assertEqual(
            resolve_output_path(local_private=True),
            LOCAL_OUTPUT.resolve(),
        )
        with self.assertRaises(MarketAnalysisError):
            resolve_output_path(
                local_private=True,
                requested=ROOT / "outputs" / "data" / "private-analysis.json",
            )

    def test_weekly_recency_does_not_label_old_data_available(self) -> None:
        status, note = _weekly_recency_status(
            "2025-12-26",
            today=date(2026, 1, 30),
        )
        self.assertEqual(status, "stale-fallback")
        self.assertIn("最新値として扱いません", note or "")

    def test_weekly_recency_boundary_uses_explicit_reference_date(self) -> None:
        available, _ = _weekly_recency_status(
            "2026-07-17",
            max_age_days=21,
            today=date(2026, 8, 7),
        )
        stale, _ = _weekly_recency_status(
            "2026-07-17",
            max_age_days=21,
            today=date(2026, 8, 8),
        )
        self.assertEqual(available, "available")
        self.assertEqual(stale, "stale-fallback")


if __name__ == "__main__":
    unittest.main()
