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
        stamp = int(datetime(2026, 10, 22, 20, 50, tzinfo=timezone.utc).timestamp() * 1000)
        raw = {"score": {"score": 39}, "ts": stamp}
        with patch("us_market.ZoneInfo", return_value=NEW_YORK_OCTOBER):
            reading = parse_fear_greed(raw, generated_at=GENERATED, market_date=MARKET_DATE)
            stale = parse_fear_greed(raw, generated_at=GENERATED, market_date="2026-10-21")
            raw["score"]["score"] = 101
            invalid = parse_fear_greed(raw, generated_at=GENERATED, market_date=MARKET_DATE)
            raw["score"]["score"] = 0
            zero = parse_fear_greed(raw, generated_at=GENERATED, market_date=MARKET_DATE)
        self.assertEqual((reading["score"], reading["label"]), (39, "恐怖"))
        self.assertEqual(stale["status"], "unavailable")
        self.assertIsNone(invalid["score"])
        self.assertEqual(zero["score"], 0)

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

    def test_previous_sentiment_history_is_preserved(self) -> None:
        with patch("us_market.ZoneInfo", return_value=NEW_YORK_OCTOBER):
            payload = build_us_market(
                points=points(), sectors=sectors(),
                fear_greed={"status": "available", "score": 39, "asOf": MARKET_DATE},
                previous={"asOf": "2026-10-21", "fearGreedHistory": [
                    {"date": "2026-10-21", "score": 48},
                ]}, generated_at=GENERATED,
            )
        self.assertEqual(payload["fearGreedHistory"], [
            {"date": "2026-10-21", "score": 48},
            {"date": "2026-10-22", "score": 39},
        ])
        self.assertEqual(payload["vixTechnical"]["asOf"], MARKET_DATE)
        self.assertEqual(payload["schemaVersion"], 1)


if __name__ == "__main__":
    unittest.main()
