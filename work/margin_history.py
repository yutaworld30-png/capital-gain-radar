"""Weekly per-issue margin observations, independent of score history."""
import json
import math
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def validate(history):
    if history.get("schemaVersion") != 1 or not isinstance(history.get("stocks"), dict):
        raise ValueError("Invalid margin history schema")
    for code, rows in history["stocks"].items():
        if not re.fullmatch(r"[0-9A-Z]{4}", code) or not isinstance(rows, list):
            raise ValueError("Invalid margin history stock")
        dates = []
        for row in rows:
            dates.append(date.fromisoformat(row["date"]))
            for field in ("buy", "sell", "ratio"):
                if row.get(field) is not None and number(row[field]) is None:
                    raise ValueError("Invalid margin observation")
            expected = round(row["buy"] / row["sell"], 4) if number(row.get("buy")) is not None and number(row.get("sell")) not in (None, 0) else None
            if row.get("ratio") != expected:
                raise ValueError("Inconsistent margin ratio")
        if dates != sorted(set(dates)):
            raise ValueError("Duplicate or unsorted margin dates")
    return history


def merge(history, dataset, generated_at):
    validate(history)
    stocks = {code: {row["date"]: row for row in rows} for code, rows in history["stocks"].items()}
    sources = dict(history.get("sources", {}))
    source = dataset.get("sources", {}).get("marginWeekly", {})
    # Failed fetches must never create a new observation from stale balances.
    if source.get("status") in ("available", "partial") and source.get("asOf"):
        as_of = date.fromisoformat(source["asOf"]).isoformat()
        sources[as_of] = {"url": source.get("pdfInspection", {}).get("url"), "observedAt": generated_at}
        for item in dataset.get("topixMargin", []):
            code = str(item.get("code", ""))
            if not re.fullmatch(r"[0-9A-Z]{4}", code):
                continue
            buy, sell = number(item.get("outstandingPurchases")), number(item.get("outstandingSales"))
            if buy is None and sell is None:
                continue
            stocks.setdefault(code, {})[as_of] = {
                "date": as_of, "buy": buy, "sell": sell,
                "ratio": round(buy / sell, 4) if buy is not None and sell not in (None, 0) else None,
                "publishedAt": source.get("updatedAt"),
            }
    cutoff = (date.fromisoformat(generated_at[:10]) - timedelta(days=400)).isoformat()
    result = {"schemaVersion": 1, "unit": "shares", "generatedAt": generated_at,
              "sources": {key: value for key, value in sources.items() if key >= cutoff},
              "stocks": {code: [rows[key] for key in sorted(rows) if key >= cutoff] for code, rows in stocks.items()}}
    return validate(result)


def update(data_dir, dataset, generated_at, remote_url=None, loader=None):
    path = Path(data_dir) / "margin-history-v1.json"
    history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schemaVersion": 1, "stocks": {}}
    validate(history)
    if remote_url:
        try:
            remote = validate(loader(remote_url))
            remote["sources"] = {**history.get("sources", {}), **remote.get("sources", {})}
            for code, rows in history["stocks"].items():
                combined = {row["date"]: row for row in rows}
                combined.update({row["date"]: row for row in remote["stocks"].get(code, [])})
                remote["stocks"][code] = [combined[key] for key in sorted(combined)]
            history = remote
        except HTTPError as error:
            # The first release has no remote history; all other errors stop publication.
            if error.code != 404:
                raise RuntimeError("Margin history restore failed") from None
            published = loader(remote_url.rsplit("/", 1)[0] + "/latest-candidates.json")
            if published.get("marginHistorySummary"):
                raise RuntimeError("Previously published margin history is missing")
        except Exception:
            raise RuntimeError("Margin history restore failed") from None
    result = merge(history, dataset, generated_at)
    shard_dir = Path(data_dir) / "margin-history"
    shard_dir.mkdir(parents=True, exist_ok=True)
    for code, rows in result["stocks"].items():
        _write(shard_dir / f"{code}.json", {"schemaVersion": 1, "unit": "shares", "code": code, "rows": rows})
    _write(path, result)
    return result


def _write(path, payload):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def validate_files(data_dir):
    directory = Path(data_dir)
    history = validate(json.loads((directory / "margin-history-v1.json").read_text(encoding="utf-8")))
    if (directory / "margin-history-v1.json").stat().st_size > 25 * 1024 * 1024:
        raise ValueError("Margin history exceeds hosting file limit")
    for code, rows in history["stocks"].items():
        shard = json.loads((directory / "margin-history" / f"{code}.json").read_text(encoding="utf-8"))
        if shard.get("code") != code or shard.get("rows") != rows or shard.get("unit") != "shares" or shard.get("schemaVersion") != 1:
            raise ValueError("Margin history shard does not match ledger")
    return history


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Import verified per-issue weekly margin JSON")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    if not dataset.get("topixMargin"):
        parser.error("Source dataset has no raw per-issue margin balances")
    update(args.data_dir, dataset, dataset["generatedAt"])
