from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "work"))
from private_us_release import DATA_FILES, prepare_us_release, private_base, remote_json, restore_us_market  # noqa: E402
from valuation_history import BASIS, FIELDS  # noqa: E402


class PrivateUsReleaseTests(unittest.TestCase):
    def test_only_known_missing_asset_fallback_is_optional(self) -> None:
        class Response:
            def __init__(self, body: bytes):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return self.body

        fallback = b'<!doctype html><meta http-equiv="refresh" content="0; url=./investment-candidate-app.html">'
        with patch.dict("os.environ", {"PRIVATE_SERVICE_TOKEN": "test-token"}), \
             patch("private_us_release.urlopen", return_value=Response(fallback)):
            self.assertIsNone(remote_json("https://capital-gain-radar-private.pages.dev",
                                          "data/us-market.json", optional=True))
        with patch.dict("os.environ", {"PRIVATE_SERVICE_TOKEN": "test-token"}), \
             patch("private_us_release.urlopen", return_value=Response(b"<h1>Login required</h1>")):
            with self.assertRaises(RuntimeError):
                remote_json("https://capital-gain-radar-private.pages.dev",
                            "data/us-market.json", optional=True)

    def test_private_source_must_be_expected_host(self) -> None:
        self.assertEqual(private_base("https://capital-gain-radar-private.pages.dev/"),
                         "https://capital-gain-radar-private.pages.dev")
        for url in ("http://capital-gain-radar-private.pages.dev",
                    "https://example.com", "https://capital-gain-radar-private.pages.dev.evil.test"):
            with self.assertRaises(ValueError):
                private_base(url)

    def test_us_only_release_preserves_existing_japanese_json(self) -> None:
        generation = "2026-10-22T13:23:00+00:00"
        remote = {name: {"schemaVersion": 1} for name in DATA_FILES}
        remote["latest-candidates.json"] = {
            "generatedAt": generation, "valuationHistorySummary": {"schemaVersion": 1},
            "scoreVersion": "3.0.0", "factorVersion": "test-v1", "priceBasis": "adjusted-close",
            "highLookbackDays": 252,
            "priceHistoryBundle": {"status": "available", "recordCount": 1,
                "shards": [{"key": "60", "url": "data/price-history/topix-60.json", "recordCount": 1}]},
        }
        remote["fundamentals-state-v1.json"] = {"generatedAt": generation}
        remote["nikkei225-analysis.json"] = {"distributionMode": "private-cloud"}
        remote["valuation-history-v1.json"] = {
            "schemaVersion": 1, "basis": BASIS, "stockCount": 1,
            "shards": [{"url": "valuation-history/60.json", "stockCount": 1, "pointCount": 1}],
        }
        remote["valuation-history/60.json"] = {
            "schemaVersion": 1, "basis": BASIS, "rowFormat": FIELDS,
            "stocks": {"6098": [["2026-10-22", 12.0, 2.0, 0.03, "forecast", None, None]]},
        }
        remote["price-history/topix-60.json"] = {
            "generatedAt": generation, "scoreVersion": "3.0.0", "factorVersion": "test-v1",
            "priceBasis": "adjusted-close", "highLookbackDays": 252,
            "prices": [{"code": "6098", "chartHistory": [["2026-10-22", 1, 1, 1, 1, 1]] * 50}],
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            us_file = root / "us-market.json"
            us_file.write_text(json.dumps({"schemaVersion": 1, "asOf": "2026-10-22",
                                           "usSectors": {"status": "available"}}), encoding="utf-8")
            stage = root / "stage"

            def fake_fetch(_base, relative):
                data = remote[relative.removeprefix("data/")]
                return json.dumps(data).encode(), data

            def fake_prepare(*, private_analysis, destination, validate_public_analysis):
                self.assertTrue(private_analysis.is_file())
                self.assertFalse(validate_public_analysis)
                (destination / "data").mkdir(parents=True)

            with patch("private_us_release.remote_json", side_effect=fake_fetch), \
                 patch("private_us_release.prepare_private_site", side_effect=fake_prepare), \
                 patch("private_us_release.validate_private_site", return_value=[]):
                prepare_us_release("https://capital-gain-radar-private.pages.dev", us_file, stage)
            self.assertEqual(json.loads((stage / "data" / "latest-candidates.json").read_text())["generatedAt"], generation)
            self.assertEqual(json.loads((stage / "data" / "us-market.json").read_text())["asOf"], "2026-10-22")
            self.assertEqual(json.loads((stage / "data" / "valuation-history/60.json").read_text())["stocks"]["6098"][0][1], 12.0)
            self.assertEqual(json.loads((stage / "data" / "price-history/topix-60.json").read_text())["prices"][0]["code"], "6098")

            def missing_price(base, relative):
                if relative == "data/price-history/topix-60.json":
                    raise RuntimeError("price shard missing")
                return fake_fetch(base, relative)

            with patch("private_us_release.remote_json", side_effect=missing_price):
                with self.assertRaisesRegex(RuntimeError, "price shard missing"):
                    prepare_us_release("https://capital-gain-radar-private.pages.dev", us_file, root / "missing-stage")

    def test_us_release_stops_if_declared_valuation_history_is_missing(self) -> None:
        generation = "2026-10-22T13:23:00+00:00"
        remote = {name: {"schemaVersion": 1} for name in DATA_FILES}
        remote["latest-candidates.json"] = {"generatedAt": generation, "valuationHistorySummary": {"schemaVersion": 1}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            us_file = root / "us-market.json"
            us_file.write_text(json.dumps({"schemaVersion": 1, "asOf": "2026-10-22",
                                           "usSectors": {"status": "available"}}), encoding="utf-8")

            def fake_fetch(_base, relative):
                name = relative.removeprefix("data/")
                if name == "valuation-history-v1.json":
                    raise RuntimeError("archive missing")
                payload = remote[name]
                return json.dumps(payload).encode(), payload

            with patch("private_us_release.remote_json", side_effect=fake_fetch):
                with self.assertRaisesRegex(RuntimeError, "archive missing"):
                    prepare_us_release("https://capital-gain-radar-private.pages.dev", us_file, root / "stage")

    def test_restore_keeps_us_file_across_japanese_deployment(self) -> None:
        payload = {"schemaVersion": 1, "asOf": "2026-10-22"}
        with tempfile.TemporaryDirectory() as folder:
            with patch("private_us_release.remote_json", return_value=(json.dumps(payload).encode(), payload)):
                self.assertTrue(restore_us_market("https://capital-gain-radar-private.pages.dev", Path(folder)))
            self.assertEqual(json.loads((Path(folder) / "data" / "us-market.json").read_text()), payload)


if __name__ == "__main__":
    unittest.main()
