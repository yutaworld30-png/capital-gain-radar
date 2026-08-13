from __future__ import annotations

import json
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from free_market_connector import (  # noqa: E402
    fetch_yahoo_history,
    fetch_yahoo_mirror_latest,
    fetch_yahoo_spark_histories,
)


class FreeMarketConnectorTests(unittest.TestCase):
    @staticmethod
    def _timestamps(count: int = 253) -> list[int]:
        start = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
        return [start + index * 86_400 for index in range(count)]

    @classmethod
    def _chart_payload(cls, *, market_hour: int) -> bytes:
        timestamps = cls._timestamps()
        closes = [1_000.0 + index for index in range(len(timestamps))]
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": timestamps,
                        "meta": {
                            "exchangeTimezoneName": "UTC",
                            "regularMarketTime": timestamps[-1] + market_hour * 3_600,
                        },
                        "indicators": {
                            "quote": [
                                {
                                    "open": closes,
                                    "high": [value + 10 for value in closes],
                                    "low": [value - 10 for value in closes],
                                    "close": closes,
                                    "volume": [10_000] * len(closes),
                                }
                            ],
                            "adjclose": [{"adjclose": closes}],
                        },
                    }
                ]
            }
        }
        return json.dumps(payload).encode("utf-8")

    @classmethod
    def _spark_payload(cls, *, market_hour: int) -> bytes:
        timestamps = cls._timestamps()
        closes = [1_000.0 + index for index in range(len(timestamps))]
        response = {
            "timestamp": timestamps,
            "meta": {
                "exchangeTimezoneName": "UTC",
                "regularMarketTime": timestamps[-1] + market_hour * 3_600,
                "regularMarketVolume": 10_000,
            },
            "indicators": {"quote": [{"close": closes}]},
        }
        return json.dumps({
            "spark": {
                "result": [
                    {"symbol": "1301.T", "response": [response]},
                ]
            }
        }).encode("utf-8")

    def test_history_excludes_complete_looking_intraday_row(self) -> None:
        with patch("free_market_connector._fetch", return_value=self._chart_payload(market_hour=14)):
            rows, _ = fetch_yahoo_history(
                "1301",
                date(2025, 1, 1),
                date(2026, 1, 1),
            )
        self.assertEqual(len(rows), 252)
        self.assertEqual(rows[-1]["Date"], "2025-09-09")

    def test_history_includes_current_row_after_market_close(self) -> None:
        with patch("free_market_connector._fetch", return_value=self._chart_payload(market_hour=16)):
            rows, _ = fetch_yahoo_history(
                "1301",
                date(2025, 1, 1),
                date(2026, 1, 1),
            )
        self.assertEqual(len(rows), 253)
        self.assertEqual(rows[-1]["Date"], "2025-09-10")

    def test_mirror_latest_uses_previous_close_during_market_hours(self) -> None:
        with patch("free_market_connector._fetch", return_value=self._spark_payload(market_hour=14)):
            result = fetch_yahoo_mirror_latest(["1301"])
        self.assertEqual(result["1301"]["date"], "2025-09-09")
        self.assertEqual(result["1301"]["close"], 1_251.0)

    def test_spark_history_excludes_intraday_row(self) -> None:
        with patch("free_market_connector._fetch", return_value=self._spark_payload(market_hour=14)):
            result = fetch_yahoo_spark_histories(["1301"])
        self.assertEqual(len(result["1301"]["rows"]), 252)
        self.assertEqual(result["1301"]["rows"][-1]["Date"], "2025-09-09")


if __name__ == "__main__":
    unittest.main()
