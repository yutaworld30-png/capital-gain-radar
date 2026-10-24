import json
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "work"))
from edinet_connector import parse_financial_metrics_from_xbrl
from jpx_weekly_connector import parse_current_margin_workbook, parse_margin_workbook
from us_sector_market import SECTORS, build_us_sectors, collect_us_sectors
from valuation_history import BASIS, FIELDS, update_valuation_history, validate_valuation_bundle


class MarketExtensionsTests(unittest.TestCase):
    def points(self):
        return {symbol: SimpleNamespace(url="https://example.test", history=[
            {"date": "2026-10-14", "value": 90}, {"date": "2026-10-15", "value": 100},
            {"date": "2026-10-16", "value": 101 + index},
        ]) for index, symbol in enumerate(SECTORS)}

    def test_us_sectors_share_same_completed_session_and_sort(self):
        payload = build_us_sectors(self.points(), "2026-10-16T21:00:00+00:00")
        self.assertEqual(payload["asOf"], "2026-10-16")
        self.assertEqual(payload["coveredCount"], 11)
        self.assertGreaterEqual(payload["rows"][0]["changeRate"], payload["rows"][-1]["changeRate"])
        during = build_us_sectors(self.points(), "2026-10-16T18:00:00+00:00")
        self.assertEqual(during["asOf"], "2026-10-15")
        self.assertAlmostEqual(during["rows"][0]["changeRate"], 100 / 90 - 1)

    def test_us_mixed_sessions_and_missing_are_not_zero(self):
        points = self.points()
        points["XLK"].history = points["XLK"].history[:2]
        points["XLF"].history[-1]["value"] = 0
        payload = build_us_sectors(points, "2026-10-16T21:00:00+00:00")
        self.assertEqual(payload["coveredCount"], 9)
        self.assertEqual(set(payload["missingSymbols"]), {"XLK", "XLF"})
        points = self.points()
        for symbol in list(SECTORS)[:6]:
            points[symbol].history = points[symbol].history[:2]
        self.assertEqual(build_us_sectors(points, "2026-10-16T21:00:00+00:00")["rows"], [])
        result = collect_us_sectors(lambda *a, **k: (_ for _ in ()).throw(OSError()), "2026-10-16T21:00:00+00:00")
        self.assertEqual(result["status"], "unavailable")

    def current_margin(self, sell_amount=762053):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet["B1"] = datetime(2026, 10, 9)
        sheet["L5"] = "千株、百万円"
        sheet["E6"] = "二市場 Tokyo&Nagoya"
        sheet["B14"] = "信用取引残高合計\nTotal Outstanding Margin Trading"
        sheet["D14"] = "株数Shs."
        sheet["E14"], sheet["G14"] = 366905, 3830621
        sheet["D15"] = "金額Val."
        sheet["E15"], sheet["G15"] = sell_amount, 6708057
        sheet["I15"], sheet["K15"] = 1, 2  # Tokyo only: not the required total.
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()

    def test_margin_image_values_amount_ratio_not_share_ratio(self):
        row = parse_current_margin_workbook(self.current_margin())
        self.assertEqual(row["weekEnd"], "2026-10-09")
        self.assertEqual(row["sellBalance100mYen"], 7620.53)
        self.assertEqual(row["buyBalance100mYen"], 67080.57)
        self.assertEqual(round(row["amountMarginRatio"], 2), 8.80)
        self.assertNotAlmostEqual(row["amountMarginRatio"], row["marginRatio"])
        self.assertIsNone(parse_current_margin_workbook(self.current_margin(0))["amountMarginRatio"])

    def test_company_profile_reads_only_official_structured_fields(self):
        xml = '''<xbrl xmlns:x="http://www.xbrl.org/2003/instance" xmlns:j="urn:test">
        <x:context id="CurrentYearInstant"><x:entity/><x:period><x:instant>2026-03-31</x:instant></x:period></x:context>
        <j:CompanyNameCoverPage>テスト株式会社</j:CompanyNameCoverPage>
        <j:CompanyNameInEnglishCoverPage>Test Corporation</j:CompanyNameInEnglishCoverPage>
        <j:AddressOfRegisteredHeadquarterCoverPage>東京都テスト区</j:AddressOfRegisteredHeadquarterCoverPage>
        <j:NumberOfEmployees contextRef="CurrentYearInstant">1234</j:NumberOfEmployees>
        <j:DescriptionOfBusinessTextBlock>転載しない本文</j:DescriptionOfBusinessTextBlock></xbrl>'''
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("XBRL/PublicDoc/test.xbrl", xml)
        profile = parse_financial_metrics_from_xbrl(output.getvalue())["companyProfile"]
        self.assertEqual(profile["companyName"], "テスト株式会社")
        self.assertEqual(profile["employees"], 1234)
        self.assertNotIn("転載しない本文", json.dumps(profile, ensure_ascii=False))

    def history(self, day="2026-10-16", per=10):
        return {"rowFormat": ["code", "per", "pbr", "dividendYield", "dividendYieldKind"],
                "snapshots": [{"date": day, "historySource": "daily-pipeline", "generatedAt": day + "T18:00:00+09:00",
                               "rows": [["6098", per, 2, 0, "forecast"], ["9999", None, None, .03, "actual"]]},
                              {"date": "2026-01-01", "historySource": "reconstructed", "rows": [["6098", 1, 1, .01, "forecast"]]}]}

    def test_valuation_archive_preserves_observations_separately_from_score_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = update_valuation_history(root, self.history(), "2026-10-16T18:00:00+09:00")
            self.assertEqual(first["stockCount"], 1)
            update_valuation_history(root, self.history("2026-10-19", per=12), "2026-10-19T18:00:00+09:00")
            shard = json.loads((root / "valuation-history/60.json").read_text(encoding="utf-8"))
            self.assertEqual(shard["basis"], BASIS)
            self.assertEqual(shard["rowFormat"], FIELDS)
            self.assertEqual([row[0] for row in shard["stocks"]["6098"]], ["2026-10-16", "2026-10-19"])
            self.assertEqual(shard["stocks"]["6098"][0][3], 0)  # A known 0% is not missing.
            self.assertEqual(shard["stocks"]["6098"][0][6], "2026-10-16T18:00:00+09:00")
            update_valuation_history(root, self.history("2026-10-19", per=13), "2026-10-19T19:00:00+09:00")
            shard = json.loads((root / "valuation-history/60.json").read_text(encoding="utf-8"))
            self.assertEqual(len(shard["stocks"]["6098"]), 2)
            self.assertEqual(shard["stocks"]["6098"][-1][1], 13)
            self.assertEqual(validate_valuation_bundle(root), [])
            shard["stocks"]["6098"][-1][1] = -100
            (root / "valuation-history/60.json").write_text(json.dumps(shard), encoding="utf-8")
            self.assertTrue(validate_valuation_bundle(root))

    def test_valuation_restore_missing_first_file_allowed_but_connection_failure_stops(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def missing(url):
                if url.endswith("latest-candidates.json"):
                    return {}
                raise HTTPError(url, 404, "missing", {}, None)
            update_valuation_history(root, self.history(), "2026-10-16", remote_base="https://example.test/data", loader=missing)
            original = (root / "valuation-history-v1.json").read_bytes()
            def failure(url):
                raise OSError("network unavailable")
            with self.assertRaises(RuntimeError):
                update_valuation_history(root, self.history(), "2026-10-16", remote_base="https://example.test/data", loader=failure)
            self.assertEqual((root / "valuation-history-v1.json").read_bytes(), original)

    def test_archive_bootstrap_handles_spa_html_but_never_loses_declared_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def spa(url):
                if url.endswith("latest-candidates.json"):
                    return {}
                raise ValueError("HTML fallback")
            update_valuation_history(root, self.history(), "2026-10-16", remote_base="https://example.test/data", loader=spa)
            def lost(url):
                if url.endswith("latest-candidates.json"):
                    return {"valuationHistorySummary": {"schemaVersion": 1}}
                raise HTTPError(url, 404, "missing", {}, None)
            with self.assertRaises(RuntimeError):
                update_valuation_history(root, self.history(), "2026-10-17", remote_base="https://example.test/data", loader=lost)

    def test_missing_published_archive_recovers_from_verified_previous_deployment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            recovery = "https://old.example.test/data"
            manifest = {"schemaVersion": 1, "basis": BASIS,
                        "shards": [{"url": "valuation-history/60.json"}]}
            shard = {"schemaVersion": 1, "basis": BASIS, "rowFormat": FIELDS,
                     "stocks": {"6098": [["2026-10-15", 11.0, 2.0, 0, "forecast", None, None]]}}

            def load(url):
                if url.endswith("latest-candidates.json"):
                    return {"valuationHistorySummary": {"schemaVersion": 1}}
                if url == f"{recovery}/valuation-history-v1.json":
                    return manifest
                if url == f"{recovery}/valuation-history/60.json":
                    return shard
                raise HTTPError(url, 404, "missing", {}, None)

            result = update_valuation_history(root, self.history("2026-10-16"), "2026-10-16",
                remote_base="https://current.example.test/data", recovery_base=recovery, loader=load)
            self.assertEqual(result["stockCount"], 1)
            rows = json.loads((root / "valuation-history/60.json").read_text())["stocks"]["6098"]
            self.assertEqual([row[0] for row in rows], ["2026-10-15", "2026-10-16"])
            self.assertEqual(validate_valuation_bundle(root), [])
