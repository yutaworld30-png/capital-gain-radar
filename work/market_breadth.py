from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


BREADTH_VERSION = "nikkei225-breadth-v1"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def build_nikkei225_breadth(
    price_records: object,
    *,
    expected_count: int = 225,
    ratio_period: int = 25,
) -> dict[str, Any]:
    if ratio_period <= 0:
        raise ValueError("ratio_period must be positive")

    daily: dict[str, dict[str, int]] = defaultdict(
        lambda: {"advances": 0, "declines": 0, "unchanged": 0}
    )
    usable_codes = 0
    if isinstance(price_records, list):
        for record in price_records:
            if not isinstance(record, dict):
                continue
            history = record.get("chartHistory")
            if not isinstance(history, list):
                continue
            by_date: dict[str, float] = {}
            for row in history:
                if not isinstance(row, dict):
                    continue
                row_date = str(row.get("date") or "")
                close = _number(row.get("close"))
                if row_date and close is not None and close > 0:
                    by_date[row_date] = close
            ordered = sorted(by_date.items())
            if len(ordered) < 2:
                continue
            usable_codes += 1
            for index in range(1, len(ordered)):
                row_date, close = ordered[index]
                previous_close = ordered[index - 1][1]
                if close > previous_close:
                    daily[row_date]["advances"] += 1
                elif close < previous_close:
                    daily[row_date]["declines"] += 1
                else:
                    daily[row_date]["unchanged"] += 1

    rows: list[dict[str, Any]] = []
    ordered_dates = sorted(daily)
    for index, row_date in enumerate(ordered_dates):
        counts = daily[row_date]
        coverage_count = sum(counts.values())
        window_dates = ordered_dates[max(0, index - ratio_period + 1):index + 1]
        advance_sum = sum(daily[item]["advances"] for item in window_dates)
        decline_sum = sum(daily[item]["declines"] for item in window_dates)
        ratio = (
            round(advance_sum / decline_sum * 100, 2)
            if len(window_dates) == ratio_period and decline_sum > 0
            else None
        )
        rows.append({
            "date": row_date,
            **counts,
            "coverageCount": coverage_count,
            "coverageRate": round(coverage_count / max(1, expected_count), 4),
            "advanceDeclineRatio25": ratio,
            "rollingAdvanceCount": advance_sum if len(window_dates) == ratio_period else None,
            "rollingDeclineCount": decline_sum if len(window_dates) == ratio_period else None,
        })

    latest = rows[-1] if rows else {}
    latest_coverage = _number(latest.get("coverageRate"))
    status = (
        "available"
        if len(rows) >= ratio_period and latest_coverage is not None and latest_coverage >= 0.80
        else "insufficient-data"
    )
    return {
        "status": status,
        "version": BREADTH_VERSION,
        "scope": "日経225",
        "membershipBasis": "current-components",
        "expectedCount": expected_count,
        "usableComponentCount": usable_codes,
        "ratioPeriod": ratio_period,
        "formula": "直近25取引日の値上がり銘柄数合計÷値下がり銘柄数合計×100",
        "unchangedTreatment": "変わらずは分子・分母から除外",
        "asOf": latest.get("date"),
        "warning": "現在の日経225構成銘柄で再計算した参考値です。過去時点の構成銘柄を再現した指数ではありません。",
        "rows": rows,
    }
