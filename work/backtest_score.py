from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "outputs" / "data" / "latest-candidates.json"
DEFAULT_SCORE_HISTORY = ROOT / "outputs" / "data" / "score-history-v2.json"
DEFAULT_BACKTEST_OUTPUT = ROOT / "outputs" / "data" / "backtest-summary.json"


@dataclass
class Position:
    code: str
    name: str
    entry_date: date
    entry_price: float
    entry_score: int


@dataclass
class Trade:
    code: str
    name: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    entry_score: int
    exit_score: int | None
    reason: str

    @property
    def return_rate(self) -> float:
        return self.exit_price / self.entry_price - 1

    @property
    def holding_days(self) -> int:
        return (self.exit_date - self.entry_date).days


def parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def load_dataset(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_snapshots(paths: list[Path]) -> list[dict[str, Any]]:
    snapshots = [load_dataset(path) for path in paths]
    snapshots.sort(key=lambda item: str(item.get("generatedAt", "")))
    return snapshots


def load_score_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    history = load_dataset(path)
    snapshots = history.get("snapshots")
    if not isinstance(snapshots, list):
        return []
    result: list[dict[str, Any]] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        rows = snapshot.get("rows")
        if not isinstance(rows, list):
            continue
        result.append({
            "generatedAt": snapshot.get("generatedAt") or snapshot.get("date"),
            "searchUniverse": rows,
        })
    result.sort(key=lambda item: str(item.get("generatedAt", "")))
    return result


def merge_snapshots(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        generated_at = str(snapshot.get("generatedAt", ""))
        if not generated_at:
            continue
        by_date[generated_at[:10]] = snapshot
    return [by_date[key] for key in sorted(by_date)]


def row_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = snapshot.get("searchUniverse")
    if not isinstance(rows, list):
        rows = snapshot.get("candidates")
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("code")): row
        for row in rows
        if isinstance(row, dict) and row.get("code")
    }


def price_map(snapshot: dict[str, Any]) -> dict[str, dict[date, float]]:
    prices = snapshot.get("primeMarketPrices")
    if not isinstance(prices, list):
        return {}
    result: dict[str, dict[date, float]] = {}
    for item in prices:
        if not isinstance(item, dict) or not item.get("code"):
            continue
        history = item.get("chartHistory")
        if not isinstance(history, list):
            continue
        rows: dict[date, float] = {}
        for row in history:
            if not isinstance(row, dict):
                continue
            raw_date = row.get("date")
            close = row.get("close")
            if isinstance(raw_date, str) and isinstance(close, (int, float)) and close > 0:
                rows[parse_date(raw_date)] = float(close)
        if rows:
            result[str(item.get("code"))] = rows
    return result


def price_on_or_before(history: dict[date, float], target: date) -> tuple[date, float] | None:
    dates = [day for day in history if day <= target]
    if not dates:
        return None
    day = max(dates)
    return day, history[day]


def score(row: dict[str, Any]) -> int | None:
    value = row.get("score")
    return int(value) if isinstance(value, int) else None


def available_data_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    rows = row_map(snapshot)
    prices = price_map(snapshot)
    scores = [score(row) for row in rows.values()]
    scores = [value for value in scores if value is not None]
    return {
        "generatedAt": snapshot.get("generatedAt"),
        "rows": len(rows),
        "priceSeries": len(prices),
        "scoreCount": len(scores),
        "scoreMin": min(scores) if scores else None,
        "scoreMax": max(scores) if scores else None,
        "buyCandidates": sum(1 for value in scores if value >= 75),
        "sellCandidates": sum(1 for value in scores if value <= 65),
    }


def run_backtest(
    snapshots: list[dict[str, Any]],
    *,
    price_source: dict[str, Any],
    buy_score: int,
    sell_score: int,
    max_holding_days: int,
    stop_loss: float,
    take_profit: float,
) -> tuple[list[Trade], dict[str, Any]]:
    if len(snapshots) < 2:
        return [], {
            "status": "insufficient-score-history",
            "reason": "日次スコア履歴が2日分未満のため、スコア75到達日と65割れ売却日を検証できません。",
        }

    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    prices = price_map(price_source)

    for snapshot in snapshots:
        snapshot_date = parse_date(str(snapshot.get("generatedAt", "")))
        rows = row_map(snapshot)

        for code, position in list(positions.items()):
            row = rows.get(code, {})
            current_score = score(row)
            history = prices.get(code, {})
            price = price_on_or_before(history, snapshot_date)
            if price is None:
                continue
            exit_date, exit_price = price
            return_rate = exit_price / position.entry_price - 1
            reason = None
            if current_score is not None and current_score <= sell_score:
                reason = f"score<={sell_score}"
            elif (snapshot_date - position.entry_date).days >= max_holding_days:
                reason = f"max_hold_{max_holding_days}d"
            elif return_rate <= stop_loss:
                reason = f"stop_loss_{stop_loss:.0%}"
            elif return_rate >= take_profit:
                reason = f"take_profit_{take_profit:.0%}"
            if reason:
                trades.append(Trade(
                    code=code,
                    name=position.name,
                    entry_date=position.entry_date,
                    exit_date=exit_date,
                    entry_price=position.entry_price,
                    exit_price=exit_price,
                    entry_score=position.entry_score,
                    exit_score=current_score,
                    reason=reason,
                ))
                del positions[code]

        for code, row in rows.items():
            if code in positions:
                continue
            current_score = score(row)
            if current_score is None or current_score < buy_score:
                continue
            history = prices.get(code, {})
            price = price_on_or_before(history, snapshot_date)
            if price is None:
                continue
            entry_date, entry_price = price
            positions[code] = Position(
                code=code,
                name=str(row.get("name") or code),
                entry_date=entry_date,
                entry_price=entry_price,
                entry_score=current_score,
            )

    return trades, {"status": "ok", "openPositions": len(positions)}


def trade_summary(trades: list[Trade]) -> dict[str, Any]:
    if not trades:
        return {
            "trades": 0,
            "winRate": None,
            "averageReturn": None,
            "bestReturn": None,
            "worstReturn": None,
            "averageHoldingDays": None,
        }
    returns = [trade.return_rate for trade in trades]
    return {
        "trades": len(trades),
        "winRate": sum(1 for value in returns if value > 0) / len(returns),
        "averageReturn": mean(returns),
        "bestReturn": max(returns),
        "worstReturn": min(returns),
        "averageHoldingDays": mean(trade.holding_days for trade in trades),
    }


def format_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def print_report(snapshots: list[dict[str, Any]], trades: list[Trade], meta: dict[str, Any]) -> None:
    latest = snapshots[-1]
    availability = available_data_summary(latest)
    summary = trade_summary(trades)
    print("# Score Backtest Report")
    print()
    print("## Rule")
    print("- Buy: score >= 75")
    print("- Sell: score <= 65, max holding 20 days, stop loss -8%, take profit +15%")
    print()
    print("## Data Availability")
    for key, value in availability.items():
        print(f"- {key}: {value}")
    print()
    print("## Result")
    print(f"- status: {meta.get('status')}")
    if meta.get("reason"):
        print(f"- reason: {meta['reason']}")
    print(f"- trades: {summary['trades']}")
    print(f"- winRate: {format_percent(summary['winRate'])}")
    print(f"- averageReturn: {format_percent(summary['averageReturn'])}")
    print(f"- bestReturn: {format_percent(summary['bestReturn'])}")
    print(f"- worstReturn: {format_percent(summary['worstReturn'])}")
    print(f"- averageHoldingDays: {summary['averageHoldingDays']}")
    if trades:
        print()
        print("## Trades")
        for trade in trades[:20]:
            print(
                f"- {trade.code} {trade.name}: {trade.entry_date} -> {trade.exit_date}, "
                f"{format_percent(trade.return_rate)}, {trade.reason}"
            )


def backtest_verification_issues(payload: dict[str, Any]) -> list[str]:
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    strategy = payload.get("strategy") if isinstance(payload.get("strategy"), dict) else {}
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    trades = artifacts.get("trades") if isinstance(artifacts.get("trades"), dict) else {}
    checks = (
        (provenance.get("generator"), "生成プログラム"),
        (provenance.get("version"), "生成プログラム版"),
        (provenance.get("dataHash"), "入力データハッシュ"),
        (data.get("periodStart"), "検証開始日"),
        (data.get("periodEnd"), "検証終了日"),
        (strategy.get("parameters"), "売買パラメータ"),
        (trades.get("path"), "取引明細パス"),
        (trades.get("sha256"), "取引明細ハッシュ"),
        (trades.get("count") if isinstance(trades.get("count"), int) else None, "取引明細件数"),
    )
    return [label for value, label in checks if value in (None, "", {}, [])]


def backtest_summary_payload(
    snapshots: list[dict[str, Any]],
    trades: list[Trade],
    meta: dict[str, Any],
    *,
    buy_score: int,
    sell_score: int,
    max_holding_days: int,
    stop_loss: float,
    take_profit: float,
) -> dict[str, Any]:
    latest = snapshots[-1]
    payload = {
        "schemaVersion": 2,
        "generatedAt": latest.get("generatedAt"),
        "status": "unverified",
        "engineStatus": meta.get("status", "unknown"),
        "strategy": {
            "name": "現行スコア2.1 / 買い75 / 売り65",
            "universe": "日経225",
            "buyScore": buy_score,
            "sellScore": sell_score,
            "maxHoldingDays": max_holding_days,
            "stopLoss": stop_loss,
            "takeProfit": take_profit,
            "parameters": {
                "buyScore": buy_score,
                "sellScore": sell_score,
                "maxHoldingDays": max_holding_days,
                "stopLoss": stop_loss,
                "takeProfit": take_profit,
            },
            "supplyRule": "現行スコア2.1の需給はJPX信用倍率だけで評価し、52週高値はテクニカル・新高値判定で扱います。",
        },
        "data": available_data_summary(latest),
        "result": trade_summary(trades),
        "notes": [
            "スコア履歴と価格履歴から再計算した参考検証です。",
            "過去検証であり、将来の成績を保証するものではありません。",
        ],
    }
    issues = backtest_verification_issues(payload)
    payload["verification"] = {
        "verified": not issues,
        "missing": issues,
    }
    payload["status"] = "verified" if not issues else "unverified"
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Capital Gain Radar score rules.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--score-history", type=Path, default=DEFAULT_SCORE_HISTORY)
    parser.add_argument("--history", type=Path, nargs="*", default=[])
    parser.add_argument("--buy-score", type=int, default=75)
    parser.add_argument("--sell-score", type=int, default=65)
    parser.add_argument("--max-holding-days", type=int, default=20)
    parser.add_argument("--stop-loss", type=float, default=-0.08)
    parser.add_argument("--take-profit", type=float, default=0.15)
    parser.add_argument("--output-json", type=Path, default=None)
    args = parser.parse_args()

    latest_dataset = load_dataset(args.dataset) if args.dataset.exists() else {}
    file_snapshots = load_snapshots([path for path in args.history if path.exists()])
    score_history_snapshots = load_score_history(args.score_history)
    snapshots = merge_snapshots([*score_history_snapshots, *file_snapshots])
    if latest_dataset:
        snapshots = merge_snapshots([*snapshots, latest_dataset])
    if not snapshots:
        raise SystemExit(f"No dataset found. Checked {args.dataset}")

    trades, meta = run_backtest(
        snapshots,
        price_source=latest_dataset or snapshots[-1],
        buy_score=args.buy_score,
        sell_score=args.sell_score,
        max_holding_days=args.max_holding_days,
        stop_loss=args.stop_loss,
        take_profit=args.take_profit,
    )
    print_report(snapshots, trades, meta)
    if args.output_json:
        payload = backtest_summary_payload(
            snapshots,
            trades,
            meta,
            buy_score=args.buy_score,
            sell_score=args.sell_score,
            max_holding_days=args.max_holding_days,
            stop_loss=args.stop_loss,
            take_profit=args.take_profit,
        )
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {args.output_json}")


if __name__ == "__main__":
    main()
