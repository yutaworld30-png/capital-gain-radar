from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "work"))
import fundamental_store as store
from edinet_connector import calculate_valuation_metrics, fetch_recent_securities_reports, parse_financial_metrics_from_xbrl
from edinet_connector import EdinetError
from free_market_connector import fetch_yahoo_dividend_forecast, FreeMarketDataError

NOW = "2026-10-19T16:10:00+09:00"
FACTS = {"eps": 100., "bps": 1000., "dps": 20., "actualDps": 20.,
         "actualDpsAsOf": "2026-03-31", "asOf": "2026-03-31", "profit": 1000., "equity": 10000.,
         "docID": "DOC1", "factVersion": store.FACT_VERSION, "url": "https://example.test/report"}


def dataset(codes=("1000", "1001", "130A")):
    return {"searchUniverse": [{"code": c, "latestClose": 2000., "priceAsOf": NOW[:10], "isStandard": True} for c in codes],
            "candidates": [], "sources": {"edinetFundamentals": {}}}


class FundamentalStoreTest(unittest.TestCase):
    def test_annual_actual_dps_uses_business_results_not_interim_or_preferred(self):
        xbrl = '''<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:j="https://example.test/jpcrp">
          <context id="CurrentYearDuration_NonConsolidatedMember"><entity><identifier scheme="test">1</identifier></entity><period><startDate>2025-04-01</startDate><endDate>2026-03-31</endDate></period><scenario>NonConsolidatedMember</scenario></context>
          <context id="PriorYearDuration_NonConsolidatedMember"><entity><identifier scheme="test">1</identifier></entity><period><startDate>2024-04-01</startDate><endDate>2025-03-31</endDate></period><scenario>NonConsolidatedMember</scenario></context>
          <j:DividendPaidPerShareSummaryOfBusinessResults contextRef="PriorYearDuration_NonConsolidatedMember">8</j:DividendPaidPerShareSummaryOfBusinessResults>
          <j:DividendPaidPerShareSummaryOfBusinessResults contextRef="CurrentYearDuration_NonConsolidatedMember">10</j:DividendPaidPerShareSummaryOfBusinessResults>
          <j:InterimDividendPaidPerShareSummaryOfBusinessResults contextRef="CurrentYearDuration_NonConsolidatedMember">5</j:InterimDividendPaidPerShareSummaryOfBusinessResults>
          <j:DividendPaidPerShareClassDPreferredSharesSummaryOfBusinessResults contextRef="CurrentYearDuration_NonConsolidatedMember">50</j:DividendPaidPerShareClassDPreferredSharesSummaryOfBusinessResults>
        </xbrl>'''
        archive_bytes = io.BytesIO()
        with ZipFile(archive_bytes, "w") as archive:
            archive.writestr("XBRL/PublicDoc/jpcrp030000-asr.xbrl", xbrl)
        result = parse_financial_metrics_from_xbrl(archive_bytes.getvalue())
        self.assertEqual(result["actualDps"], 10)
        self.assertEqual(result["actualDpsAsOf"], "2026-03-31")

    def test_missing_dps_never_becomes_roe(self):
        value = calculate_valuation_metrics({"profit": 100, "equity": 1000, "eps": 10, "bps": 100}, 100)
        self.assertIsNone(value["doe"])
        self.assertIsNone(value["dividendPayoutRatio"])
        self.assertNotEqual(value["dividendPayoutRatioStatus"], "calculated")

    def test_forecast_actual_zero_and_periods(self):
        value = calculate_valuation_metrics({**FACTS, "dps": 40, "dpsKind": "forecast", "dividendYield": .02}, 2000)
        self.assertEqual(value["actualDps"], 20)
        self.assertEqual(value["forecastDps"], 40)
        self.assertEqual(value["dividendPayoutRatio"], .2)
        self.assertEqual(value["doe"], .02)
        zero = calculate_valuation_metrics({**FACTS, "actualDps": 0, "dps": 0}, 2000)
        self.assertEqual(zero["dividendPayoutRatio"], 0)
        self.assertEqual(zero["doe"], 0)
        mismatch = calculate_valuation_metrics({**FACTS, "actualDpsAsOf": "2025-03-31"}, 2000)
        self.assertIsNone(mismatch["dividendPayoutRatio"])
        self.assertIsNone(mismatch["doe"])
        loss = calculate_valuation_metrics({**FACTS, "eps": -10, "bps": 0}, 2000)
        self.assertIsNone(loss["per"])
        self.assertIsNone(loss["pbr"])
        self.assertEqual(loss["dividendPayoutRatioStatus"], "not-applicable")

    def test_yahoo_zero_and_optional_glossary(self):
        page = "配当利回り(会社予想) 0.00 % (10/19) 1株配当（会社予想） 0.00 円 （2027/03）"
        with patch("free_market_connector._fetch", return_value=page.encode()):
            result = fetch_yahoo_dividend_forecast("130A")
        self.assertEqual(result["dps"], 0)
        self.assertEqual(result["dividendYield"], 0)
        self.assertEqual(result["eventStatus"]["exDividendDate"], "not-found-on-page")

    def test_invalid_dps_does_not_discard_valid_yield(self):
        page = "配当利回り（会社予想） 用語 2.00 % （10/19） 1株配当（会社予想） 用語 99999 円 （2027/03）"
        with patch("free_market_connector._fetch", return_value=page.encode()):
            result = fetch_yahoo_dividend_forecast("1000")
        self.assertNotIn("dps", result)
        self.assertEqual(result["dividendYield"], .02)

    def test_null_does_not_refresh_prior_field_date(self):
        old = store.merge_forecast({}, {"dps": 20, "dividendYield": .01}, "2026-10-01")
        new = store.merge_forecast(old, {"dps": None, "dividendYield": None, "exDividendDate": "2026-12-29"}, NOW)
        self.assertEqual(new["dps"], 20)
        self.assertEqual(new["observedAt"]["dividendYield"], "2026-10-01")

    def test_roundtrip_and_invalid_state(self):
        state = store.seed_state({"edinetFundamentals": [{"code": "130A", **FACTS}]})
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"FUNDAMENTALS_STATE_URL": ""}):
            path = Path(directory) / "state.json"
            store.save_state(state, path)
            self.assertEqual(store.load_state({}, None, path), state)
        state["companies"]["130A"]["financial"]["eps"] = float("nan")
        with self.assertRaises(ValueError):
            store.validate_state(state)

    def test_remote_404_bootstrap_and_auth_fail_closed(self):
        def loader(url):
            if url.endswith("fundamentals-state-v1.json"):
                raise HTTPError(url, 404, "missing", {}, None)
            return {"searchUniverse": [{"code": "130A", "fundamentals": FACTS}]}
        with patch.dict(os.environ, {"FUNDAMENTALS_STATE_URL": "https://private.test/data/fundamentals-state-v1.json"}):
            state = store.load_state({}, loader)
            self.assertIn("130A", state["companies"])
            with self.assertRaises(RuntimeError):
                store.load_state({}, lambda url: (_ for _ in ()).throw(HTTPError(url, 403, "denied", {}, None)))
            with self.assertRaises(ValueError):
                store.load_state({}, lambda url: {"login": "required"})

    def test_html_fallback_bootstraps_only_before_new_factor_version(self):
        def loader(url):
            if url.endswith("fundamentals-state-v1.json"):
                raise json.JSONDecodeError("HTML fallback", "<html>", 0)
            return {"factorVersion": "topix-standard-capital-gain-v3.1",
                    "searchUniverse": [{"code": "130A", "fundamentals": FACTS}]}
        with patch.dict(os.environ, {"FUNDAMENTALS_STATE_URL": "https://private.test/data/fundamentals-state-v1.json"}):
            self.assertIn("130A", store.load_state({}, loader)["companies"])

            def after_migration(url):
                result = loader(url)
                result["factorVersion"] = "topix-standard-capital-gain-v3.2"
                return result

            with self.assertRaisesRegex(ValueError, "missing after migration"):
                store.load_state({}, after_migration)

    def test_queue_rotates_and_transient_failure_retries_next_day(self):
        state = store.seed_state({})
        codes = [str(1000 + i) for i in range(6)]
        first = store.select_codes(codes, state, "edinet", 2, NOW)
        for code in first:
            state["companies"][code] = {"edinetAttempt": {"at": NOW, "status": "error"}}
        second = store.select_codes(codes, state, "edinet", 2, NOW)
        for code in second:
            state["companies"][code] = {"financial": FACTS, "edinetAttempt": {"at": NOW, "status": "available"}}
        third = store.select_codes(codes, state, "edinet", 2, NOW)
        self.assertEqual(len(set(first + second + third)), 6)
        self.assertEqual(store.select_codes(first, state, "edinet", 2, "2026-10-20"), first)
        for code in second + third:
            state["companies"].setdefault(code, {})["edinetAttempt"] = {"at": NOW, "status": "available"}
        self.assertEqual(store.select_codes(codes, state, "edinet", 2, "2026-10-20"), first)

    def test_missing_source_field_keeps_weekly_cooldown(self):
        state = store.seed_state({})
        state["companies"]["1000"] = {
            "yahooAttempt": {"at": NOW, "status": "not-found", "reason": "not-found-on-page"}
        }
        self.assertEqual(store.select_codes(["1000"], state, "yahoo", 1, "2026-10-20"), [])
        self.assertEqual(store.select_codes(["1000"], state, "yahoo", 1, "2026-10-27"), ["1000"])

    def test_manual_refresh_retries_only_same_day_errors(self):
        state = store.seed_state({})
        state["companies"] = {
            "1000": {"yahooAttempt": {"at": NOW, "status": "error", "reason": "HTTP 500"}},
            "1001": {"yahooAttempt": {"at": NOW, "status": "not-found"}},
            "1002": {"yahooAttempt": {"at": NOW, "status": "available"}},
        }
        codes = ["1000", "1001", "1002"]
        self.assertEqual(store.select_codes(codes, state, "yahoo", 3, NOW), [])
        self.assertEqual(
            store.select_codes(codes, state, "yahoo", 3, NOW, retry_failed_today=True),
            ["1000"],
        )

    def test_outdated_financial_fact_retries_once_on_same_day(self):
        state = store.seed_state({})
        state["companies"]["1000"] = {"financial": {**FACTS, "factVersion": store.FACT_VERSION - 1},
                                     "edinetAttempt": {"at": NOW, "status": "available", "docID": "DOC1"}}
        self.assertEqual(store.select_codes(["1000"], state, "edinet", 1, NOW), ["1000"])
        state["companies"]["1000"]["edinetAttempt"]["factVersion"] = store.FACT_VERSION
        self.assertEqual(store.select_codes(["1000"], state, "edinet", 1, NOW), [])

    def test_alpha_correction_and_cached_index(self):
        day = date.today().isoformat()
        payload = {"results": [
            {"secCode": "130A0", "docTypeCode": "120", "docID": "D1", "submitDateTime": day + " 09:00"},
            {"secCode": "130A0", "docTypeCode": "130", "docID": "D2", "submitDateTime": day + " 10:00"},
            {"secCode": "130A0", "docTypeCode": "130", "docID": "D3", "submitDateTime": day + " 11:00", "withdrawalStatus": "1"},
        ]}
        index = {}
        with patch("edinet_connector._json", return_value=payload) as fetch:
            reports, errors = fetch_recent_securities_reports({"130A"}, "test", 1, index=index, checked_at=NOW)
            again, _ = fetch_recent_securities_reports({"130A"}, "test", 1, index=index, checked_at=NOW)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(reports["130A"]["docID"], "D2")
        self.assertEqual(reports, again)
        self.assertEqual(errors, [])

    def test_disclosure_resumed_but_withdrawn_and_non_xbrl_excluded(self):
        day = date.today().isoformat()
        base = {"secCode": "130A0", "docTypeCode": "120", "submitDateTime": day + " 09:00",
                "withdrawalStatus": "0", "xbrlFlag": "1", "legalStatus": "1"}
        payload = {"results": [
            {**base, "docID": "VISIBLE", "disclosureStatus": "3"},
            {**base, "docID": "HIDDEN", "submitDateTime": day + " 10:00", "disclosureStatus": "2"},
            {**base, "docID": "WITHDRAWN", "submitDateTime": day + " 11:00", "withdrawalStatus": "2"},
            {**base, "docID": "NO_XBRL", "submitDateTime": day + " 12:00", "xbrlFlag": "0"},
            {**base, "docID": "EXPIRED", "submitDateTime": day + " 13:00", "legalStatus": "0"},
        ]}
        with patch("edinet_connector._json", return_value=payload):
            reports, errors = fetch_recent_securities_reports({"130A"}, "test", 1, index={})
        self.assertEqual(errors, [])
        self.assertEqual(reports["130A"]["docID"], "VISIBLE")

    def test_report_index_stops_after_three_api_failures(self):
        with patch("edinet_connector._json", side_effect=EdinetError("HTTP 503")) as fetch:
            reports, errors = fetch_recent_securities_reports({"130A"}, "test", 430, index={})
        self.assertEqual(reports, {})
        self.assertEqual(len(errors), 3)
        self.assertEqual(fetch.call_count, 3)

    def test_failure_preserves_facts_with_key_and_other_batch(self):
        state = store.seed_state({})
        state["companies"]["1000"] = {"financial": dict(FACTS)}
        data = dataset()
        with patch.dict(os.environ, {"EDINET_API_KEY": "test", "EDINET_MAX_DOWNLOADS": "1", "YAHOO_COMPANY_MAX_DOWNLOADS": "1", "YAHOO_DIVIDEND_REQUEST_DELAY_SECONDS": "0"}), \
             patch.object(store, "fetch_yahoo_dividend_forecast", side_effect=FreeMarketDataError("offline")), \
             patch.object(store, "fetch_recent_securities_reports", return_value=({}, ["offline"])):
            store.refresh_fundamentals(data, NOW, state)
        self.assertEqual(data["searchUniverse"][0]["per"], 20)
        self.assertEqual(state["companies"]["1000"]["financial"], FACTS)
        self.assertEqual(data["sources"]["edinetFundamentals"]["targetCount"], 3)
        self.assertEqual(data["financialCoverage"]["standard"]["fields"]["per"]["available"], 1)
        self.assertIsNone(data["searchUniverse"][0]["dividendYield"])

    def test_coverage_and_payout_validation_reject_tampering(self):
        data = dataset(("1000",))
        state = store.seed_state({})
        state["companies"]["1000"] = {"financial": dict(FACTS)}
        store.attach_facts(data, state, NOW)
        data["financialCoverage"] = store.coverage(data["searchUniverse"])
        self.assertEqual(store.validate_coverage(data), [])
        data["searchUniverse"][0]["doe"] = 9
        self.assertTrue(any("inconsistent doe basis" in error for error in store.validate_coverage(data)))
        data["searchUniverse"][0]["doe"] = .02
        data["financialCoverage"]["all"]["fields"]["doe"]["available"] = 0
        self.assertIn("Financial coverage does not match stock rows", store.validate_coverage(data))

    def test_zero_forecast_yield_still_requires_forecast_basis(self):
        data = dataset(("1000",))
        data["searchUniverse"][0]["dividendYield"] = 0
        data["financialCoverage"] = store.coverage(data["searchUniverse"])
        self.assertIn("1000: primary yield is not a forecast", store.validate_coverage(data))

    def test_unchanged_document_not_downloaded_and_new_doc_downloaded(self):
        state = store.seed_state({})
        state["companies"]["1000"] = {"financial": dict(FACTS)}
        report = {"1000": {"docID": "DOC1", "code": "1000"}}
        with patch.dict(os.environ, {"EDINET_API_KEY": "test", "YAHOO_DIVIDEND_REQUEST_DELAY_SECONDS": "0"}), \
             patch.object(store, "fetch_yahoo_dividend_forecast", return_value={"dps": 40, "dpsKind": "forecast", "dividendYield": .02}), \
             patch.object(store, "fetch_recent_securities_reports", return_value=(report, [])), \
             patch.object(store, "download_xbrl_zip", return_value=(b"fixture", "https://example.test")) as download, \
             patch.object(store, "parse_financial_metrics_from_xbrl", return_value=dict(FACTS)):
            data = dataset(("1000",))
            store.refresh_fundamentals(data, NOW, state)
            download.assert_not_called()
            report["1000"]["docID"] = "DOC2"
            store.refresh_fundamentals(data, "2026-10-20T16:10:00+09:00", state)
            self.assertEqual(download.call_count, 1)
        self.assertEqual(data["searchUniverse"][0]["dividendPayoutRatio"], .2)
        self.assertEqual(state["companies"]["1000"]["financial"]["dps"], 20)
        self.assertEqual(len(state["companies"]["1000"]["financial"]["dataHash"]), 64)


if __name__ == "__main__":
    unittest.main()
