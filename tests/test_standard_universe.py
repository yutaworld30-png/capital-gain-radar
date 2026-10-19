import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "work"))
import fetch_official_data as pipeline
import test_output_validation as fixtures
from stock_universe import UNIVERSE_ID, listing_errors, screening_components, price_coverage_by_group
from validate_output import validate_dataset, validate_history


def extended_dataset():
    payload = fixtures.OutputValidationTest().valid_dataset()
    topix = [{**row, "isTopix": True, "isStandard": index >= 1400,
              "exchangeSection": "スタンダード" if index >= 1400 else "プライム"}
             for index, row in enumerate(payload["topixComponents"])]
    standard_only = [{"code": str(code), "name": "standard", "isTopix": False,
                      "isStandard": True, "exchangeSection": "スタンダード"}
                     for code in range(2500, 3500)]
    payload.update({"topixComponents": topix, "standardComponents": topix[1400:] + standard_only,
                    "listedComponents": topix + standard_only})
    payload["universe"] = {"id": UNIVERSE_ID, "expectedCount": 2500}
    payload["sources"]["standard"] = {"status": "available", "asOf": "2026-07-14"}
    payload["sources"]["priceHistory"]["coverageByGroup"] = price_coverage_by_group(payload, payload["listedComponents"])
    payload["priceHistoryBundle"]["recordCount"] = 2500
    payload["priceHistoryBundle"]["shards"][0]["recordCount"] = 2500
    payload["searchUniverse"][0].update({"isStandard": False, "exchangeSection": "プライム"})
    return payload


class StandardUniverseTests(unittest.TestCase):
    def test_official_listing_union_excludes_other_products_and_deduplicates_overlap(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["日付", "コード", "銘柄名", "市場・商品区分", "規模区分"])
        for code, market, size in [(1001, "プライム（内国株式）", "TOPIX Small 1"),
                                   (1002, "スタンダード（内国株式）", "TOPIX Small 2"),
                                   (1003, "スタンダード（内国株式）", "-"),
                                   ("409A", "スタンダード（内国株式）", "-"),
                                   (1004, "グロース（内国株式）", "-"),
                                   (1005, "ETF・ETN", "-"),
                                   (1006, "スタンダード（外国株式）", "-")]:
            sheet.append([20260930, code, str(code), market, size])
        sheet.append([20260930, 1002, "duplicate", "スタンダード（内国株式）", "TOPIX Small 2"])
        content = BytesIO()
        workbook.save(content)
        rows, as_of = pipeline.parse_topix_components(content.getvalue(), {"1001"}, include_standard=True)
        self.assertEqual(as_of, "2026-09-30")
        self.assertEqual([row["code"] for row in rows], ["1001", "1002", "1003", "409A"])
        self.assertTrue(rows[1]["isStandard"] and rows[1]["isTopix"])
        self.assertTrue(rows[2]["isStandard"])
        self.assertFalse(rows[2]["isTopix"])

    def test_extended_listing_and_output_pass_and_legacy_remains_supported(self):
        payload = extended_dataset()
        self.assertEqual(listing_errors(payload), [])
        self.assertEqual(len(screening_components(payload)), 2500)
        self.assertEqual(validate_dataset(payload, today=date(2026, 7, 14)), [])
        self.assertEqual(listing_errors(fixtures.OutputValidationTest().valid_dataset()), [])

    def test_incomplete_listing_never_falls_back_to_topix_only(self):
        payload = extended_dataset()
        payload.pop("listedComponents")
        self.assertEqual(screening_components(payload), [])
        self.assertTrue(listing_errors(payload))

    def test_bad_membership_duplicate_and_unverified_standard_source_fail(self):
        for kind in ("duplicate", "flag", "source"):
            payload = extended_dataset()
            if kind == "duplicate":
                payload["listedComponents"].append(payload["listedComponents"][-1])
            elif kind == "flag":
                payload["listedComponents"][-1]["isTopix"] = True
            else:
                payload["sources"]["standard"]["status"] = "stale-fallback"
            self.assertTrue(validate_dataset(payload, today=date(2026, 7, 14)), kind)

    def test_price_coverage_is_required_per_group_not_just_union(self):
        payload = extended_dataset()
        payload["sources"]["priceHistory"]["coverageByGroup"]["standard"]["verifiedCount"] = 1044
        self.assertTrue(any("standard" in error for error in validate_dataset(payload, today=date(2026, 7, 14))))
        stats = price_coverage_by_group(payload, payload["topixComponents"])
        self.assertEqual(stats["topix"]["coverage"], 1)
        self.assertLess(stats["standard"]["coverage"], .95)

    def test_different_listing_dates_are_not_mixed(self):
        payload = extended_dataset()
        payload["sources"]["standard"]["asOf"] = "2026-06-30"
        self.assertTrue(any("基準日" in error for error in validate_dataset(payload, today=date(2026, 7, 14))))

    def test_expanded_price_files_use_two_character_prefixes(self):
        history = [[f"2026-07-{day:02d}", 100, 110, 90, 105, 1000] for day in range(1, 6)]
        dataset = {"universe": {"id": UNIVERSE_ID}, "topixPrices": [
            {"code": code, "chartHistory": history} for code in ("1001", "1002", "3001", "409A")]}
        bundle, payloads = pipeline.build_price_history_shards(dataset, "2026-07-14")
        self.assertEqual(set(payloads), {"10", "30", "40"})
        self.assertEqual(bundle["recordCount"], 4)
        self.assertEqual(len(payloads["10"]["prices"]), 2)
        self.assertTrue(all(entry["byteSize"] < 25*1024*1024 for entry in bundle["shards"]))

    def test_standard_candidate_with_missing_margin_is_not_lost_or_given_zero_ratio(self):
        row = extended_dataset()["listedComponents"][-1]
        dataset = {"universe": {"id": UNIVERSE_ID}, "listedComponents": [row],
                   "topixPrices": [{"code": row["code"], "latestClose": 100, "high52w": 110,
                                    "monthsFromHigh": 1, "technical": 70, "liquidity": 70, "risk": 20,
                                    "asOf": "2026-07-14", "sources": {}}], "topixMargin": [],
                   "sources": {key: {"status": "available", "asOf": "2026-07-14"}
                               for key in ("topix", "standard", "priceHistory", "marginWeekly", "themeNews", "fundamentals")}}
        with patch.object(pipeline, "fetch_recent_disclosures", return_value=([], [])), \
             patch.object(pipeline, "fetch_theme_news_counts", return_value=({}, [])), \
             patch.object(pipeline, "analyze_disclosures", return_value=([{"code": row["code"], "earnings": 50}], [], {})), \
             patch.object(pipeline, "augment_candidate_chart_histories"):
            pipeline.collect_tdnet_and_build_candidates(dataset, "2026-07-14T18:00:00+09:00")
        candidate = dataset["searchUniverse"][0]
        self.assertIsNone(candidate["margin"])
        self.assertFalse(candidate["isTopix"])
        self.assertTrue(candidate["isStandard"])
        pipeline._attach_candidate_source_statuses(dataset)
        pipeline._attach_scores(dataset)
        self.assertEqual(candidate["supply"], 0)
        self.assertEqual(candidate["sources"]["marginRatio"]["status"], "unavailable")
        self.assertTrue(any("信用" in text for text in candidate["dataWarnings"]))

    def test_topix_paid_opt_in_does_not_expand_api_requests(self):
        with patch.dict(os.environ, {"JQUANTS_API_KEY": "fixture-not-a-real-key", "JQUANTS_TOPIX_ENABLED": "true"}):
            self.assertFalse(pipeline.collect_jquants_metrics({"universe": {"id": UNIVERSE_ID}, "sources": {"priceHistory": {}, "fundamentals": {}}}, "2026-07-14"))

    def test_free_collector_requests_each_union_code_once_and_preserves_membership(self):
        rows = extended_dataset()["listedComponents"]
        components = [rows[1], rows[1400], rows[-1]]
        dataset = {"universe": {"id": UNIVERSE_ID}, "listedComponents": components,
                   "topixComponents": components[:2], "standardComponents": components[1:],
                   "sources": {"priceHistory": {}, "fundamentals": {}}}
        today = date.today()
        history = [{"Date": (today - timedelta(days=299-index)).isoformat(), "O": 100+index,
                    "H": 102+index, "L": 99+index, "C": 101+index, "V": 1000000,
                    "AdjH": 102+index, "AdjL": 99+index, "AdjC": 101+index, "Va": (101+index)*1000000}
                   for index in range(300)]
        mirror = {row["code"]: {"date": today.isoformat(), "close": 400} for row in components}
        with patch.object(pipeline, "listing_errors", return_value=[]), \
             patch.object(pipeline, "fetch_yahoo_mirror_latest", return_value=mirror) as quotes, \
             patch.object(pipeline, "fetch_yahoo_history", return_value=(history, "https://example.test/prices")) as prices, \
             patch.object(pipeline.time, "sleep"):
            pipeline.collect_free_market_metrics(dataset, today.isoformat(), {})
        self.assertEqual(prices.call_count, 3)
        self.assertEqual(len(quotes.call_args.args[0]), 3)
        self.assertEqual(len(dataset["topixPrices"]), 3)
        self.assertFalse(next(row for row in dataset["topixPrices"] if row["code"] == components[-1]["code"])["isTopix"])
        self.assertEqual(dataset["sources"]["priceHistory"]["status"], "available")

    def test_factor_version_migration_retains_old_history_across_updates(self):
        old = fixtures.OutputValidationTest().valid_history()
        dataset = fixtures.OutputValidationTest().valid_dataset()
        dataset["factorVersion"] = pipeline.FACTOR_VERSION
        dataset["searchUniverse"][0]["factorVersion"] = pipeline.FACTOR_VERSION
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(pipeline, "SCORE_HISTORY_OUTPUT", Path(folder) / "score-history-v2.json"), \
             patch.object(pipeline, "load_json_url", return_value=old), \
             patch.dict(os.environ, {"SCORE_HISTORY_REQUIRE_REMOTE": "true", "SCORE_HISTORY_URL": "https://example.test/score-history-v2.json"}):
            updated = pipeline.update_score_history(dataset, "2026-07-14T18:00:00+09:00")
            self.assertEqual(updated["restoredSnapshotCount"], 0)
            self.assertEqual(updated["archivedVersions"][0]["snapshots"], old["snapshots"])
            self.assertEqual(updated["snapshots"][0]["factorVersion"], pipeline.FACTOR_VERSION)
            again = pipeline.update_score_history(dataset, "2026-07-14T19:00:00+09:00")
            self.assertEqual(len(again["archivedVersions"]), 1)
            self.assertEqual(validate_history(again, dataset), [])
            saved = json.loads(pipeline.SCORE_HISTORY_OUTPUT.read_text(encoding="utf-8"))
            self.assertEqual(saved["archivedVersions"][0]["factorVersion"], old["factorVersion"])
            before = pipeline.SCORE_HISTORY_OUTPUT.read_bytes()
            old["snapshots"][0]["date"] = "2026-07-15"
            with self.assertRaisesRegex(RuntimeError, "旧版"):
                pipeline.update_score_history(dataset, "2026-07-14T20:00:00+09:00")
            self.assertEqual(before, pipeline.SCORE_HISTORY_OUTPUT.read_bytes())
