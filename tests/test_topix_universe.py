from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from fetch_official_data import (  # noqa: E402
    build_price_history_shards,
    compact_dataset_for_output,
    parse_topix_components,
)
from tdnet_connector import industry_theme  # noqa: E402


class FakeSheet:
    def __init__(self, rows: list[list[object]]) -> None:
        self.rows = rows
        self.nrows = len(rows)

    def row_values(self, index: int) -> list[object]:
        return self.rows[index]


class FakeWorkbook:
    def __init__(self, rows: list[list[object]]) -> None:
        self.sheet = FakeSheet(rows)

    def sheet_by_index(self, index: int) -> FakeSheet:
        if index != 0:
            raise IndexError(index)
        return self.sheet


class TopixUniverseTest(unittest.TestCase):
    def test_only_topix_size_classes_are_included(self) -> None:
        rows = [
            ["日付", "コード", "銘柄名", "市場・商品区分", "33業種区分", "17業種区分", "規模区分"],
            [20260630.0, 1001.0, "Prime Topix", "プライム（内国株式）", "電気機器", "電機・精密", "TOPIX Core30"],
            [20260630.0, 1002.0, "Standard Topix", "スタンダード（内国株式）", "サービス業", "情報通信・サービスその他", "TOPIX Small 2"],
            [20260630.0, 1003.0, "Not Topix", "プライム（内国株式）", "卸売業", "商社・卸売", "-"],
        ]

        with patch(
            "fetch_official_data.xlrd.open_workbook",
            return_value=FakeWorkbook(rows),
        ):
            components, as_of = parse_topix_components(b"fixture", {"1001"})

        self.assertEqual(as_of, "2026-06-30")
        self.assertEqual([item["code"] for item in components], ["1001", "1002"])
        self.assertTrue(components[0]["isTopix"])
        self.assertTrue(components[0]["isNikkei225"])
        self.assertFalse(components[1]["isNikkei225"])
        self.assertEqual(components[1]["exchangeSection"], "スタンダード")
        self.assertEqual(components[1]["industry"], "サービス業")

    def test_output_compaction_removes_duplicate_collections(self) -> None:
        dataset = {
            "topixPrices": [{
                "code": "1001",
                "chartHistory": [{
                    "date": "2026-07-24",
                    "open": 100,
                    "high": 110,
                    "low": 95,
                    "close": 108,
                    "volume": 1000,
                }, {
                    "date": "2026-07-25", "open": 101, "high": 111, "low": 96, "close": 109, "volume": 1001,
                }, {
                    "date": "2026-07-26", "open": 102, "high": 112, "low": 97, "close": 110, "volume": 1002,
                }, {
                    "date": "2026-07-27", "open": 103, "high": 113, "low": 98, "close": 111, "volume": 1003,
                }, {
                    "date": "2026-07-28", "open": 104, "high": 114, "low": 99, "close": 112, "volume": 1004,
                }],
            }],
            "nikkei225Prices": [{"code": "1001"}],
            "searchUniverse": [{
                "code": "1001",
                "latestClose": 108,
                "weeklyTarget": {
                    "version": "weekly-fixture",
                    "metrics": {"ma25": 100},
                    "profiles": {
                        "single": {
                            "score": 80,
                            "components": [{"id": "unused"}],
                            "positives": ["a", "b", "c", "d"],
                            "blockers": [],
                        },
                    },
                },
            }],
            "candidates": [{"code": "1001"}],
        }

        bundle, payloads = build_price_history_shards(dataset, "2026-07-28T16:10:00+09:00")
        dataset["priceHistoryBundle"] = bundle
        compact_dataset_for_output(dataset)

        self.assertEqual(
            payloads["1"]["prices"][0]["chartHistory"][0],
            ["2026-07-24", 100, 110, 95, 108, 1000],
        )
        self.assertNotIn("topixPrices", dataset)
        self.assertEqual(dataset["candidates"], [])
        self.assertNotIn("nikkei225Prices", dataset)
        self.assertEqual(dataset["candidateCollection"], "searchUniverse")
        self.assertTrue(dataset["searchUniverse"][0]["aboveMa25"])
        self.assertNotIn(
            "components",
            dataset["searchUniverse"][0]["weeklyTarget"]["profiles"]["single"],
        )
        self.assertEqual(
            dataset["searchUniverse"][0]["weeklyTarget"]["profiles"]["single"]["positives"],
            ["a", "b", "c"],
        )

    def test_industry_theme_is_labelled_as_non_scoring_reference(self) -> None:
        self.assertEqual(industry_theme("情報・通信業"), "デジタル・通信（業種参考）")
        self.assertEqual(industry_theme("unknown"), "その他事業（業種参考）")

    def test_frontend_loads_only_selected_price_history_shard(self) -> None:
        source = (ROOT / "outputs" / "investment-candidate-app.html").read_text(encoding="utf-8")

        self.assertIn("function priceHistoryShardEntry(code)", source)
        self.assertIn("async function ensurePriceHistoryLoaded(code)", source)
        self.assertIn(r"data\/price-history\/topix-", source)
        self.assertIn("JPX業種から付けた参考分類。テーマ点への加点なし", source)


if __name__ == "__main__":
    unittest.main()
