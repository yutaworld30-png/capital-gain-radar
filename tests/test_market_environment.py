from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from market_environment import (  # noqa: E402
    MarketPoint,
    apply_meta_quote,
    label_for_score,
    score_crude,
    weighted_score,
)


class MarketEnvironmentPolicyTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
