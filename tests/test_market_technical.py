from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from market_technical import (  # noqa: E402
    bollinger_bands,
    build_technical_rows,
    ichimoku_series,
    macd_series,
    parabolic_sar,
    rsi_wilder,
    simple_moving_average,
)


def sample_rows(count: int = 120) -> list[dict[str, float | str]]:
    start = date(2026, 1, 1)
    return [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "open": 100.0 + index,
            "high": 102.0 + index,
            "low": 98.0 + index,
            "close": 101.0 + index,
        }
        for index in range(count)
    ]


class MarketTechnicalTests(unittest.TestCase):
    def test_simple_moving_average_starts_at_period_boundary(self) -> None:
        self.assertEqual(
            simple_moving_average([1.0, 2.0, 3.0], 2),
            [None, 1.5, 2.5],
        )

    def test_bollinger_bands_for_constant_prices(self) -> None:
        middle, upper, lower = bollinger_bands([100.0] * 20)
        self.assertIsNone(middle[18])
        self.assertEqual(middle[19], 100.0)
        self.assertEqual(upper[19], 100.0)
        self.assertEqual(lower[19], 100.0)

    def test_rsi_wilder_all_gains_is_100(self) -> None:
        values = [float(index) for index in range(20)]
        result = rsi_wilder(values)
        self.assertIsNone(result[13])
        self.assertEqual(result[14], 100.0)

    def test_macd_warmup_boundaries(self) -> None:
        macd, signal, histogram = macd_series([float(index) for index in range(60)])
        self.assertIsNone(macd[24])
        self.assertIsNotNone(macd[25])
        self.assertIsNone(signal[32])
        self.assertIsNotNone(signal[33])
        self.assertIsNotNone(histogram[33])

    def test_parabolic_sar_returns_finite_values(self) -> None:
        result = parabolic_sar(sample_rows(20))
        self.assertEqual(len(result), 20)
        self.assertTrue(all(isinstance(value, float) for value in result))

    def test_ichimoku_displacement_boundaries(self) -> None:
        result = ichimoku_series(sample_rows(100))
        self.assertIsNone(result["spanA"][50])
        self.assertIsNotNone(result["spanA"][51])
        self.assertIsNone(result["spanB"][76])
        self.assertIsNotNone(result["spanB"][77])
        self.assertEqual(result["chikou"][0], 127.0)

    def test_per_bands_use_same_day_weighted_per(self) -> None:
        rows = sample_rows(120)
        target = rows[-1]
        output = build_technical_rows(
            rows,
            weighted_per_rows=[{"date": target["date"], "weightedPer": 20.0}],
        )
        latest = output[-1]
        self.assertEqual(latest["weightedPer"], 20.0)
        self.assertEqual(latest["impliedEps"], round(float(target["close"]) / 20.0, 2))
        self.assertEqual(
            latest["perBands"]["20"],
            round(float(target["close"]), 2),
        )

    def test_invalid_ohlc_rows_are_removed(self) -> None:
        rows = sample_rows(100)
        rows.append({
            "date": "2026-07-01",
            "open": 0.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
        })
        output = build_technical_rows(rows)
        self.assertEqual(len(output), 100)


if __name__ == "__main__":
    unittest.main()
