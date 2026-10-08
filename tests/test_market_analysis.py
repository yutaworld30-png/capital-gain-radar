from __future__ import annotations

import json
import io
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from market_analysis import (  # noqa: E402
    ANALYSIS_VERSION,
    LOCAL_OUTPUT,
    LOCAL_PRIVATE_DISTRIBUTION_MODE,
    PRIVATE_CLOUD_DISTRIBUTION_MODE,
    PRIVATE_OUTPUT,
    MarketAnalysisError,
    _per_data,
    _per_reference,
    _load_private_previous,
    _timestamp_date,
    _weekly_data,
    build_analysis_payload,
    fetch_nikkei225_ohlc,
    merge_price_rows,
    parse_weighted_per_html,
    parse_index_pbr_html,
    parse_nikkei_daily_close_html,
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
    @staticmethod
    def _yahoo_chart_payload(
        *,
        meta_time_offset: int = 16 * 60 * 60,
        latest_close: float | None = None,
    ) -> bytes:
        start = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
        timestamps = [start + index * 86_400 for index in range(101)]
        opens = [40_000 + index for index in range(101)]
        closes = [value + 50 for value in opens]
        closes[-1] = latest_close
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": timestamps,
                        "meta": {
                            "exchangeTimezoneName": "UTC",
                            "regularMarketTime": timestamps[-1] + meta_time_offset,
                            "regularMarketPrice": 40_150.0,
                        },
                        "indicators": {
                            "quote": [
                                {
                                    "open": opens,
                                    "high": [value + 100 for value in opens],
                                    "low": [value - 100 for value in opens],
                                    "close": closes,
                                }
                            ]
                        },
                    }
                ]
            }
        }
        return json.dumps(payload).encode("utf-8")

    def test_latest_missing_close_uses_same_day_post_close_meta_price(self) -> None:
        with patch(
            "market_analysis.fetch_bytes",
            return_value=self._yahoo_chart_payload(),
        ):
            rows, _ = fetch_nikkei225_ohlc(today=date(2026, 4, 11))
        self.assertEqual(len(rows), 101)
        self.assertEqual(rows[-1]["date"], "2026-04-11")
        self.assertEqual(rows[-1]["close"], 40_150.0)

    def test_intraday_meta_price_does_not_replace_missing_close(self) -> None:
        with patch(
            "market_analysis.fetch_bytes",
            return_value=self._yahoo_chart_payload(meta_time_offset=14 * 60 * 60),
        ):
            rows, _ = fetch_nikkei225_ohlc(today=date(2026, 4, 11))
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[-1]["date"], "2026-04-10")

    def test_intraday_complete_looking_row_is_not_treated_as_daily_close(self) -> None:
        with patch(
            "market_analysis.fetch_bytes",
            return_value=self._yahoo_chart_payload(
                meta_time_offset=14 * 60 * 60,
                latest_close=40_150.0,
            ),
        ):
            rows, _ = fetch_nikkei225_ohlc(today=date(2026, 4, 11))
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[-1]["date"], "2026-04-10")

    def test_tokyo_timestamp_fallback_does_not_require_tzdata(self) -> None:
        timestamp = int(datetime(2026, 8, 11, 15, 30, tzinfo=timezone.utc).timestamp())
        with patch(
            "market_analysis.ZoneInfo",
            side_effect=ZoneInfoNotFoundError("Asia/Tokyo"),
        ):
            result = _timestamp_date(timestamp, "Asia/Tokyo")
        self.assertEqual(result, "2026-08-12")

    def test_merge_keeps_prior_confirmed_candle_when_refresh_omits_it(self) -> None:
        previous = sample_rows(101)
        refreshed = sample_rows(100)
        merged = merge_price_rows(previous, refreshed)
        self.assertEqual(len(merged), 101)
        self.assertEqual(merged[-1]["date"], previous[-1]["date"])
        self.assertEqual(merged[-1]["close"], previous[-1]["close"])

    def test_merge_prefers_refreshed_complete_candle_for_same_date(self) -> None:
        previous = sample_rows(101)
        refreshed = [dict(previous[-1], close=41_000.0, high=41_100.0)]
        merged = merge_price_rows(previous, refreshed)
        self.assertEqual(merged[-1]["close"], 41_000.0)

    def test_merge_ignores_incomplete_or_invalid_prior_candles(self) -> None:
        rows = sample_rows(100)
        invalid = [
            {"date": "2026-08-12", "open": 1, "high": 2, "low": 1},
            {"date": "2026-08-13", "open": 2, "high": 1, "low": 1, "close": 2},
        ]
        merged = merge_price_rows(rows, invalid)
        self.assertEqual(len(merged), 100)
        self.assertEqual(merged[-1]["date"], rows[-1]["date"])

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
            per_rows=[{"date": latest_date, "weightedPer": 20.0, "indexPer": 25.0,
                       "weightedPbr": 2.0, "indexPbr": 2.5, "close": 40_189.0,
                       "eps": 1607.56, "bps": 16075.6}],
            per_source={"status": "available", "url": "https://example.test/per", "asOf": latest_date},
            margin={"status": "permission-required", "rows": []},
            investor={"status": "permission-required", "rows": []},
            breadth={"status": "available", "rows": []},
        )
        self.assertEqual(payload["analysisVersion"], ANALYSIS_VERSION)
        self.assertEqual(validate_analysis(payload), [])
        self.assertEqual(payload["per"]["reference"]["indexPer"], 25.0)
        self.assertEqual(payload["per"]["reference"]["lowerMultiple"], 20)
        self.assertEqual(payload["per"]["reference"]["upperMultiple"], 21)
        self.assertEqual(payload["per"]["reference"]["lowerPrice"], 40_189.0)

    def test_parse_official_pbr_and_close_and_reject_misaligned_dates(self) -> None:
        pbr = parse_index_pbr_html("<tr><td>2026.10.08</td><td>1.90</td><td>2.83</td></tr>")
        closes = parse_nikkei_daily_close_html(
            "<tr><td>2026.10.08</td><td>69,840.64</td><td>69,918.20</td>"
            "<td>69,042.11</td><td>69,042.11</td></tr>"
        )
        self.assertEqual(pbr[0]["indexPbr"], 2.83)
        self.assertEqual(closes[0]["close"], 69_042.11)
        with patch("market_analysis.fetch_bytes", side_effect=[
            b"<tr><td>2026.10.07</td><td>17.4</td><td>23.2</td></tr>",
            b"<tr><td>2026.10.08</td><td>1.9</td><td>2.8</td></tr>",
            b"<tr><td>2026.10.08</td><td>1</td><td>2</td><td>1</td><td>2</td></tr>",
        ]):
            rows, source = _per_data({}, distribution_mode=LOCAL_PRIVATE_DISTRIBUTION_MODE)
        self.assertEqual(rows, [])
        self.assertEqual(source["status"], "unavailable")

    def test_adjacent_per_multiples_use_same_day_index_eps(self) -> None:
        rows = [{"date": "2026-10-08", "weightedPer": 17.4, "weightedPbr": 1.5,
                 "indexPer": 20.0, "indexPbr": 2.0,
                 "close": 34_800.0, "eps": 1_740.0, "bps": 17_400.0}]
        reference = _per_reference([], rows)
        self.assertEqual((reference["lowerMultiple"], reference["upperMultiple"]), (17, 18))
        self.assertEqual((reference["lowerPrice"], reference["upperPrice"]), (34_000, 36_000))
        self.assertIsNone(_per_reference([], [{"date": "2026-10-08", "indexPer": None, "eps": None, "bps": None}]))

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

    def test_private_valuation_history_restores_with_service_token(self) -> None:
        prior = {"distributionMode": "private-cloud", "per": {"rows": [{"date": "2026-10-07"}]}}
        with (
            patch.dict("os.environ", {"PRIVATE_HISTORY_SOURCE_URL": "https://private.example.test", "PRIVATE_SERVICE_TOKEN": "secret"}),
            patch("market_analysis.urlopen", return_value=io.BytesIO(json.dumps(prior).encode())) as remote,
        ):
            self.assertEqual(_load_private_previous(), prior)
        request = remote.call_args.args[0]
        self.assertEqual(request.get_header("X-capital-radar-service"), "secret")

    def test_private_valuation_history_requires_token(self) -> None:
        with patch.dict("os.environ", {"PRIVATE_HISTORY_SOURCE_URL": "https://private.example.test", "PRIVATE_SERVICE_TOKEN": ""}):
            with self.assertRaises(MarketAnalysisError):
                _load_private_previous()

    def test_local_private_mode_fetches_restricted_sources(self) -> None:
        per_html = b"""
        <table><tr><td>2026.07.17</td><td>17.42</td><td>22.99</td></tr></table>
        """
        pbr_html = b"<tr><td>2026.07.17</td><td>1.6</td><td>2.1</td></tr>"
        close_html = b"<tr><td>2026.07.17</td><td>39,900</td><td>40,100</td><td>39,800</td><td>40,000</td></tr>"
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
            patch("market_analysis.fetch_bytes", side_effect=[per_html, pbr_html, close_html]),
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
        self.assertEqual(per_rows[-1]["eps"], round(40_000 / 22.99, 2))
        self.assertEqual(per_rows[-1]["bps"], round(40_000 / 2.1, 2))
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

    def test_private_cloud_sources_require_explicit_confirmation(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "NIKKEI_PRIVATE_CLOUD_USE_CONFIRMED": "",
                "JPX_PRIVATE_CLOUD_USE_CONFIRMED": "",
            },
            clear=False,
        ):
            per_rows, per_source = _per_data(
                {},
                distribution_mode=PRIVATE_CLOUD_DISTRIBUTION_MODE,
            )
            margin, investor = _weekly_data(
                {},
                distribution_mode=PRIVATE_CLOUD_DISTRIBUTION_MODE,
            )
        self.assertEqual(per_rows, [])
        self.assertEqual(per_source["status"], "permission-required")
        self.assertEqual(margin["status"], "permission-required")
        self.assertEqual(investor["status"], "permission-required")

    def test_private_cloud_output_is_confined_to_private_data_directory(self) -> None:
        self.assertEqual(
            resolve_output_path(local_private=False, private_cloud=True),
            PRIVATE_OUTPUT.resolve(),
        )
        with self.assertRaises(MarketAnalysisError):
            resolve_output_path(
                local_private=False,
                private_cloud=True,
                requested=ROOT / "outputs" / "data" / "private-analysis.json",
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
