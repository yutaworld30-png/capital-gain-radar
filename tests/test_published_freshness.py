from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from check_published_freshness import (  # noqa: E402
    analysis_freshness_issues,
    freshness_issues,
)


JST = timezone(timedelta(hours=9), name="JST")


def sample_payload(generated_at: str = "2026-08-13T07:20:00+00:00") -> dict:
    return {
        "generatedAt": generated_at,
        "sources": {
            "priceHistory": {
                "status": "available",
                "asOf": "2026-08-13",
            }
        },
        "searchUniverse": [{"code": "9433"}],
    }


def sample_analysis(generated_at: str = "2026-08-13T07:30:00+00:00") -> dict:
    return {
        "generatedAt": generated_at,
        "priceSource": {"status": "available", "asOf": "2026-08-13"},
        "rows": [{"date": "2026-08-13", "close": 67_000}],
    }


class PublishedFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 13, 19, 10, tzinfo=JST)

    def test_today_payload_is_fresh(self) -> None:
        self.assertEqual(freshness_issues(sample_payload(), now=self.now), [])

    def test_utc_timestamp_is_compared_in_jst(self) -> None:
        payload = sample_payload("2026-08-12T23:30:00+00:00")
        self.assertEqual(freshness_issues(payload, now=self.now), [])

    def test_previous_day_payload_is_stale(self) -> None:
        issues = freshness_issues(
            sample_payload("2026-08-12T07:20:00+00:00"),
            now=self.now,
        )
        self.assertTrue(any("当日ではありません" in issue for issue in issues))

    def test_unavailable_price_source_is_stale(self) -> None:
        payload = sample_payload()
        payload["sources"]["priceHistory"]["status"] = "stale-fallback"
        issues = freshness_issues(payload, now=self.now)
        self.assertIn("価格履歴がavailableではありません。", issues)

    def test_previous_trading_date_requests_backup_refresh(self) -> None:
        payload = sample_payload()
        payload["sources"]["priceHistory"]["asOf"] = "2026-08-12"
        issues = freshness_issues(payload, now=self.now)
        self.assertTrue(any("直近の確定取引日ではありません" in issue for issue in issues))

    def test_morning_accepts_previous_weekday_close(self) -> None:
        payload = sample_payload()
        payload["sources"]["priceHistory"]["asOf"] = "2026-08-12"
        morning = datetime(2026, 8, 13, 10, 0, tzinfo=JST)
        self.assertEqual(freshness_issues(payload, now=morning), [])

    def test_monday_morning_accepts_previous_friday_close(self) -> None:
        payload = sample_payload("2026-08-17T00:30:00+00:00")
        payload["sources"]["priceHistory"]["asOf"] = "2026-08-14"
        monday_morning = datetime(2026, 8, 17, 10, 0, tzinfo=JST)
        self.assertEqual(freshness_issues(payload, now=monday_morning), [])

    def test_empty_ranking_universe_is_stale(self) -> None:
        payload = sample_payload()
        payload["searchUniverse"] = []
        issues = freshness_issues(payload, now=self.now)
        self.assertIn("ランキング母集団が空です。", issues)

    def test_analysis_matching_candidate_price_date_is_fresh(self) -> None:
        self.assertEqual(
            analysis_freshness_issues(
                sample_analysis(),
                candidate_payload=sample_payload(),
                now=self.now,
            ),
            [],
        )

    def test_analysis_older_than_candidate_requests_refresh(self) -> None:
        analysis = sample_analysis()
        analysis["priceSource"]["asOf"] = "2026-08-10"
        analysis["rows"][-1]["date"] = "2026-08-10"
        issues = analysis_freshness_issues(
            analysis,
            candidate_payload=sample_payload(),
            now=self.now,
        )
        self.assertTrue(any("候補データと一致しません" in issue for issue in issues))

    def test_previous_day_analysis_generation_is_stale(self) -> None:
        issues = analysis_freshness_issues(
            sample_analysis("2026-08-12T07:30:00+00:00"),
            candidate_payload=sample_payload(),
            now=self.now,
        )
        self.assertTrue(any("generatedAtが当日ではありません" in issue for issue in issues))

    def test_workflow_has_freshness_gate_and_backup_schedule(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "deploy-pages.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "10 7 * * 1-5"', workflow)
        self.assertIn('cron: "10 10 * * 1-5"', workflow)
        self.assertIn("python3 work/check_published_freshness.py", workflow)
        self.assertIn("needs.freshness.outputs.should_refresh == 'true'", workflow)


if __name__ == "__main__":
    unittest.main()
