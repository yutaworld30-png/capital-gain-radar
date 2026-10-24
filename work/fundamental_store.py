"""Persistent source facts and bounded, fair financial-data refreshes."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from xml.etree import ElementTree

from edinet_connector import (
    EdinetError, calculate_valuation_metrics, download_xbrl_zip,
    fetch_recent_securities_reports, parse_financial_metrics_from_xbrl,
)
from free_market_connector import FreeMarketDataError, fetch_yahoo_dividend_forecast

STATE_PATH = Path(__file__).resolve().parents[1] / ".private-data" / "fundamentals-state-v1.json"
FACT_VERSION = 3
FIELDS = (
    "per", "pbr", "roe", "roa", "marketCap", "dps", "dpsKind", "dpsAsOf", "dpsSource",
    "actualDps", "actualDpsAsOf", "forecastDps", "dividendYield", "dividendYieldAsOf",
    "dividendYieldSource", "dividendYieldKind", "dividendYieldBasis", "dividendPayoutRatio",
    "dividendPayoutRatioStatus", "dividendPayoutRatioNote", "doe", "doeStatus", "equityRatio",
    "salesGrowth", "profitGrowth", "earningsAnnouncementDate", "earningsAnnouncementSource",
    "exDividendDate", "exDividendSource", "eventStatus",
    "dividendObservedAt",
)
FORECAST_FIELDS = (
    "dps", "dpsKind", "dpsAsOf", "dpsSource", "dpsUrl", "dividendYield",
    "dividendYieldAsOf", "dividendYieldSource", "dividendYieldKind",
    "earningsAnnouncementDate", "earningsAnnouncementSource", "exDividendDate", "exDividendSource",
    "eventStatus",
)


def numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_state(state: object) -> None:
    if not isinstance(state, dict) or state.get("schemaVersion") != 1:
        raise ValueError("Invalid fundamentals state version")
    for key in ("companies", "reportIndex"):
        if not isinstance(state.get(key), dict):
            raise ValueError(f"Invalid fundamentals state: {key}")
    for code, entry in state["companies"].items():
        if not re.fullmatch(r"[0-9A-Z]{4}", code) or not isinstance(entry, dict):
            raise ValueError("Invalid company record")
        for key in ("financial", "forecast", "edinetAttempt", "yahooAttempt"):
            if key in entry and not isinstance(entry[key], dict):
                raise ValueError(f"Invalid company section: {key}")
        for section in ("financial", "forecast"):
            for key, value in entry.get(section, {}).items():
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError(f"Non-finite financial value: {key}")
    for day, entry in state["reportIndex"].items():
        datetime.strptime(day, "%Y-%m-%d")
        if not isinstance(entry, dict) or not isinstance(entry.get("results"), list):
            raise ValueError("Invalid report index")


def seed_state(dataset: dict) -> dict:
    state = {"schemaVersion": 1, "companies": {}, "reportIndex": {}}
    rows = dataset.get("edinetFundamentals") or dataset.get("searchUniverse") or []
    for row in rows:
        code = str(row.get("code", ""))
        if not re.fullmatch(r"[0-9A-Z]{4}", code):
            continue
        facts = dict(row.get("fundamentals") or (row if row.get("docID") else {}))
        forecast = {key: facts.get(key) if facts.get(key) is not None else row.get(key) for key in FORECAST_FIELDS}
        forecast = {key: value for key, value in forecast.items() if value is not None}
        if forecast.get("dpsKind") != "forecast":
            forecast = {key: value for key, value in forecast.items() if not key.startswith("dps")}
        if facts:
            if facts.get("dpsKind") == "forecast" or "会社予想" in str(facts.get("dpsSource", "")):
                facts["dps"] = None
            facts["actualDps"] = facts.get("actualDps") if facts.get("actualDps") is not None else facts.get("dps")
            facts["actualDpsAsOf"] = facts.get("actualDpsAsOf")
            for key in FORECAST_FIELDS:
                if key != "dps":
                    facts.pop(key, None)
        if facts or forecast:
            state["companies"][code] = {"financial": facts, "forecast": forecast}
    validate_state(state)
    return state


def load_state(previous: dict, loader, path: Path = STATE_PATH) -> dict:
    url = os.environ.get("FUNDAMENTALS_STATE_URL", "").strip()
    if url:
        bootstrap = False
        try:
            state = loader(url)
        except HTTPError as error:
            if error.code != 404:
                raise RuntimeError("Financial state restore failed; retain published data") from None
            bootstrap = True
        except json.JSONDecodeError:
            # Pages can return its HTML fallback with HTTP 200 for a missing file.
            bootstrap = True
        if bootstrap:
            legacy = loader(url.rsplit("/", 1)[0] + "/latest-candidates.json")
            if not isinstance(legacy, dict) or not legacy.get("searchUniverse"):
                raise ValueError("Cannot bootstrap financial state from an empty snapshot")
            if legacy.get("factorVersion") == "topix-standard-capital-gain-v3.2":
                raise ValueError("Financial state missing after migration; retain published data")
            state = seed_state(legacy)
        validate_state(state)
        return state
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        validate_state(state)
        return state
    return seed_state(previous)


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    validate_state(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def select_codes(codes: list[str], state: dict, provider: str, limit: int, now: str, reports=None,
                 retry_failed_today: bool = False) -> list[str]:
    today = now[:10]
    cutoff = (datetime.fromisoformat(today) - timedelta(days=7)).date().isoformat()
    companies = state["companies"]
    ordered = []
    for code in codes:
        entry = companies.get(code, {})
        attempt = entry.get(provider + "Attempt", {})
        stamp = str(attempt.get("at") or "")
        facts = entry.get("financial" if provider == "edinet" else "forecast", {})
        changed = bool(reports and reports.get(code) and reports[code].get("docID") != facts.get("docID"))
        untried_document = changed and attempt.get("docID") != reports[code].get("docID")
        outdated = provider == "edinet" and facts.get("docID") and facts.get("factVersion") != FACT_VERSION
        version_needs_attempt = outdated and attempt.get("factVersion") != FACT_VERSION
        if not untried_document and not version_needs_attempt and (
            (stamp[:10] == today and not (retry_failed_today and attempt.get("status") == "error"))
            or (attempt.get("status") == "not-found" and stamp[:10] > cutoff)
        ):
            continue
        if changed:
            priority = 0
        elif outdated or (attempt.get("status") == "error" and not facts):
            priority = 1
        else:
            priority = 2 if not facts else 3
        ordered.append((priority, stamp, code))
    return [item[2] for item in sorted(ordered)[:limit]]


def merge_forecast(previous: dict, incoming: dict, now: str) -> dict:
    merged = dict(previous)
    observed = dict(previous.get("observedAt", {}))
    for key, value in incoming.items():
        if key in FORECAST_FIELDS and value is not None:
            merged[key] = value
            observed[key] = now
    merged["observedAt"] = observed
    return merged


def attach_facts(dataset: dict, state: dict, now: str) -> None:
    for row in dataset.get("searchUniverse", []):
        code = str(row["code"])
        entry = state["companies"].get(code, {})
        financial = dict(entry.get("financial", {}))
        forecast = entry.get("forecast", {})
        # Actual DPS is retained alongside the independent company forecast.
        facts = {**financial, **{key: value for key, value in forecast.items() if key in FORECAST_FIELDS}}
        metrics = calculate_valuation_metrics(facts, row.get("latestClose"))
        # The primary yield is the provider's forecast, observed at its own price/date.
        metrics["actualDividendYield"] = calculate_valuation_metrics(financial, row.get("latestClose")).get("dividendYield")
        metrics["dividendYield"] = forecast.get("dividendYield")
        metrics["dividendYieldKind"] = "forecast" if numeric(forecast.get("dividendYield")) else None
        metrics["dividendYieldBasis"] = "forecast-yield" if numeric(forecast.get("dividendYield")) else None
        metrics["valuationPriceAsOf"] = row.get("priceAsOf")
        metrics["dividendObservedAt"] = forecast.get("observedAt", {}).get("dividendYield")
        if financial:
            row["fundamentals"] = metrics
        for key in FIELDS:
            row[key] = metrics.get(key)
        for key in ("earningsAnnouncementDate", "exDividendDate"):
            if row.get(key) and str(row[key]) < now[:10]:
                row[key] = None
                metrics[key] = None
        sources = row.setdefault("sources", {})
        edinet_attempt = entry.get("edinetAttempt", {})
        sources["edinet"] = {
            "url": financial.get("url"), "updatedAt": financial.get("asOf"),
            "checkedAt": edinet_attempt.get("at"),
            "status": "available" if financial else "missing",
            "refreshStatus": edinet_attempt.get("status", "not-attempted"),
        }
        sources["dividend"] = {
            "url": forecast.get("dpsUrl"),
            "updatedAt": forecast.get("observedAt", {}).get("dividendYield"),
            "status": "available" if numeric(row.get("dividendYield")) else "missing",
            "refreshStatus": entry.get("yahooAttempt", {}).get("status", "not-attempted"),
            "basis": "Yahoo Finance会社予想", "maxAgeDays": 45,
        }
        row["financialAcquisition"] = {
            provider: entry.get(provider + "Attempt", {"status": "not-attempted"})
            for provider in ("edinet", "yahoo")
        }
        row["metricAvailability"] = {
            key: ("available" if numeric(row.get(key)) else
                  "not-applicable" if key == "per" and numeric(financial.get("eps")) and financial["eps"] <= 0 else
                  "not-applicable" if key == "pbr" and numeric(financial.get("bps")) and financial["bps"] <= 0 else
                  "source-field-missing" if financial else "not-acquired")
            for key in ("per", "pbr", "roe", "dividendYield", "dividendPayoutRatio", "doe")
        }
    by_code = {str(row["code"]): row for row in dataset.get("searchUniverse", [])}
    dataset["candidates"] = [by_code.get(str(row.get("code")), row) for row in dataset.get("candidates", [])]


def coverage(rows: list[dict]) -> dict:
    result = {}
    for name, group in (("all", rows), ("topix", [r for r in rows if r.get("isTopix")]),
                        ("standard", [r for r in rows if r.get("isStandard")])):
        fields = {}
        for field in ("per", "pbr", "roe", "dividendYield", "actualDps", "forecastDps", "dividendPayoutRatio", "doe", "earningsAnnouncementDate", "exDividendDate"):
            count = sum(bool(row.get(field)) if field.endswith("Date") else numeric(row.get(field)) for row in group)
            reasons = {}
            for row in group:
                if (bool(row.get(field)) if field.endswith("Date") else numeric(row.get(field))):
                    continue
                provider = "yahoo" if field in {"forecastDps", "dividendYield", "earningsAnnouncementDate", "exDividendDate"} else "edinet"
                attempt = row.get("financialAcquisition", {}).get(provider, {}).get("status", "not-attempted")
                reason = row.get("metricAvailability", {}).get(field)
                if reason != "not-applicable":
                    reason = "source-field-missing" if attempt == "available" else attempt
                reasons[reason] = reasons.get(reason, 0) + 1
            fields[field] = {"available": count, "total": len(group), "missing": len(group) - count, "missingReasons": reasons}
        result[name] = {"total": len(group), "fields": fields}
    return result


def validate_coverage(dataset: dict) -> list[str]:
    errors = []
    rows = dataset.get("searchUniverse", [])
    if dataset.get("financialCoverage") != coverage(rows):
        errors.append("Financial coverage does not match stock rows")
    for row in rows:
        facts = row.get("fundamentals", {})
        if numeric(row.get("dividendYield")) and row.get("dividendYieldKind") != "forecast":
            errors.append(f"{row.get('code')}: primary yield is not a forecast")
        for field, denominator, period_field in (("doe", "bps", "bpsAsOf"), ("dividendPayoutRatio", "eps", "epsAsOf")):
            if not numeric(row.get(field)):
                continue
            dps, base = row.get("actualDps"), facts.get(denominator)
            period = row.get("actualDpsAsOf")
            if (not numeric(dps) or not numeric(base) or base <= 0 or not period
                    or period != facts.get(period_field, facts.get("asOf"))
                    or not math.isclose(row[field], dps / base, rel_tol=1e-8, abs_tol=1e-8)):
                errors.append(f"{row.get('code')}: inconsistent {field} basis")
    return errors


def refresh_fundamentals(dataset: dict, now: str, state: dict) -> None:
    rows = dataset.get("searchUniverse", [])
    codes = sorted({str(row["code"]) for row in rows if re.fullmatch(r"[0-9A-Z]{4}", str(row.get("code", "")))})
    companies = state["companies"]
    for code in codes:
        companies.setdefault(code, {})
    retry_failed_today = os.environ.get("RETRY_FAILED_COMPANY_DATA") == "true"
    yahoo_targets = select_codes(
        codes, state, "yahoo", max(1, int(os.environ.get("YAHOO_COMPANY_MAX_DOWNLOADS", "225"))),
        now, retry_failed_today=retry_failed_today,
    )
    delay = max(0, float(os.environ.get("YAHOO_DIVIDEND_REQUEST_DELAY_SECONDS", "0.45")))

    def yahoo(code):
        try:
            return code, fetch_yahoo_dividend_forecast(code), "available", None
        except FreeMarketDataError as error:
            reason = str(error)
            if "配当予想・イベント日を確認できません" in reason:
                return code, None, "not-found", "not-found-on-page"
            return code, None, "error", reason if re.fullmatch(r"HTTP [0-9]{3}", reason) else "network-or-parse"
        except (ValueError, OSError):
            return code, None, "error", "network-or-parse"
        finally:
            if delay:
                time.sleep(delay)

    workers = max(1, min(4, int(os.environ.get("YAHOO_COMPANY_WORKERS", "4"))))
    consecutive_errors = 0
    yahoo_checked = 0
    yahoo_failures: dict[str, int] = {}
    yahoo_failure_samples: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for offset in range(0, len(yahoo_targets), workers):
            for code, incoming, status, reason in executor.map(yahoo, yahoo_targets[offset:offset + workers]):
                yahoo_checked += 1
                entry = companies[code]
                entry["yahooAttempt"] = {"at": now, "status": status, "reason": reason}
                if reason:
                    yahoo_failures[reason] = yahoo_failures.get(reason, 0) + 1
                    if len(yahoo_failure_samples) < 10:
                        yahoo_failure_samples.append(f"{code}: {reason}")
                consecutive_errors = consecutive_errors + 1 if status == "error" else 0
                if incoming is not None:
                    entry["forecast"] = merge_forecast(entry.get("forecast", {}), incoming, now)
            if consecutive_errors >= 12:
                break
    print(f"Company forecasts: attempted {yahoo_checked}, unavailable {yahoo_failures}", flush=True)
    if yahoo_failure_samples:
        print(f"Company forecast sample failures: {', '.join(yahoo_failure_samples)}", flush=True)
    api_key = os.environ.get("EDINET_API_KEY", "").strip()
    target_codes = []
    report_errors = []
    refreshed = 0
    consecutive_download_errors = 0
    if api_key:
        index = state["reportIndex"]
        recent, report_errors = fetch_recent_securities_reports(set(codes), api_key, lookback_days=7, index=index, checked_at=now)
        target_codes = select_codes(
            codes, state, "edinet", max(1, int(os.environ.get("EDINET_MAX_DOWNLOADS", "225"))),
            now, recent, retry_failed_today=retry_failed_today,
        )
        reports, older_errors = fetch_recent_securities_reports(set(target_codes), api_key,
            lookback_days=max(30, int(os.environ.get("EDINET_LOOKBACK_DAYS", "430"))), index=index, checked_at=now) if target_codes else ({}, [])
        report_errors.extend(older_errors)
        for count, code in enumerate(target_codes, 1):
            entry = companies[code]
            attempt = {"at": now, "status": "not-found", "factVersion": FACT_VERSION}
            report = reports.get(code)
            if report:
                attempt["docID"] = report["docID"]
                try:
                    prior = entry.get("financial", {})
                    if prior.get("submitDateTime") and str(report.get("submitDateTime", "")) < str(prior["submitDateTime"]):
                        raise EdinetError("Report lookup is older than retained facts")
                    if prior.get("docID") != report["docID"] or prior.get("factVersion") != FACT_VERSION:
                        raw, url = download_xbrl_zip(str(report["docID"]), api_key)
                        parsed = parse_financial_metrics_from_xbrl(raw)
                        if not parsed.get("asOf") or not any(numeric(parsed.get(k)) for k in ("eps", "bps", "profit", "equity")):
                            raise EdinetError("Financial facts are incomplete")
                        if prior.get("asOf") and str(parsed["asOf"]) < str(prior["asOf"]):
                            raise EdinetError("Report period is older than retained facts")
                        # Replacing the whole period avoids mixing facts from different years.
                        entry["financial"] = {
                            **parsed, **report, "url": url, "actualDps": parsed.get("dps"),
                            "actualDpsAsOf": parsed.get("actualDpsAsOf"), "factVersion": FACT_VERSION,
                            "retrievedAt": now, "dataHash": hashlib.sha256(raw).hexdigest(),
                        }
                        refreshed += 1
                    attempt["status"] = "available"
                except (EdinetError, ValueError, KeyError, OSError, zipfile.BadZipFile, ElementTree.ParseError):
                    attempt["status"] = "error"
            elif report_errors:
                attempt["status"] = "error"
            entry["edinetAttempt"] = attempt
            consecutive_download_errors = consecutive_download_errors + 1 if report and attempt["status"] == "error" else 0
            if count % 25 == 0 or count == len(target_codes):
                print(f"EDINET: checked {count}/{len(target_codes)}, downloaded {refreshed}", flush=True)
            if consecutive_download_errors >= 5:
                break
        cutoff = (datetime.fromisoformat(now[:10]) - timedelta(days=450)).date().isoformat()
        state["reportIndex"] = {day: record for day, record in index.items() if day >= cutoff}
    attach_facts(dataset, state, now)
    dataset["financialCoverage"] = coverage(rows)
    summary = dataset["financialCoverage"]["all"]["fields"]
    print(
        "Fundamental coverage: "
        + ", ".join(
            f"{field} {summary[field]['available']}/{summary[field]['total']}"
            for field in ("per", "pbr", "roe", "dividendYield", "actualDps", "forecastDps")
        ),
        flush=True,
    )
    available = sum(bool(row.get("fundamentals")) for row in rows)
    source = dataset["sources"]["edinetFundamentals"]
    source.update({
        "status": "available" if available == len(rows) and rows else "partial",
        "recordCount": available, "targetCount": len(rows), "batchTargetCount": len(target_codes),
        "refreshedCount": refreshed, "targetPolicy": "new-disclosures-missing-oldest-attempt",
        "companyPageAttemptCount": yahoo_checked,
        "checkedAt": now, "errorCount": len(report_errors) + sum(companies[c].get("edinetAttempt", {}).get("status") == "error" for c in target_codes),
        "perCount": sum(numeric(row.get("per")) for row in rows),
        "pbrCount": sum(numeric(row.get("pbr")) for row in rows),
        "roeCount": sum(numeric(row.get("roe")) for row in rows),
        "reason": f"財務取得済み {available}/{len(rows)}銘柄。今回確認 {len(target_codes)}、新規・更新 {refreshed}。未取得・新規開示・確認の古い銘柄を順次取得します。" + (" EDINET APIキー未設定。保存済み値を使用。" if not api_key else ""),
    })
    state["generatedAt"] = now
