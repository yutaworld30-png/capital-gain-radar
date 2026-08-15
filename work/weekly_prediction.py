from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta
from statistics import mean, median
from typing import Any

from weekly_target import (
    WEEKLY_STOP_LIMIT,
    WEEKLY_TARGET_RETURN,
    WEEKLY_TARGET_VERSION,
)


PREDICTION_SCHEMA_VERSION = 1
PREDICTION_VERSION = "weekly-prediction-v1.0"
LABEL_VERSION = "next-open-5sessions-target-stop-v1.0"
SUMMARY_VERSION = "weekly-accuracy-v1.0"
ENTRY_BASIS = "next-session-open"
HORIZON_SESSIONS = 5
DEFAULT_COST_BPS = 20
DEFAULT_RETENTION_DAYS = 400
CONTROL_LIMIT_PER_PROFILE = 25
EVALUATED_OUTCOMES = {"target-first", "stop-first", "expired", "ambiguous"}
PRIMARY_OUTCOMES = {"target-first", "stop-first", "expired"}

STRICT_CHECK_LABELS = {
    "overallScore": "総合スコア75点以上",
    "dataQuality": "データ信頼度85点以上",
    "trend": "上昇トレンド",
    "nearHigh": "52週高値まで3%以内",
    "relativeStrength": "相対強度80以上",
    "rsi": "RSI 55〜70",
    "atr": "ATR率2.5〜5.5%",
    "volume": "出来高倍率1.5倍以上",
    "liquidity": "平均売買代金10億円以上",
    "fundamentals": "売上・利益・ROE条件",
    "supply": "信用倍率3倍以下",
    "stopWidth": "損切り幅2.5%以下",
    "earningsSafe": "決算3営業日前を回避",
}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_data_hash(record: dict[str, object]) -> str:
    return _stable_hash({key: value for key, value in record.items() if key != "dataHash"})


def _chart_rows(price: dict[str, object] | None) -> list[dict[str, object]]:
    raw_rows = price.get("chartHistory") if isinstance(price, dict) else None
    rows: list[dict[str, object]] = []
    if not isinstance(raw_rows, list):
        return rows
    for raw in raw_rows:
        if isinstance(raw, dict):
            row = raw
        elif isinstance(raw, list) and len(raw) >= 6:
            row = {
                "date": raw[0],
                "open": raw[1],
                "high": raw[2],
                "low": raw[3],
                "close": raw[4],
                "volume": raw[5],
            }
        else:
            continue
        if _as_date(row.get("date")) is not None:
            rows.append(row)
    rows.sort(key=lambda item: str(item.get("date") or ""))
    return rows


def _price_map(dataset: dict[str, object]) -> dict[str, dict[str, object]]:
    prices = dataset.get("topixPrices")
    if not isinstance(prices, list):
        prices = dataset.get("nikkei225Prices")
    return {
        str(item.get("code")): item
        for item in prices
        if isinstance(item, dict) and item.get("code")
    } if isinstance(prices, list) else {}


def _market_snapshot(payload: dict[str, object] | None) -> dict[str, object]:
    environment = payload.get("marketEnvironment") if isinstance(payload, dict) else None
    indicators = payload.get("indicators") if isinstance(payload, dict) else None
    environment = environment if isinstance(environment, dict) else {}
    indicators = indicators if isinstance(indicators, dict) else {}

    def indicator_value(key: str, field: str = "value") -> float | None:
        item = indicators.get(key)
        return _number(item.get(field)) if isinstance(item, dict) else None

    return {
        "generatedAt": payload.get("generatedAt") if isinstance(payload, dict) else None,
        "score": _number(environment.get("score")),
        "label": str(environment.get("label") or "未取得"),
        "nikkei225ChangeRate": indicator_value("nikkei225", "changeRate"),
        "nikkeiFuturesChangeRate": indicator_value("nikkei225Futures", "changeRate"),
        "sp500ChangeRate": indicator_value("sp500", "changeRate"),
        "nasdaqChangeRate": indicator_value("nasdaq", "changeRate"),
        "vix": indicator_value("vix"),
        "usdJpy": indicator_value("usdJpy"),
    }


def _event_snapshot(row: dict[str, object]) -> list[dict[str, object]]:
    events = row.get("events")
    if not isinstance(events, list):
        return []
    compact: list[dict[str, object]] = []
    for event in events[:5]:
        if not isinstance(event, dict):
            continue
        compact.append({
            key: event.get(key)
            for key in ("type", "label", "date", "daysFromNow", "source")
            if event.get(key) is not None
        })
    return compact


def _feature_snapshot(
    row: dict[str, object],
    weekly: dict[str, object],
    profile: dict[str, object],
    market: dict[str, object],
) -> dict[str, object]:
    metrics = weekly.get("metrics") if isinstance(weekly.get("metrics"), dict) else {}
    components = profile.get("components") if isinstance(profile.get("components"), list) else []
    strict_checks = profile.get("strictChecks") if isinstance(profile.get("strictChecks"), dict) else {}
    return {
        "priceAsOf": row.get("priceAsOf"),
        "latestClose": _number(row.get("latestClose")),
        "overallScore": _number(row.get("score")),
        "dataQuality": _number(row.get("dataQuality")),
        "weeklyScore": _number(profile.get("score")),
        "industry": str(row.get("industry") or "業種未分類"),
        "marketCap": _number(row.get("marketCap")),
        "salesGrowth": _number(row.get("salesGrowth")),
        "profitGrowth": _number(row.get("profitGrowth")),
        "roe": _number(row.get("roe")),
        "metrics": {
            key: metrics.get(key)
            for key in (
                "ma5",
                "ma25",
                "ma75",
                "ma25Rising",
                "trendStrong",
                "trendPartial",
                "rsi14",
                "volumeRatio20",
                "atrPercent",
                "high52wDistance",
                "averageTurnover20",
                "relativeStrength",
                "marginRatio",
                "suggestedStopWidth",
            )
        },
        "strictChecks": {
            str(key): bool(value)
            for key, value in strict_checks.items()
        },
        "components": [
            {
                key: component.get(key)
                for key in ("id", "label", "points", "earned", "passed")
            }
            for component in components
            if isinstance(component, dict)
        ],
        "events": _event_snapshot(row),
        "marketEnvironment": market,
    }


def _prediction_id(
    signal_date: str,
    code: str,
    profile: str,
    score_version: str,
    factor_version: str,
) -> str:
    raw = ":".join((signal_date, code, profile, WEEKLY_TARGET_VERSION, score_version, factor_version))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _new_prediction(
    row: dict[str, object],
    price: dict[str, object],
    profile_key: str,
    profile_rank: int,
    generated_at: str,
    dataset: dict[str, object],
    market: dict[str, object],
) -> dict[str, object] | None:
    weekly = row.get("weeklyTarget")
    if not isinstance(weekly, dict):
        return None
    profiles = weekly.get("profiles")
    profile = profiles.get(profile_key) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict) or profile.get("budgetFit") is not True:
        return None
    history = _chart_rows(price)
    if not history:
        return None
    signal_date = str(history[-1].get("date") or "")[:10]
    if _as_date(signal_date) is None:
        return None
    code = str(row.get("code") or "")
    if not code:
        return None
    score_version = str(dataset.get("scoreVersion") or "")
    factor_version = str(dataset.get("factorVersion") or "")
    status = str(profile.get("status") or "not-eligible")
    snapshot = _feature_snapshot(row, weekly, profile, market)
    record: dict[str, object] = {
        "predictionId": _prediction_id(signal_date, code, profile_key, score_version, factor_version),
        "predictionVersion": PREDICTION_VERSION,
        "labelVersion": LABEL_VERSION,
        "weeklyTargetVersion": str(weekly.get("version") or WEEKLY_TARGET_VERSION),
        "scoreVersion": score_version,
        "factorVersion": factor_version,
        "priceBasis": str(dataset.get("priceBasis") or ""),
        "signalDate": signal_date,
        "predictedAt": generated_at,
        "code": code,
        "name": str(row.get("name") or code),
        "industry": str(row.get("industry") or "業種未分類"),
        "profile": profile_key,
        "profileLabel": str(profile.get("label") or profile_key),
        "rankAtSignal": profile_rank,
        "statusAtSignal": status,
        "sampleKind": "candidate" if status in {"qualified", "watch"} else "control",
        "weeklyScore": _number(profile.get("score")),
        "overallScore": _number(row.get("score")),
        "featureSnapshot": snapshot,
        "execution": {
            "entryBasis": ENTRY_BASIS,
            "horizonSessions": HORIZON_SESSIONS,
            "targetReturn": WEEKLY_TARGET_RETURN,
            "stopLimit": WEEKLY_STOP_LIMIT,
            "costBps": int(os.environ.get("WEEKLY_PREDICTION_COST_BPS", str(DEFAULT_COST_BPS))),
        },
        "state": "pending",
        "outcome": None,
    }
    record["signalHash"] = _stable_hash({
        "predictionId": record["predictionId"],
        "featureSnapshot": snapshot,
        "execution": record["execution"],
    })
    record["dataHash"] = record_data_hash(record)
    return record


def build_daily_predictions(
    dataset: dict[str, object],
    generated_at: str,
    market_environment: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    rows = dataset.get("searchUniverse")
    if not isinstance(rows, list):
        rows = dataset.get("candidates")
    typed_rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    prices = _price_map(dataset)
    market = _market_snapshot(market_environment)
    predictions: list[dict[str, object]] = []
    for profile_key in ("single", "double"):
        ranked: list[tuple[dict[str, object], dict[str, object]]] = []
        for row in typed_rows:
            weekly = row.get("weeklyTarget")
            profiles = weekly.get("profiles") if isinstance(weekly, dict) else None
            profile = profiles.get(profile_key) if isinstance(profiles, dict) else None
            if isinstance(profile, dict) and profile.get("budgetFit") is True:
                ranked.append((row, profile))
        ranked.sort(key=lambda pair: (-float(_number(pair[1].get("score")) or -1), str(pair[0].get("code") or "")))
        selected = [pair for pair in ranked if pair[1].get("status") in {"qualified", "watch"}]
        controls = [pair for pair in ranked if pair[1].get("status") == "not-eligible"][:CONTROL_LIMIT_PER_PROFILE]
        rank_by_code = {
            str(row.get("code") or ""): index + 1
            for index, (row, _) in enumerate(ranked)
        }
        for row, _ in selected + controls:
            code = str(row.get("code") or "")
            price = prices.get(code)
            if not isinstance(price, dict):
                continue
            prediction = _new_prediction(
                row,
                price,
                profile_key,
                rank_by_code.get(code, 0),
                generated_at,
                dataset,
                market,
            )
            if prediction is not None:
                predictions.append(prediction)
    return predictions


def _finalize(
    record: dict[str, object],
    outcome_label: str,
    entry_date: str,
    entry_price: float,
    observed: list[dict[str, object]],
    outcome_date: str,
    exit_price: float | None,
    days_to_outcome: int | None,
) -> dict[str, object]:
    highs = [_number(row.get("high")) for row in observed]
    lows = [_number(row.get("low")) for row in observed]
    valid_highs = [value for value in highs if value is not None]
    valid_lows = [value for value in lows if value is not None]
    gross_return = (exit_price / entry_price - 1) if exit_price is not None else None
    execution = record.get("execution") if isinstance(record.get("execution"), dict) else {}
    cost_bps = int(_number(execution.get("costBps")) or DEFAULT_COST_BPS)
    net_return = gross_return - cost_bps / 10_000 if gross_return is not None else None
    record["state"] = "evaluated"
    record["outcome"] = {
        "label": outcome_label,
        "primaryEligible": outcome_label in PRIMARY_OUTCOMES,
        "conservativeLabel": "stop-first" if outcome_label == "ambiguous" else outcome_label,
        "entryDate": entry_date,
        "entryPrice": _round(entry_price, 4),
        "targetPrice": _round(entry_price * (1 + WEEKLY_TARGET_RETURN), 4),
        "stopPrice": _round(entry_price * (1 - WEEKLY_STOP_LIMIT), 4),
        "outcomeDate": outcome_date,
        "exitPrice": _round(exit_price, 4),
        "daysToOutcome": days_to_outcome,
        "observedSessions": len(observed),
        "grossReturn": _round(gross_return),
        "netReturn": _round(net_return),
        "mfe": _round(max(valid_highs) / entry_price - 1) if valid_highs else None,
        "mae": _round(min(valid_lows) / entry_price - 1) if valid_lows else None,
    }
    record["evaluatedAt"] = outcome_date
    record["dataHash"] = record_data_hash(record)
    return record


def evaluate_prediction(
    record: dict[str, object],
    history: list[dict[str, object]] | list[list[object]],
    *,
    as_of: date | None = None,
) -> dict[str, object]:
    if record.get("state") == "evaluated":
        return record
    normalized = _chart_rows({"chartHistory": history})
    signal_date = _as_date(record.get("signalDate"))
    if signal_date is None:
        record["state"] = "unavailable"
        record["unavailableReason"] = "予測日が不正です。"
        record["dataHash"] = record_data_hash(record)
        return record
    future = [row for row in normalized if (_as_date(row.get("date")) or signal_date) > signal_date]
    if not future:
        if as_of is not None and as_of - signal_date > timedelta(days=14):
            record["state"] = "unavailable"
            record["unavailableReason"] = "予測後の価格履歴を確認できません。"
        else:
            record["state"] = "pending"
        record["dataHash"] = record_data_hash(record)
        return record
    entry_price = _number(future[0].get("open"))
    entry_date = str(future[0].get("date") or "")[:10]
    if entry_price is None or entry_price <= 0:
        record["state"] = "unavailable"
        record["unavailableReason"] = "翌営業日の始値を確認できません。"
        record["dataHash"] = record_data_hash(record)
        return record
    target_price = entry_price * (1 + WEEKLY_TARGET_RETURN)
    stop_price = entry_price * (1 - WEEKLY_STOP_LIMIT)
    observed: list[dict[str, object]] = []
    for index, row in enumerate(future[:HORIZON_SESSIONS], start=1):
        observed.append(row)
        row_date = str(row.get("date") or "")[:10]
        open_price = _number(row.get("open"))
        high = _number(row.get("high"))
        low = _number(row.get("low"))
        if open_price is not None and open_price >= target_price:
            return _finalize(record, "target-first", entry_date, entry_price, observed, row_date, open_price, index)
        if open_price is not None and open_price <= stop_price:
            return _finalize(record, "stop-first", entry_date, entry_price, observed, row_date, open_price, index)
        target_touched = high is not None and high >= target_price
        stop_touched = low is not None and low <= stop_price
        if target_touched and stop_touched:
            return _finalize(record, "ambiguous", entry_date, entry_price, observed, row_date, None, index)
        if target_touched:
            return _finalize(record, "target-first", entry_date, entry_price, observed, row_date, target_price, index)
        if stop_touched:
            return _finalize(record, "stop-first", entry_date, entry_price, observed, row_date, stop_price, index)
    if len(future) >= HORIZON_SESSIONS:
        exit_row = observed[-1]
        exit_price = _number(exit_row.get("close"))
        if exit_price is None or exit_price <= 0:
            record["state"] = "unavailable"
            record["unavailableReason"] = "5営業日目の終値を確認できません。"
            record["dataHash"] = record_data_hash(record)
            return record
        return _finalize(
            record,
            "expired",
            entry_date,
            entry_price,
            observed,
            str(exit_row.get("date") or "")[:10],
            exit_price,
            HORIZON_SESSIONS,
        )
    record["state"] = "pending"
    record["dataHash"] = record_data_hash(record)
    return record


def update_prediction_ledger(
    dataset: dict[str, object],
    generated_at: str,
    existing: dict[str, object] | None = None,
    market_environment: dict[str, object] | None = None,
) -> dict[str, object]:
    active_versions = {
        "weeklyTargetVersion": WEEKLY_TARGET_VERSION,
        "scoreVersion": str(dataset.get("scoreVersion") or ""),
        "factorVersion": str(dataset.get("factorVersion") or ""),
        "priceBasis": str(dataset.get("priceBasis") or ""),
    }
    old_records = existing.get("records") if isinstance(existing, dict) else None
    records = [
        dict(record)
        for record in old_records
        if isinstance(record, dict)
        and all(record.get(key) == value for key, value in active_versions.items())
    ] if isinstance(old_records, list) else []
    by_id = {
        str(record.get("predictionId")): record
        for record in records
        if record.get("predictionId")
    }
    for prediction in build_daily_predictions(dataset, generated_at, market_environment):
        prediction_id = str(prediction.get("predictionId") or "")
        if prediction_id and prediction_id not in by_id:
            by_id[prediction_id] = prediction
    prices = _price_map(dataset)
    generated_date = _as_date(generated_at)
    updated_records: list[dict[str, object]] = []
    for record in by_id.values():
        code = str(record.get("code") or "")
        price = prices.get(code)
        history = price.get("chartHistory") if isinstance(price, dict) else []
        if isinstance(history, list):
            record = evaluate_prediction(record, history, as_of=generated_date)
        updated_records.append(record)
    retention_days = max(30, int(os.environ.get("WEEKLY_PREDICTION_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))))
    cutoff = (generated_date - timedelta(days=retention_days)) if generated_date else None
    if cutoff is not None:
        updated_records = [
            record for record in updated_records
            if (_as_date(record.get("signalDate")) or cutoff) >= cutoff
        ]
    updated_records.sort(key=lambda record: (str(record.get("signalDate") or ""), str(record.get("predictionId") or "")))
    counts = {
        state: sum(1 for record in updated_records if record.get("state") == state)
        for state in ("pending", "evaluated", "unavailable")
    }
    return {
        "schemaVersion": PREDICTION_SCHEMA_VERSION,
        "predictionVersion": PREDICTION_VERSION,
        "labelVersion": LABEL_VERSION,
        "generatedAt": generated_at,
        **active_versions,
        "execution": {
            "entryBasis": ENTRY_BASIS,
            "horizonSessions": HORIZON_SESSIONS,
            "targetReturn": WEEKLY_TARGET_RETURN,
            "stopLimit": WEEKLY_STOP_LIMIT,
            "costBps": int(os.environ.get("WEEKLY_PREDICTION_COST_BPS", str(DEFAULT_COST_BPS))),
            "ambiguousPrimaryPolicy": "excluded",
            "ambiguousConservativePolicy": "stop-first",
        },
        "sampling": {
            "candidateStatuses": ["qualified", "watch"],
            "controlStatus": "not-eligible",
            "controlLimitPerProfile": CONTROL_LIMIT_PER_PROFILE,
        },
        "retentionDays": retention_days,
        "recordCount": len(updated_records),
        "stateCounts": counts,
        "records": updated_records,
    }


def _metric_summary(records: list[dict[str, object]]) -> dict[str, object]:
    states = {state: sum(1 for record in records if record.get("state") == state) for state in ("pending", "evaluated", "unavailable")}
    evaluated = [record for record in records if record.get("state") == "evaluated"]
    outcome_counts = {
        label: sum(
            1 for record in evaluated
            if isinstance(record.get("outcome"), dict) and record["outcome"].get("label") == label
        )
        for label in ("target-first", "stop-first", "expired", "ambiguous")
    }
    primary_count = sum(outcome_counts[label] for label in PRIMARY_OUTCOMES)
    conservative_count = primary_count + outcome_counts["ambiguous"]
    net_returns = [
        float(record["outcome"]["netReturn"])
        for record in evaluated
        if isinstance(record.get("outcome"), dict)
        and record["outcome"].get("label") in PRIMARY_OUTCOMES
        and _number(record["outcome"].get("netReturn")) is not None
    ]
    mfes = [
        float(record["outcome"]["mfe"])
        for record in evaluated
        if isinstance(record.get("outcome"), dict) and _number(record["outcome"].get("mfe")) is not None
    ]
    maes = [
        float(record["outcome"]["mae"])
        for record in evaluated
        if isinstance(record.get("outcome"), dict) and _number(record["outcome"].get("mae")) is not None
    ]
    evaluated_count = len(evaluated)
    sample_status = "available" if primary_count >= 100 else "limited" if primary_count >= 30 else "collecting"
    return {
        "recordCount": len(records),
        "pendingCount": states["pending"],
        "evaluatedCount": evaluated_count,
        "unavailableCount": states["unavailable"],
        "primaryEvaluatedCount": primary_count,
        "targetFirstCount": outcome_counts["target-first"],
        "stopFirstCount": outcome_counts["stop-first"],
        "expiredCount": outcome_counts["expired"],
        "ambiguousCount": outcome_counts["ambiguous"],
        "targetFirstRate": _round(outcome_counts["target-first"] / primary_count, 4) if primary_count else None,
        "stopFirstRate": _round(outcome_counts["stop-first"] / primary_count, 4) if primary_count else None,
        "conservativeTargetRate": _round(outcome_counts["target-first"] / conservative_count, 4) if conservative_count else None,
        "conservativeStopRate": _round((outcome_counts["stop-first"] + outcome_counts["ambiguous"]) / conservative_count, 4) if conservative_count else None,
        "averageNetReturn": _round(mean(net_returns)) if net_returns else None,
        "medianNetReturn": _round(median(net_returns)) if net_returns else None,
        "positiveReturnRate": _round(sum(1 for value in net_returns if value > 0) / len(net_returns), 4) if net_returns else None,
        "averageMfe": _round(mean(mfes)) if mfes else None,
        "averageMae": _round(mean(maes)) if maes else None,
        "sampleStatus": sample_status,
    }


def _condition_group(
    records: list[dict[str, object]],
    key_fn: Any,
    label_fn: Any | None = None,
) -> list[dict[str, object]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for record in records:
        key = str(key_fn(record) or "未取得")
        groups.setdefault(key, []).append(record)
    items = [
        {
            "key": key,
            "label": str(label_fn(key) if label_fn else key),
            "metrics": _metric_summary(group_records),
        }
        for key, group_records in groups.items()
    ]
    items.sort(key=lambda item: (-int(item["metrics"]["primaryEvaluatedCount"]), str(item["label"])))
    return items


def _score_band(record: dict[str, object]) -> str:
    score = _number(record.get("weeklyScore"))
    if score is None:
        return "missing"
    if score >= 80:
        return "80+"
    if score >= 70:
        return "70-79"
    if score >= 60:
        return "60-69"
    if score >= 50:
        return "50-59"
    return "0-49"


def _snapshot_value(record: dict[str, object], *keys: str) -> object:
    value: object = record.get("featureSnapshot")
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _profile_summary(records: list[dict[str, object]]) -> dict[str, object]:
    metrics = _metric_summary(records)
    candidates = [record for record in records if record.get("sampleKind") == "candidate"]
    controls = [record for record in records if record.get("sampleKind") == "control"]
    candidate_metrics = _metric_summary(candidates)
    control_metrics = _metric_summary(controls)
    candidate_rate = _number(candidate_metrics.get("targetFirstRate"))
    control_rate = _number(control_metrics.get("targetFirstRate"))
    conditions = {
        "status": _condition_group(
            records,
            lambda record: record.get("statusAtSignal"),
            lambda key: {"qualified": "厳格条件一致", "watch": "監視候補", "not-eligible": "対照銘柄"}.get(key, key),
        ),
        "scoreBands": _condition_group(records, _score_band),
        "marketRegimes": _condition_group(
            records,
            lambda record: _snapshot_value(record, "marketEnvironment", "label"),
        ),
        "industries": _condition_group(records, lambda record: record.get("industry")),
        "strictChecks": [
            {
                "key": key,
                "label": label,
                "passed": _metric_summary([
                    record for record in records
                    if _snapshot_value(record, "strictChecks", key) is True
                ]),
                "failed": _metric_summary([
                    record for record in records
                    if _snapshot_value(record, "strictChecks", key) is False
                ]),
            }
            for key, label in STRICT_CHECK_LABELS.items()
        ],
    }
    return {
        **metrics,
        "candidateMetrics": candidate_metrics,
        "controlMetrics": control_metrics,
        "targetRateLift": _round(candidate_rate - control_rate, 4) if candidate_rate is not None and control_rate is not None else None,
        "conditions": conditions,
    }


def build_accuracy_summary(ledger: dict[str, object], generated_at: str) -> dict[str, object]:
    raw_records = ledger.get("records")
    records = [record for record in raw_records if isinstance(record, dict)] if isinstance(raw_records, list) else []
    profiles = {
        profile: _profile_summary([record for record in records if record.get("profile") == profile])
        for profile in ("single", "double")
    }
    evaluated = [
        record for record in records
        if record.get("state") == "evaluated" and isinstance(record.get("outcome"), dict)
    ]
    evaluated.sort(
        key=lambda record: (
            str(record["outcome"].get("outcomeDate") or ""),
            str(record.get("signalDate") or ""),
            str(record.get("predictionId") or ""),
        ),
        reverse=True,
    )
    recent_records = [
        {
            "predictionId": record.get("predictionId"),
            "signalDate": record.get("signalDate"),
            "code": record.get("code"),
            "name": record.get("name"),
            "industry": record.get("industry"),
            "profile": record.get("profile"),
            "statusAtSignal": record.get("statusAtSignal"),
            "weeklyScore": record.get("weeklyScore"),
            "rankAtSignal": record.get("rankAtSignal"),
            "marketLabel": _snapshot_value(record, "marketEnvironment", "label"),
            "outcome": record.get("outcome"),
        }
        for record in evaluated[:100]
    ]
    signal_dates = [str(record.get("signalDate")) for record in records if record.get("signalDate")]
    return {
        "schemaVersion": PREDICTION_SCHEMA_VERSION,
        "summaryVersion": SUMMARY_VERSION,
        "predictionVersion": ledger.get("predictionVersion"),
        "labelVersion": ledger.get("labelVersion"),
        "generatedAt": generated_at,
        "weeklyTargetVersion": ledger.get("weeklyTargetVersion"),
        "scoreVersion": ledger.get("scoreVersion"),
        "factorVersion": ledger.get("factorVersion"),
        "priceBasis": ledger.get("priceBasis"),
        "execution": ledger.get("execution"),
        "dataPeriod": {
            "from": min(signal_dates) if signal_dates else None,
            "to": max(signal_dates) if signal_dates else None,
        },
        "overall": _profile_summary(records),
        "profiles": profiles,
        "recentRecords": recent_records,
        "notes": [
            "的中率は目標+5%へ損切り-2.5%より先に到達した割合です。",
            "同一日中に目標と損切りの両方へ触れた場合は順序不明として主的中率から除外します。",
            "手数料とスリッページの参考値として往復20bpsをリターンから控除しています。",
            "十分な評価件数が蓄積するまでは学習中として扱い、候補順位には反映しません。",
        ],
    }


def validate_prediction_ledger(payload: object, dataset: dict[str, object]) -> list[str]:
    if not isinstance(payload, dict):
        return ["週5%予測台帳のルートがJSONオブジェクトではありません。"]
    errors: list[str] = []
    if payload.get("schemaVersion") != PREDICTION_SCHEMA_VERSION:
        errors.append("週5%予測台帳のschemaVersionが不正です。")
    for key, expected in (
        ("predictionVersion", PREDICTION_VERSION),
        ("labelVersion", LABEL_VERSION),
        ("weeklyTargetVersion", WEEKLY_TARGET_VERSION),
        ("scoreVersion", dataset.get("scoreVersion")),
        ("factorVersion", dataset.get("factorVersion")),
        ("priceBasis", dataset.get("priceBasis")),
    ):
        if payload.get(key) != expected:
            errors.append(f"週5%予測台帳の{key}が生成データと一致しません。")
    execution = payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
    if (
        execution.get("entryBasis") != ENTRY_BASIS
        or execution.get("horizonSessions") != HORIZON_SESSIONS
        or execution.get("ambiguousPrimaryPolicy") != "excluded"
    ):
        errors.append("週5%予測台帳の売買・曖昧判定条件が不正です。")
    records = payload.get("records")
    if not isinstance(records, list):
        return errors + ["週5%予測台帳のrecordsが配列ではありません。"]
    identifiers: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            errors.append(f"週5%予測台帳{index + 1}行目がオブジェクトではありません。")
            continue
        identifier = str(record.get("predictionId") or "")
        if not identifier or identifier in identifiers:
            errors.append(f"週5%予測IDが欠損または重複しています: {identifier or index + 1}")
        identifiers.add(identifier)
        if any(record.get(key) != payload.get(key) for key in ("predictionVersion", "labelVersion", "weeklyTargetVersion", "scoreVersion", "factorVersion", "priceBasis")):
            errors.append(f"{identifier}: 予測レコードの生成版が台帳と一致しません。")
        snapshot = record.get("featureSnapshot") if isinstance(record.get("featureSnapshot"), dict) else {}
        price_as_of = _as_date(snapshot.get("priceAsOf"))
        signal_date = _as_date(record.get("signalDate"))
        if signal_date is None or (price_as_of is not None and price_as_of > signal_date):
            errors.append(f"{identifier}: 予測時点より未来の特徴量日付が含まれています。")
        if record.get("dataHash") != record_data_hash(record):
            errors.append(f"{identifier}: データハッシュが一致しません。")
        state = record.get("state")
        outcome = record.get("outcome")
        if state == "evaluated" and (not isinstance(outcome, dict) or outcome.get("label") not in EVALUATED_OUTCOMES):
            errors.append(f"{identifier}: 評価済み結果が不完全です。")
        if state not in {"pending", "evaluated", "unavailable"}:
            errors.append(f"{identifier}: 状態が不正です。")
    if payload.get("recordCount") != len(records):
        errors.append("週5%予測台帳のrecordCountが実件数と一致しません。")
    return errors


def validate_accuracy_summary(payload: object, ledger: dict[str, object]) -> list[str]:
    if not isinstance(payload, dict):
        return ["週5%予測実績のルートがJSONオブジェクトではありません。"]
    errors: list[str] = []
    if payload.get("schemaVersion") != PREDICTION_SCHEMA_VERSION or payload.get("summaryVersion") != SUMMARY_VERSION:
        errors.append("週5%予測実績のスキーマまたは集計版が不正です。")
    for key in ("predictionVersion", "labelVersion", "weeklyTargetVersion", "scoreVersion", "factorVersion", "priceBasis"):
        if payload.get(key) != ledger.get(key):
            errors.append(f"週5%予測実績の{key}が台帳と一致しません。")
    profiles = payload.get("profiles") if isinstance(payload.get("profiles"), dict) else {}
    if any(not isinstance(profiles.get(profile), dict) for profile in ("single", "double")):
        errors.append("週5%予測実績に資金配分別集計がありません。")
    for profile, summary in profiles.items():
        if not isinstance(summary, dict):
            continue
        rate = _number(summary.get("targetFirstRate"))
        if rate is not None and not 0 <= rate <= 1:
            errors.append(f"週5%予測実績 {profile} の的中率が範囲外です。")
        conditions = summary.get("conditions") if isinstance(summary.get("conditions"), dict) else {}
        if any(not isinstance(conditions.get(key), list) for key in ("status", "scoreBands", "marketRegimes", "industries", "strictChecks")):
            errors.append(f"週5%予測実績 {profile} の条件別集計が不完全です。")
    return errors
