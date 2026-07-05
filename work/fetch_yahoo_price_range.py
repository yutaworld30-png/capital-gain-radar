from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
WORK_DIR = ROOT / "work"
if str(WORK_DIR) not in sys.path:
    sys.path.insert(0, str(WORK_DIR))

from free_market_connector import FreeMarketDataError, fetch_yahoo_history  # noqa: E402


DEFAULT_DATA_DIR = ROOT / "data" / "input"


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def load_codes(data_dir: Path) -> list[str]:
    membership_path = data_dir / "nikkei225_membership.csv"
    if not membership_path.exists():
        raise SystemExit(f"Missing {membership_path}")
    membership = pd.read_csv(membership_path, dtype={"code": "string"}, encoding="utf-8-sig")
    codes = sorted(set(membership["code"].dropna().astype("string").str.zfill(4)))
    if not codes:
        raise SystemExit("nikkei225_membership.csv has no codes.")
    return codes


def normalize_rows(code: str, rows: list[dict[str, object]], start: date, end: date) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for row in rows:
        row_date = parse_date(str(row["Date"]))
        if not start <= row_date <= end:
            continue
        normalized.append(
            {
                "date": row_date.isoformat(),
                "code": code,
                "open": float(row["O"]),
                "high": float(row["H"]),
                "low": float(row["L"]),
                "close": float(row["C"]),
                "volume": float(row.get("V") or 0),
                "turnover_value": float(row.get("Va") or 0),
            }
        )
    return normalized


def merge_prices(data_dir: Path, fetched: pd.DataFrame) -> Path:
    output = data_dir / "prices.csv"
    if not output.exists():
        raise SystemExit(f"Missing {output}")
    existing = pd.read_csv(output, dtype={"code": "string"}, encoding="utf-8-sig")
    backup = data_dir / "prices.before_yahoo_range_merge.csv"
    if not backup.exists():
        shutil.copyfile(output, backup)
        print(f"Wrote backup {backup}")
    merged = pd.concat([existing, fetched], ignore_index=True)
    merged["code"] = merged["code"].astype("string").str.zfill(4)
    merged = merged.drop_duplicates(["date", "code"], keep="last").sort_values(["date", "code"])
    merged.to_csv(output, index=False, encoding="utf-8-sig")
    print(f"Wrote {output} ({len(merged)} rows, fetched {len(fetched)} rows before de-duplication)")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch and merge Yahoo Finance price rows for a date range.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    parser.add_argument("--max-codes", type=int, default=0)
    parser.add_argument("--request-delay-seconds", type=float, default=0.2)
    args = parser.parse_args()

    start = parse_date(args.from_date)
    end = parse_date(args.to_date)
    codes = load_codes(args.data_dir)
    if args.max_codes > 0:
        codes = codes[: args.max_codes]

    all_rows: list[dict[str, object]] = []
    errors: list[str] = []
    for index, code in enumerate(codes, start=1):
        try:
            rows, _url = fetch_yahoo_history(code, start, end)
            normalized = normalize_rows(code, rows, start, end)
            all_rows.extend(normalized)
            print(f"[{index}/{len(codes)}] {code}: {len(normalized)} rows")
        except FreeMarketDataError as error:
            errors.append(f"{code}: {error}")
            print(f"[{index}/{len(codes)}] {code}: error")
        if index < len(codes) and args.request_delay_seconds > 0:
            time.sleep(args.request_delay_seconds)

    if errors:
        error_path = args.data_dir / "prices_yahoo_range_fetch_errors.txt"
        error_path.write_text("\n".join(errors), encoding="utf-8")
        print(f"Wrote errors to {error_path}")
    if not all_rows:
        raise SystemExit("No Yahoo price rows were fetched.")

    fetched = pd.DataFrame(all_rows)
    fetched["code"] = fetched["code"].astype("string").str.zfill(4)
    fetched = fetched.drop_duplicates(["date", "code"]).sort_values(["date", "code"])
    merge_prices(args.data_dir, fetched)


if __name__ == "__main__":
    main()
