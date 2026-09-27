import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "work"))
from margin_history import merge, update, validate, validate_files


class MarginHistoryTest(unittest.TestCase):
    def dataset(self, buy=200, sell=100, day="2026-09-18", status="available"):
        return {"sources": {"marginWeekly": {"status": status, "asOf": day, "updatedAt": "2026-09-24"}},
                "topixMargin": [{"code": "9433", "outstandingPurchases": buy, "outstandingSales": sell}]}

    def empty(self):
        return {"schemaVersion": 1, "stocks": {}}

    def test_ratio_zero_missing_and_invalid(self):
        for buy, sell, expected in [(200, 100, 2), (0, 100, 0), (200, 0, None), (None, 100, None), (-1, 100, None)]:
            result = merge(self.empty(), self.dataset(buy, sell), "2026-09-27")
            self.assertEqual(result["stocks"]["9433"][0]["ratio"], expected)

    def test_revision_deduplication_and_stale(self):
        first = merge(self.empty(), self.dataset(), "2026-09-25")
        revised = merge(first, self.dataset(300), "2026-09-27")
        self.assertEqual(len(revised["stocks"]["9433"]), 1)
        self.assertEqual(revised["stocks"]["9433"][0]["ratio"], 3)
        stale = merge(revised, self.dataset(day="2026-09-25", status="stale-fallback"), "2026-09-27")
        self.assertEqual(stale["stocks"], revised["stocks"])

    def test_schema_rejects_bad_ratio(self):
        result = merge(self.empty(), self.dataset(), "2026-09-27")
        result["stocks"]["9433"][0]["ratio"] = 9
        with self.assertRaises(ValueError): validate(result)

    def test_retention(self):
        old = merge(self.empty(), self.dataset(day="2025-01-03"), "2025-01-04")
        result = merge(old, self.dataset(), "2026-09-27")
        self.assertEqual(len(result["stocks"]["9433"]), 1)

    def test_restore_and_shard(self):
        remote = merge(self.empty(), self.dataset(), "2026-09-24")
        with tempfile.TemporaryDirectory() as directory:
            result = update(directory, self.dataset(day="2026-09-25"), "2026-09-27", "https://example.test", lambda url: {"schemaVersion": 2, "sources": {}, "marginHistorySummary": {"schemaVersion": 1}} if url.endswith("latest-candidates.json") else remote)
            self.assertEqual(len(result["stocks"]["9433"]), 2)
            shard = json.loads((Path(directory) / "margin-history/9433.json").read_text())
            self.assertEqual(shard["rows"], result["stocks"]["9433"])
            validate_files(directory)
            shard["rows"] = []
            (Path(directory) / "margin-history/9433.json").write_text(json.dumps(shard))
            with self.assertRaises(ValueError): validate_files(directory)

    def test_restore_failure_preserves_file(self):
        def fail(url): raise HTTPError(url, 403, "Forbidden", None, None)
        with tempfile.TemporaryDirectory() as directory:
            update(directory, self.dataset(), "2026-09-27")
            path = Path(directory) / "margin-history-v1.json"
            before = path.read_bytes()
            with self.assertRaises(RuntimeError):
                update(directory, self.dataset(), "2026-09-27", "https://example.test", fail)
            self.assertEqual(path.read_bytes(), before)

    def test_bootstrap_and_missing_remote_gate(self):
        for initialized in (False, True):
            def loader(url):
                if url.endswith("latest-candidates.json"):
                    return {"schemaVersion": 2, "sources": {}, **({"marginHistorySummary": {"schemaVersion": 1}} if initialized else {})}
                raise HTTPError(url, 404, "Not found", None, None)
            with tempfile.TemporaryDirectory() as directory:
                if initialized:
                    with self.assertRaises(RuntimeError):
                        update(directory, self.dataset(), "2026-09-27", "https://example.test/margin-history-v1.json", loader)
                else:
                    result = update(directory, self.dataset(), "2026-09-27", "https://example.test/margin-history-v1.json", loader)
                    self.assertEqual(len(result["stocks"]), 1)

    def test_first_release_does_not_request_nonexistent_asset(self):
        requests = []
        def loader(url):
            requests.append(url)
            if url.endswith("latest-candidates.json"):
                return {"schemaVersion": 2, "sources": {}}
            raise json.JSONDecodeError("HTML fallback", "<html>", 0)
        with tempfile.TemporaryDirectory() as directory:
            update(directory, self.dataset(), "2026-09-27", "https://example.test/margin-history-v1.json", loader)
        self.assertEqual(len(requests), 1)

    def test_invalid_dataset_is_not_a_bootstrap_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                update(directory, self.dataset(), "2026-09-27", "https://example.test/margin-history-v1.json", lambda url: {})


if __name__ == "__main__":
    unittest.main()
