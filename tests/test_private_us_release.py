from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "work"))
from private_us_release import DATA_FILES, prepare_us_release, private_base, remote_json, restore_us_market  # noqa: E402


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
        remote["latest-candidates.json"] = {"generatedAt": generation}
        remote["fundamentals-state-v1.json"] = {"generatedAt": generation}
        remote["nikkei225-analysis.json"] = {"distributionMode": "private-cloud"}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            us_file = root / "us-market.json"
            us_file.write_text(json.dumps({"schemaVersion": 1, "asOf": "2026-10-22",
                                           "usSectors": {"status": "available"}}), encoding="utf-8")
            stage = root / "stage"

            def fake_fetch(_base, relative):
                data = remote[relative.removeprefix("data/")]
                return json.dumps(data).encode(), data

            def fake_prepare(*, private_analysis, destination):
                self.assertTrue(private_analysis.is_file())
                (destination / "data").mkdir(parents=True)

            with patch("private_us_release.remote_json", side_effect=fake_fetch), \
                 patch("private_us_release.prepare_private_site", side_effect=fake_prepare), \
                 patch("private_us_release.validate_private_site", return_value=[]):
                prepare_us_release("https://capital-gain-radar-private.pages.dev", us_file, stage)
            self.assertEqual(json.loads((stage / "data" / "latest-candidates.json").read_text())["generatedAt"], generation)
            self.assertEqual(json.loads((stage / "data" / "us-market.json").read_text())["asOf"], "2026-10-22")

    def test_restore_keeps_us_file_across_japanese_deployment(self) -> None:
        payload = {"schemaVersion": 1, "asOf": "2026-10-22"}
        with tempfile.TemporaryDirectory() as folder:
            with patch("private_us_release.remote_json", return_value=(json.dumps(payload).encode(), payload)):
                self.assertTrue(restore_us_market("https://capital-gain-radar-private.pages.dev", Path(folder)))
            self.assertEqual(json.loads((Path(folder) / "data" / "us-market.json").read_text()), payload)


if __name__ == "__main__":
    unittest.main()
