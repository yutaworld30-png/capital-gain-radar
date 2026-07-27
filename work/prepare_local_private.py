from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from market_analysis import (
    LOCAL_CANDIDATE_OUTPUT,
    PUBLISHED_CANDIDATE_URL,
    MarketAnalysisError,
    fetch_bytes,
)
from validate_output import validate_dataset
from weekly_target import attach_weekly_targets


class LocalPrivatePreparationError(RuntimeError):
    pass


def write_local_candidates(payload: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(output)


def validate_local_candidates(payload: object) -> list[str]:
    errors = validate_dataset(payload)
    if not isinstance(payload, dict):
        return errors
    universe = payload.get("universe") if isinstance(payload.get("universe"), dict) else {}
    expected_count = universe.get("expectedCount")
    components = payload.get("topixComponents")
    candidates = payload.get("searchUniverse")
    if not isinstance(expected_count, int) or not isinstance(components, list) or len(components) != expected_count:
        errors.append("TOPIX構成銘柄数がユニバース契約と一致しません。")
    if not isinstance(candidates, list) or not candidates:
        errors.append("TOPIXの候補銘柄がありません。")
    return list(dict.fromkeys(errors))


def refresh_local_candidates(
    *,
    output: Path = LOCAL_CANDIDATE_OUTPUT,
    url: str = PUBLISHED_CANDIDATE_URL,
) -> dict[str, Any]:
    try:
        payload = json.loads(fetch_bytes(url).decode("utf-8"))
    except (MarketAnalysisError, json.JSONDecodeError) as error:
        raise LocalPrivatePreparationError(
            "公開済みの検証済み候補JSONを取得できませんでした。"
        ) from error
    attach_weekly_targets(payload)
    errors = validate_local_candidates(payload)
    if errors:
        raise LocalPrivatePreparationError(" / ".join(errors))
    write_local_candidates(payload, output)
    return payload


def load_existing_local_candidates(
    path: Path = LOCAL_CANDIDATE_OUTPUT,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LocalPrivatePreparationError(
            "The previous verified local candidate JSON is unavailable."
        ) from error
    attach_weekly_targets(payload)
    errors = validate_local_candidates(payload)
    if errors:
        raise LocalPrivatePreparationError(" / ".join(errors))
    return payload


def main() -> int:
    try:
        payload = refresh_local_candidates()
    except LocalPrivatePreparationError as error:
        try:
            payload = load_existing_local_candidates()
        except LocalPrivatePreparationError:
            print(f"ERROR: {error}")
            return 1
        print(
            "WARNING: The downloaded candidate JSON did not pass validation. "
            "Using the previous verified TOPIX local cache."
        )
        write_local_candidates(payload, LOCAL_CANDIDATE_OUTPUT)
        print(
            "OK: Previous local candidate JSON retained "
            f"({len(payload.get('searchUniverse', []))} stocks, {payload.get('generatedAt')})"
        )
        return 0
    print(
        "OK: ローカル候補JSONを更新しました "
        f"({len(payload.get('searchUniverse', []))}銘柄, {payload.get('generatedAt')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
