from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "work"))
from market_environment import MarketPoint  # noqa: E402
from us_market import US_KEYS, build_us_market, parse_fear_greed  # noqa: E402


GENERATED = "2026-10-22T21:00:00+00:00"
MARKET_DATE = "2026-10-22"
NEW_YORK_OCTOBER = timezone(timedelta(hours=-4))


def points(day: str = MARKET_DATE) -> dict[str, MarketPoint]:
    rows = []
    start = date.fromisoformat(day) - timedelta(days=39)
    for index in range(40):
        stamp = (start + timedelta(days=index)).isoformat()
        rows.append({"date": stamp, "value": 20 + index / 10,
                     "open": 20 + index / 10, "high": 21 + index / 10,
                     "low": 19 + index / 10, "close": 20 + index / 10})
    return {key: MarketPoint(key, key, 20, 19, day, "test", "https://example.test",
                              history=rows) for key in US_KEYS}


def sectors(day: str = MARKET_DATE) -> dict:
    return {"status": "available", "asOf": day, "coveredCount": 11, "rows": []}


class UsMarketTests(unittest.TestCase):
    def test_fear_greed_requires_same_session_and_real_score(self) -> None:
        stamp = datetime(2026, 10, 22, 20, 50, tzinfo=timezone.utc).isoformat()
        raw = {"fear_and_greed": {"score": 39, "rating": "fear", "timestamp": stamp}}
        with patch("us_market.ZoneInfo", return_value=NEW_YORK_OCTOBER):
            reading = parse_fear_greed(raw, generated_at=GENERATED, market_date=MARKET_DATE)
            stale = parse_fear_greed(raw, generated_at=GENERATED, market_date="2026-10-21")
            raw["fear_and_greed"]["score"] = 101
            invalid = parse_fear_greed(raw, generated_at=GENERATED, market_date=MARKET_DATE)
            raw["fear_and_greed"]["score"] = 0
            zero = parse_fear_greed(raw, generated_at=GENERATED, market_date=MARKET_DATE)
        self.assertEqual((reading["score"], reading["label"]), (39, "恐怖"))
        self.assertEqual(stale["status"], "unavailable")
        self.assertIsNone(invalid["score"])
        self.assertEqual(zero["score"], 0)
        self.assertEqual(reading["providerId"], "cnn")

    def test_cnn_missing_or_old_reading_is_unavailable(self) -> None:
        with patch("us_market.ZoneInfo", return_value=NEW_YORK_OCTOBER):
            missing = parse_fear_greed({"fear_and_greed": {"score": None}},
                                       generated_at=GENERATED, market_date=MARKET_DATE)
            old = parse_fear_greed(
                {"fear_and_greed": {"score": 45, "rating": "neutral",
                                    "timestamp": "2026-10-22T08:00:00+00:00"}},
                generated_at=GENERATED, market_date=MARKET_DATE,
            )
        self.assertEqual(missing["status"], "unavailable")
        self.assertEqual(old["status"], "unavailable")
        self.assertIsNone(old["score"])

    def test_cnn_history_filters_invalid_and_future_rows(self) -> None:
        day = lambda value: int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)
        raw = {
            "fear_and_greed": {"score": 19.8, "rating": "extreme fear",
                               "timestamp": "2026-10-22T20:50:00+00:00"},
            "fear_and_greed_historical": {"data": [
                {"x": day("2026-10-21"), "y": 31.3},
                {"x": day("2026-10-22"), "y": 19.8},
                {"x": day("2026-10-23"), "y": 40},
                {"x": day("2026-10-20"), "y": None},
            ]},
        }
        with patch("us_market.ZoneInfo", return_value=NEW_YORK_OCTOBER):
            reading = parse_fear_greed(raw, generated_at=GENERATED, market_date=MARKET_DATE)
        self.assertEqual((reading["score"], reading["label"]), (20, "強い恐怖"))
        self.assertEqual(reading["history"], [
            {"date": "2026-10-21", "score": 31},
            {"date": "2026-10-22", "score": 20},
        ])

    def test_mixed_session_or_backward_date_cannot_publish(self) -> None:
        data = points()
        data["nasdaq"].as_of = "2026-10-21"
        with self.assertRaisesRegex(ValueError, "確定日"):
            build_us_market(points=data, sectors=sectors(), fear_greed={}, previous={},
                            generated_at=GENERATED)
        with self.assertRaisesRegex(ValueError, "逆戻り"):
            build_us_market(points=points(), sectors=sectors(), fear_greed={},
                            previous={"asOf": "2026-10-23"}, generated_at=GENERATED)
        with self.assertRaisesRegex(ValueError, "業種ランキング"):
            build_us_market(points=points(), sectors=sectors("2026-10-21"),
                            fear_greed={}, previous={}, generated_at=GENERATED)

    def test_previous_cnn_history_is_preserved(self) -> None:
        with patch("us_market.ZoneInfo", return_value=NEW_YORK_OCTOBER):
            payload = build_us_market(
                points=points(), sectors=sectors(),
                fear_greed={"status": "available", "score": 39, "asOf": MARKET_DATE,
                            "providerId": "cnn"},
                previous={"asOf": "2026-10-21", "fearGreed": {"providerId": "cnn"},
                          "fearGreedHistory": [
                    {"date": "2026-10-21", "score": 48},
                ]}, generated_at=GENERATED,
            )
        self.assertEqual(payload["fearGreedHistory"], [
            {"date": "2026-10-21", "score": 48},
            {"date": "2026-10-22", "score": 39},
        ])
        self.assertEqual(payload["vixTechnical"]["asOf"], MARKET_DATE)
        self.assertEqual(payload["schemaVersion"], 1)

    def test_previous_other_provider_history_is_not_mixed_with_cnn(self) -> None:
        with patch("us_market.ZoneInfo", return_value=NEW_YORK_OCTOBER):
            payload = build_us_market(
                points=points(), sectors=sectors(),
                fear_greed={"status": "available", "score": 39, "asOf": MARKET_DATE,
                            "providerId": "cnn", "history": [
                                {"date": "2026-10-21", "score": 31}]},
                previous={"asOf": "2026-10-21", "fearGreed": {"source": "FearGreedChart"},
                          "fearGreedHistory": [{"date": "2026-10-21", "score": 88}]},
                generated_at=GENERATED,
            )
        self.assertEqual(payload["fearGreedHistory"], [
            {"date": "2026-10-21", "score": 31},
            {"date": "2026-10-22", "score": 39},
        ])
        self.assertNotIn("history", payload["fearGreed"])

    def test_unavailable_cnn_reason_is_preserved(self) -> None:
        with patch("us_market.ZoneInfo", return_value=NEW_YORK_OCTOBER):
            payload = build_us_market(
                points=points(), sectors=sectors(),
                fear_greed={"status": "unavailable", "score": None, "providerId": "cnn",
                            "note": "CNN側で自動取得が制限されています。"},
                previous={}, generated_at=GENERATED,
            )
        self.assertEqual(payload["fearGreed"]["status"], "unavailable")
        self.assertEqual(payload["fearGreed"]["note"], "CNN側で自動取得が制限されています。")


if __name__ == "__main__":
    unittest.main()
