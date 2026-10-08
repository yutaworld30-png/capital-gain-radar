"""Import available official dated PDFs; never infer missing trading days."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from fetch_official_data import (
    fetch_margin_file_links, inspect_latest_margin_pdf, margin_pdf_date,
)
from margin_history import update, validate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "outputs/data")
    parser.add_argument("--dataset", type=Path, default=Path(__file__).resolve().parents[1] / "outputs/data/latest-candidates.json")
    args = parser.parse_args()
    universe = json.loads(args.dataset.read_text(encoding="utf-8"))
    codes = {str(row["code"]) for row in universe.get("topixComponents", [])}
    if not codes:
        parser.error("TOPIX components are required")
    history_path = args.data_dir / "margin-history-v1.json"
    history = validate(json.loads(history_path.read_text(encoding="utf-8"))) if history_path.exists() else {"sources": {}}
    links = fetch_margin_file_links()
    count = 0
    for link in sorted(links, key=lambda item: item["url"]):
        as_of = margin_pdf_date(link["url"])
        if not as_of:
            continue
        if history.get("sources", {}).get(as_of, {}).get("url") == link["url"]:
            continue
        inspected = inspect_latest_margin_pdf([link])
        records = [row for row in inspected.pop("records", []) if str(row["code"]) in codes]
        if len(records) / len(codes) < 0.95 or inspected.get("asOf") != as_of:
            raise RuntimeError(f"Margin PDF coverage/date check failed: {as_of}")
        dataset = {"sources": {"marginWeekly": {
            "status": "partial" if inspected.get("parseFailureCount") else "available", "asOf": inspected["asOf"],
            "updatedAt": inspected.get("publishedAt"), "pdfInspection": inspected,
        }}, "topixMargin": records}
        history = update(args.data_dir, dataset, datetime.now(timezone.utc).isoformat())
        print(f"Imported {as_of}: {len(records)} stocks, published {inspected.get('publishedAt')}", flush=True)
        count += 1
    print(f"Imported {count} official dated PDFs. Unavailable dates remain missing.")


if __name__ == "__main__":
    main()
