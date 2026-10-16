from __future__ import annotations

import sys
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from market_environment import (  # noqa: E402
    MarketPoint,
    MarketEnvironmentError,
    SettlementLinks,
    apply_meta_quote,
    fetch_yahoo_point,
    fetch_topix_futures,
    parse_topix_settlement,
    label_for_score,
    merge_history_snapshots,
    point_at_date,
    score_crude,
    weighted_score,
)


class MarketEnvironmentPolicyTest(unittest.TestCase):
    @staticmethod
    def settlement_csv(rows: str) -> bytes:
        return ("注記,,,,,\n銘柄コード,銘柄名称,PUT/CAL,限月,権利行使価格,清算価格\n" + rows).encode("cp932")

    def test_topix_settlement_selects_nearest_standard_future_not_mini_or_options(self) -> None:
        data = self.settlement_csv(
            "1,FUT_TPXM_261210,,202612,,1\n"
            "2,CAL_TPX_261210_4000,CAL,202612,4000,2\n"
            "3,FUT_TPX_270311,,202703,,4097\n"
            "4,FUT_TPX_261210,,202612,,4081.5\n"
        )
        self.assertEqual(parse_topix_settlement(data), ("202612", 4081.5))
        self.assertEqual(parse_topix_settlement(data, contract="202703"), ("202703", 4097))

    def test_topix_settlement_rejects_zero_missing_nan_and_wrong_contract(self) -> None:
        for value in ("0", "", "nan", "-1", "inf"):
            with self.subTest(value=value), self.assertRaises(MarketEnvironmentError):
                parse_topix_settlement(self.settlement_csv(f"1,FUT_TPX_261210,,202612,,{value}\n"))
        with self.assertRaises(MarketEnvironmentError):
            parse_topix_settlement(self.settlement_csv("1,FUT_TPX_261210,,202612,,4081\n"), contract="202703")

    def test_topix_source_limits_links_to_jpx_and_compares_same_contract(self) -> None:
        parser = SettlementLinks()
        parser.feed('<a href="https://example.com/rb20261016.csv">bad</a>')
        self.assertEqual(parser.links, [])
        page = b'<a href="/markets/derivatives/settlement-price/files/rb20261016.csv">CSV</a>'
        latest = self.settlement_csv("1,FUT_TPX_261210,,202612,,4081.5\n")
        saved = {"contract": "202612", "priceBasis": "ose-settlement", "settlementHistory": [
            {"date": "2026-10-15", "value": 4070}, {"date": "2026-10-16", "value": 1},
            {"date": "2026-10-17", "value": 9999}, {"date": "2026-10-14", "value": 0},
        ]}
        with patch("market_environment.fetch_bytes", side_effect=[page, latest]):
            point = fetch_topix_futures(saved)
        self.assertEqual(point.as_of, "2026-10-16")
        self.assertEqual(point.value, 4081.5)
        self.assertEqual(point.previous, 4070)
        self.assertAlmostEqual(point.change, 11.5)
        self.assertIn("清算値", point.note)
        self.assertIn("2026年12月限", point.note)
        self.assertEqual(point.to_dict()["settlementHistory"][-1]["value"], 4081.5)
        self.assertEqual(len(point.history), 2)
        for contract, basis in (("202609", "ose-settlement"), ("202612", "trade")):
            with patch("market_environment.fetch_bytes", side_effect=[page, latest]):
                saved.update(contract=contract, priceBasis=basis)
                self.assertIsNone(fetch_topix_futures(saved).previous)

    def test_topix_missing_previous_does_not_invent_change(self) -> None:
        page = b'<a href="/files/rb20261016.csv">CSV</a>'
        latest = self.settlement_csv("1,FUT_TPX_261210,,202612,,4081.5\n")
        with patch("market_environment.fetch_bytes", side_effect=[page, latest]):
            point = fetch_topix_futures()
        self.assertEqual(point.value, 4081.5)
        self.assertIsNone(point.previous)
        self.assertIsNone(point.change_rate)
        self.assertIn("比較日の清算値は未取得", point.note)

    def test_topix_failure_is_unavailable_not_zero(self) -> None:
        with patch("market_environment.fetch_bytes", side_effect=MarketEnvironmentError("HTTP 404")):
            point = fetch_topix_futures()
        self.assertEqual(point.status, "unavailable")
        self.assertIsNone(point.value)

    def test_label_thresholds(self) -> None:
        self.assertEqual(label_for_score(80), "追い風")
        self.assertEqual(label_for_score(65), "やや追い風")
        self.assertEqual(label_for_score(50), "中立")
        self.assertEqual(label_for_score(35), "慎重")
        self.assertEqual(label_for_score(20), "逆風")

    def test_weighted_score_ignores_missing_parts(self) -> None:
        self.assertEqual(weighted_score([(80, 0.7), (None, 0.3)]), 80)
        self.assertEqual(weighted_score([(None, 1.0)]), 50)

    def test_crude_spike_is_market_caution(self) -> None:
        calm = MarketPoint("wti", "WTI", 80.0, 79.0, "2026-07-09", "FRED", "")
        spike = MarketPoint("wti", "WTI", 85.0, 80.0, "2026-07-09", "FRED", "")

        self.assertGreater(score_crude(calm), score_crude(spike))

    def test_newer_yahoo_meta_quote_overrides_stale_history(self) -> None:
        value, previous, as_of, used_meta = apply_meta_quote(
            66819.05,
            68256.96,
            "2026-07-08",
            {
                "regularMarketPrice": 67743.85,
                "regularMarketPreviousClose": 65416.63,
                "regularMarketTime": 1783579503,
                "exchangeTimezoneName": "Asia/Tokyo",
            },
        )

        self.assertTrue(used_meta)
        self.assertEqual(as_of, "2026-07-09")
        self.assertEqual(value, 67743.85)
        self.assertEqual(previous, 65416.63)

    def test_point_at_date_uses_latest_available_and_previous_values(self) -> None:
        point = MarketPoint(
            "nikkei225",
            "日経225",
            102.0,
            101.0,
            "2026-07-03",
            "test",
            "",
            history=[
                {"date": "2026-07-01", "value": 100.0},
                {"date": "2026-07-02", "value": 101.0},
                {"date": "2026-07-03", "value": 102.0},
            ],
        )

        historical = point_at_date(point, "2026-07-02")

        self.assertEqual(historical.value, 101.0)
        self.assertEqual(historical.previous, 100.0)
        self.assertEqual(historical.as_of, "2026-07-02")

    def test_history_merge_replaces_same_date_and_retains_recent_window(self) -> None:
        existing = [
            {"date": "2026-01-01", "score": 40},
            {"date": "2026-07-01", "score": 50},
        ]
        incoming = [
            {"date": "2026-07-01", "score": 60},
            {"date": "2026-07-10", "score": 70},
        ]

        merged = merge_history_snapshots(existing, incoming, max_days=30)

        self.assertEqual([row["date"] for row in merged], ["2026-07-01", "2026-07-10"])
        self.assertEqual(merged[0]["score"], 60)

    def test_vix_history_keeps_ohlc_and_does_not_invent_meta_candle(self) -> None:
        stamp = lambda day: int(datetime(2026, 10, day, 20, tzinfo=timezone.utc).timestamp())
        payload = {"chart": {"result": [{
            "timestamp": [stamp(13), stamp(14)],
            "meta": {"exchangeTimezoneName": "America/New_York",
                     "regularMarketPrice": 25.0, "regularMarketPreviousClose": 20.0,
                     "regularMarketTime": stamp(15)},
            "indicators": {"quote": [{"open": [20.0, 21.0], "high": [22.0, 23.0],
                                      "low": [19.0, 20.0], "close": [21.0, 22.0]}]},
        }]}}
        with patch("market_environment.fetch_bytes", return_value=json.dumps(payload).encode()):
            point = fetch_yahoo_point("vix", "VIX", ["^VIX"])
        self.assertEqual(len(point.history), 2)
        self.assertEqual(point.history[-1]["high"], 23.0)
        self.assertEqual(point.history[-1]["close"], 22.0)
        self.assertEqual(point.as_of, "2026-10-15")


if __name__ == "__main__":
    unittest.main()
