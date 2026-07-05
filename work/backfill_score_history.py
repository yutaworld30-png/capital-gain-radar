from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import pandas as pd

from backtest225.data.loaders import active_membership, load_input_bundle
from backtest225.scoring.factors import calculate_scores


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "outputs" / "data" / "score-history.json"
DEFAULT_HISTORY_URL = "https://yutaworld30-png.github.io/capital-gain-radar/data/score-history.json"


def load_existing_history(path: Path, url: str) -> dict[str, object]:
    if path.exists():
      try:
          return json.loads(path.read_text(encoding="utf-8"))
      except (OSError, json.JSONDecodeError):
          pass
    try:
        with urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return {"schemaVersion": 1, "snapshots": []}


def compact_row(row: pd.Series) -> dict[str, object]:
    margin_ratio = row.get("margin_ratio")
    months_from_high = row.get("months_from_high")
    high_52w = row.get("high_52w")
    close = row.get("close")
    return {
        "code": str(row["code"]).zfill(4),
        "name": str(row.get("name", "") or ""),
        "industry": str(row.get("industry", "日経225") or "日経225"),
        "isNikkei225": True,
        "score": int(round(float(row["total_score"]))),
        "supply": int(round(float(row["supply_demand"]))),
        "theme": int(round(float(row["theme"]))),
        "technical": int(round(float(row["technical"]))),
        "relative": int(round(float(row["relative_strength"]))),
        "earnings": int(round(float(row["earnings"]))),
        "liquidity": int(round(float(row["liquidity"]))),
        "risk": int(round(100 - float(row["low_risk"]))),
        "margin": round(float(margin_ratio), 4) if pd.notna(margin_ratio) else None,
        "monthsFromHigh": round(float(months_from_high), 2) if pd.notna(months_from_high) else None,
        "isNewHigh52w": bool(pd.notna(high_52w) and pd.notna(close) and float(close) >= float(high_52w)),
        "latestClose": round(float(close), 2) if pd.notna(close) else None,
        "priceAsOf": str(pd.to_datetime(row["date"]).date()),
        "scoreVersion": "backtest225-supplyB-reconstructed-v1",
        "historySource": "reconstructed-backtest225",
    }


def build_backfilled_snapshots(
    *,
    data_dir: Path,
    start_date: str,
    end_date: str,
    include_news: bool,
) -> list[dict[str, object]]:
    bundle = load_input_bundle(data_dir, include_news=include_news)
    prices = bundle["prices"]
    start = pd.to_datetime(start_date)
    end = pd.to_datetime(end_date)
    bundle["prices"] = prices.copy()
    bundle["membership_active"] = active_membership(bundle["membership"], bundle["prices"]["date"])

    scores = calculate_scores(bundle, supply_mode="B", include_news=include_news)
    scores = scores.dropna(subset=["close", "ma50", "ma200", "return_120d"]).copy()
    scores = scores[(scores["date"] >= start) & (scores["date"] <= end)].copy()
    if scores.empty:
        return []

    latest_names = (
        pd.read_csv(data_dir / "themes.csv", dtype={"code": "string"})
        .assign(code=lambda df: df["code"].astype("string").str.zfill(4))
        .drop_duplicates("code")
    )
    if "name" in latest_names.columns:
        scores = scores.merge(latest_names[["code", "name"]], on="code", how="left")

    snapshots: list[dict[str, object]] = []
    for snapshot_date, group in scores.groupby(scores["date"].dt.date):
        rows = [compact_row(row) for _, row in group.iterrows()]
        rows = [row for row in rows if row.get("code")]
        snapshots.append({
            "date": str(snapshot_date),
            "generatedAt": f"{snapshot_date}T00:00:00+09:00",
            "rowCount": len(rows),
            "scoreMax": max((int(row["score"]) for row in rows), default=None),
            "scoreMin": min((int(row["score"]) for row in rows), default=None),
            "buy75Count": sum(1 for row in rows if int(row["score"]) >= 75),
            "sell65Count": sum(1 for row in rows if int(row["score"]) <= 65),
            "source": "reconstructed-backtest225",
            "rows": rows,
        })
    return snapshots


def merge_snapshots(existing: dict[str, object], backfilled: list[dict[str, object]]) -> dict[str, object]:
    merged: dict[str, dict[str, object]] = {}
    for snapshot in existing.get("snapshots", []):
        if isinstance(snapshot, dict) and snapshot.get("date"):
            merged[str(snapshot["date"])] = snapshot
    for snapshot in backfilled:
        date = str(snapshot.get("date", ""))
        if date and date not in merged:
            merged[date] = snapshot
    snapshots = [merged[key] for key in sorted(merged)]
    return {
        "schemaVersion": 1,
        "generatedAt": existing.get("generatedAt"),
        "retentionDays": max(int(existing.get("retentionDays", 400) or 400), len(snapshots)),
        "historyNote": "Rows with source=reconstructed-backtest225 are recreated from backtest inputs and are reference data, not original daily production snapshots.",
        "snapshots": snapshots,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill score-history.json from backtest225 inputs.")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "input")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--history-url", default=DEFAULT_HISTORY_URL)
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--end-date", default="2026-04-07")
    parser.add_argument("--include-news", action="store_true")
    args = parser.parse_args()

    existing = load_existing_history(args.output, args.history_url)
    backfilled = build_backfilled_snapshots(
        data_dir=args.data_dir,
        start_date=args.start_date,
        end_date=args.end_date,
        include_news=args.include_news,
    )
    merged = merge_snapshots(existing, backfilled)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(merged, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {args.output}")
    print(f"Snapshots: {len(merged.get('snapshots', []))}")
    print(f"Backfilled: {len(backfilled)}")


if __name__ == "__main__":
    main()
