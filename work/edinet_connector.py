from __future__ import annotations

import json
import re
import zipfile
from datetime import date, timedelta
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree


EDINET_API_BASE = "https://api.edinet-fsa.go.jp/api/v2"
EDINET_DOCUMENTS_URL = f"{EDINET_API_BASE}/documents.json"


class EdinetError(RuntimeError):
    pass


def _request(url: str, api_key: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 CapitalGainRadar/0.4",
            "Ocp-Apim-Subscription-Key": api_key,
            "Subscription-Key": api_key,
        },
    )
    try:
        with urlopen(request, timeout=45) as response:
            return response.read()
    except HTTPError as error:
        raise EdinetError(f"EDINET HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise EdinetError("EDINET APIの取得に失敗しました。") from error


def _json(url: str, api_key: str) -> dict[str, object]:
    try:
        return json.loads(_request(url, api_key).decode("utf-8"))
    except json.JSONDecodeError as error:
        raise EdinetError("EDINET APIのJSONを解析できません。") from error


def fetch_recent_securities_reports(
    target_codes: set[str],
    api_key: str,
    lookback_days: int = 430,
) -> tuple[dict[str, dict[str, object]], list[str]]:
    reports: dict[str, dict[str, object]] = {}
    errors: list[str] = []
    today = date.today()
    target_sec_codes = {f"{code}0" if re.fullmatch(r"\d{4}", code) else code for code in target_codes}
    for offset in range(lookback_days):
        target = today - timedelta(days=offset)
        query = urlencode({"date": target.isoformat(), "type": 2})
        url = f"{EDINET_DOCUMENTS_URL}?{query}"
        try:
            payload = _json(url, api_key)
        except EdinetError as error:
            errors.append(f"{target.isoformat()}: {error}")
            continue
        results = payload.get("results")
        if not isinstance(results, list):
            continue
        for item in results:
            if not isinstance(item, dict):
                continue
            sec_code = str(item.get("secCode") or "")
            doc_type = str(item.get("docTypeCode") or "")
            form_code = str(item.get("formCode") or "")
            doc_id = str(item.get("docID") or "")
            if sec_code not in target_sec_codes or not doc_id:
                continue
            if doc_type != "120" and form_code != "030000":
                continue
            code = sec_code[:4]
            current = reports.get(code)
            submit = str(item.get("submitDateTime") or target.isoformat())
            if not current or submit > str(current.get("submitDateTime") or ""):
                reports[code] = {
                    "code": code,
                    "docID": doc_id,
                    "edinetCode": item.get("edinetCode"),
                    "secCode": sec_code,
                    "filerName": item.get("filerName"),
                    "submitDateTime": submit,
                    "docDescription": item.get("docDescription"),
                    "url": f"{EDINET_API_BASE}/documents/{doc_id}",
                }
        if reports.keys() >= target_codes:
            break
    return reports, errors


def download_xbrl_zip(doc_id: str, api_key: str) -> tuple[bytes, str]:
    query = urlencode({"type": 1})
    url = f"{EDINET_API_BASE}/documents/{doc_id}?{query}"
    return _request(url, api_key), url


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _concept_name(value: str) -> str:
    fragment = value.rsplit("#", 1)[-1]
    return fragment.rsplit(":", 1)[-1]


def _is_consolidated(element: ElementTree.Element) -> bool:
    text = "".join(element.itertext())
    return "NonConsolidatedMember" not in text and "個別" not in text


def _contexts(root: ElementTree.Element) -> dict[str, dict[str, object]]:
    contexts: dict[str, dict[str, object]] = {}
    for context in root.iter():
        if _local_name(context.tag) != "context":
            continue
        context_id = context.attrib.get("id")
        if not context_id:
            continue
        period = next((child for child in context if _local_name(child.tag) == "period"), None)
        if period is None:
            continue
        data: dict[str, object] = {"consolidated": _is_consolidated(context)}
        for child in period:
            name = _local_name(child.tag)
            if name in {"instant", "startDate", "endDate"}:
                data[name] = (child.text or "").strip()
        contexts[context_id] = data
    return contexts


def _concept_labels_from_linkbases(archive: zipfile.ZipFile) -> dict[str, list[str]]:
    labels: dict[str, list[str]] = {}
    for name in archive.namelist():
        lowered = name.lower().replace("\\", "/")
        if not lowered.endswith(".xml") or "label" not in lowered:
            continue
        try:
            root = ElementTree.fromstring(archive.read(name))
        except ElementTree.ParseError:
            continue

        locators: dict[str, str] = {}
        resources: dict[str, str] = {}
        for element in root.iter():
            local_name = _local_name(element.tag)
            label = element.attrib.get("{http://www.w3.org/1999/xlink}label")
            if local_name == "loc" and label:
                href = element.attrib.get("{http://www.w3.org/1999/xlink}href", "")
                locators[label] = _concept_name(href)
            elif local_name == "label" and label:
                text = " ".join("".join(element.itertext()).split())
                if text:
                    resources[label] = text

        for element in root.iter():
            if _local_name(element.tag) != "labelArc":
                continue
            from_label = element.attrib.get("{http://www.w3.org/1999/xlink}from")
            to_label = element.attrib.get("{http://www.w3.org/1999/xlink}to")
            concept = locators.get(from_label or "")
            text = resources.get(to_label or "")
            if concept and text:
                labels.setdefault(concept, [])
                if text not in labels[concept]:
                    labels[concept].append(text)
    return labels


def _number(text: str | None) -> float | None:
    if text is None:
        return None
    value = text.strip().replace(",", "")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _find_value(
    root: ElementTree.Element,
    contexts: dict[str, dict[str, object]],
    tag_names: tuple[str, ...],
    duration: bool,
    consolidated_only: bool = True,
) -> tuple[float | None, str | None]:
    candidates: list[tuple[str, float]] = []
    for element in root.iter():
        name = _local_name(element.tag)
        if name not in tag_names:
            continue
        value = _number(element.text)
        if value is None:
            continue
        context = contexts.get(element.attrib.get("contextRef", ""))
        if not context:
            continue
        if consolidated_only and not context.get("consolidated", False):
            continue
        key = str(context.get("endDate" if duration else "instant") or "")
        if duration and not context.get("startDate"):
            continue
        if not duration and not key:
            continue
        candidates.append((key, value))
    if not candidates:
        return None, None
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1], candidates[-1][0]


def _find_recent_values(
    root: ElementTree.Element,
    contexts: dict[str, dict[str, object]],
    tag_names: tuple[str, ...],
    duration: bool,
    consolidated_only: bool = True,
    limit: int = 2,
) -> list[tuple[str, float]]:
    by_period: dict[str, float] = {}
    for element in root.iter():
        name = _local_name(element.tag)
        if name not in tag_names:
            continue
        value = _number(element.text)
        if value is None:
            continue
        context = contexts.get(element.attrib.get("contextRef", ""))
        if not context:
            continue
        if consolidated_only and not context.get("consolidated", False):
            continue
        key = str(context.get("endDate" if duration else "instant") or "")
        if duration and not context.get("startDate"):
            continue
        if not duration and not key:
            continue
        by_period[key] = value
    return sorted(by_period.items(), key=lambda item: item[0])[-limit:]


def _find_recent_best_values(
    root: ElementTree.Element,
    contexts: dict[str, dict[str, object]],
    tag_names: tuple[str, ...],
    duration: bool,
    limit: int = 2,
) -> list[tuple[str, float]]:
    values = _find_recent_values(root, contexts, tag_names, duration, consolidated_only=True, limit=limit)
    if values:
        return values
    return _find_recent_values(root, contexts, tag_names, duration, consolidated_only=False, limit=limit)


def _find_best_value(
    root: ElementTree.Element,
    contexts: dict[str, dict[str, object]],
    tag_names: tuple[str, ...],
    duration: bool,
) -> tuple[float | None, str | None]:
    value, as_of = _find_value(root, contexts, tag_names, duration, consolidated_only=True)
    if value is not None:
        return value, as_of
    return _find_value(root, contexts, tag_names, duration, consolidated_only=False)


def _find_best_value_any_period(
    root: ElementTree.Element,
    contexts: dict[str, dict[str, object]],
    tag_names: tuple[str, ...],
) -> tuple[float | None, str | None]:
    value, as_of = _find_best_value(root, contexts, tag_names, duration=True)
    if value is not None:
        return value, as_of
    return _find_best_value(root, contexts, tag_names, duration=False)


def _find_best_value_by_name_pattern(
    root: ElementTree.Element,
    contexts: dict[str, dict[str, object]],
    required_tokens: tuple[str, ...],
    excluded_tokens: tuple[str, ...] = (),
) -> tuple[float | None, str | None]:
    required = tuple(token.lower() for token in required_tokens)
    excluded = tuple(token.lower() for token in excluded_tokens)
    names = {
        _local_name(element.tag)
        for element in root.iter()
        if all(token in _local_name(element.tag).lower() for token in required)
        and not any(token in _local_name(element.tag).lower() for token in excluded)
    }
    if not names:
        return None, None
    return _find_best_value_any_period(root, contexts, tuple(sorted(names)))


def _find_first_best_value_any_period(
    root: ElementTree.Element,
    contexts: dict[str, dict[str, object]],
    tag_names: tuple[str, ...],
) -> tuple[float | None, str | None]:
    for tag_name in tag_names:
        value, as_of = _find_best_value_any_period(root, contexts, (tag_name,))
        if value is not None:
            return value, as_of
    return None, None


def _normalized_text(value: str) -> str:
    return value.lower().replace(" ", "").replace("　", "")


def _is_dividend_per_share_candidate(name: str, labels: list[str]) -> bool:
    haystack = _normalized_text(" ".join((name, *labels)))
    if not ("dividend" in haystack or "配当" in haystack):
        return False
    if any(token in haystack for token in ("paid", "支払", "payout", "性向", "ratio", "forecast", "予想", "plan", "予定")):
        return False
    return (
        "pershare" in haystack
        or "percommonshare" in haystack
        or "1株" in haystack
        or "１株" in haystack
        or "一株" in haystack
        or "１単位" in haystack
    )


def _dividend_per_share_score(name: str, labels: list[str]) -> int:
    haystack = _normalized_text(" ".join((name, *labels)))
    score = 0
    if any(token in haystack for token in ("annual", "total", "年間", "年額", "合計", "通期")):
        score += 40
    if "summaryofbusinessresults" in haystack or "経営指標" in haystack or "業績" in haystack:
        score += 20
    if "cashdividends" in haystack or "cashdividend" in haystack or "剰余金" in haystack:
        score += 10
    if "totalannual" in haystack:
        score += 10
    return score


MAX_PLAUSIBLE_DPS = 2000

ANNUAL_DPS_TOKENS = (
    "annual",
    "totalannual",
    "total",
    "年間",
    "年額",
    "合計",
    "通期",
)

PARTIAL_DPS_TOKENS = (
    "interim",
    "quarter",
    "2ndquarter",
    "secondquarter",
    "yearend",
    "endoffiscalyear",
    "中間",
    "四半期",
    "第1四半期",
    "第１四半期",
    "第2四半期",
    "第２四半期",
    "第3四半期",
    "第３四半期",
    "期末",
)


def _dividend_per_share_text(name: str, labels: list[str]) -> str:
    return _normalized_text(" ".join((name, *labels)))


def _is_annual_dividend_per_share(name: str, labels: list[str]) -> bool:
    haystack = _dividend_per_share_text(name, labels)
    if not any(token in haystack for token in ANNUAL_DPS_TOKENS):
        return False
    return not any(token in haystack for token in PARTIAL_DPS_TOKENS)


def _is_partial_dividend_per_share(name: str, labels: list[str]) -> bool:
    haystack = _dividend_per_share_text(name, labels)
    return any(token in haystack for token in PARTIAL_DPS_TOKENS)


def _find_dividend_per_share_by_label(
    root: ElementTree.Element,
    contexts: dict[str, dict[str, object]],
    concept_labels: dict[str, list[str]],
) -> tuple[float | None, str | None]:
    candidates: list[dict[str, object]] = []
    for element in root.iter():
        name = _local_name(element.tag)
        labels = concept_labels.get(name, [])
        if not _is_dividend_per_share_candidate(name, labels):
            continue
        value = _number(element.text)
        if value is None or value <= 0:
            continue
        context = contexts.get(element.attrib.get("contextRef", ""))
        if not context:
            continue
        key = str(context.get("endDate") or context.get("instant") or "")
        if not key:
            continue
        if value > MAX_PLAUSIBLE_DPS:
            continue
        score = _dividend_per_share_score(name, labels)
        if context.get("consolidated", False):
            score += 5
        candidates.append({
            "name": name,
            "labels": labels,
            "score": score,
            "as_of": key,
            "value": value,
        })
    if not candidates:
        return None, None
    annual_candidates = [
        item for item in candidates
        if _is_annual_dividend_per_share(str(item["name"]), list(item["labels"]))
    ]
    if annual_candidates:
        annual_candidates.sort(
            key=lambda item: (str(item["as_of"]), int(item["score"]), float(item["value"])),
            reverse=True,
        )
        best = annual_candidates[0]
        return float(best["value"]), str(best["as_of"])

    by_period: dict[str, dict[str, float]] = {}
    for item in candidates:
        name = str(item["name"])
        labels = list(item["labels"])
        if not _is_partial_dividend_per_share(name, labels):
            continue
        as_of = str(item["as_of"])
        label_key = _dividend_per_share_text(name, labels)
        by_period.setdefault(as_of, {})
        by_period[as_of][label_key] = max(by_period[as_of].get(label_key, 0), float(item["value"]))
    for as_of in sorted(by_period, reverse=True):
        parts = list(by_period[as_of].values())
        if 2 <= len(parts) <= 4:
            total = sum(parts)
            if 0 < total <= MAX_PLAUSIBLE_DPS:
                return total, as_of
    return None, None


ANNUAL_DPS_CONCEPTS = (
    "AnnualDividendsPerShare",
    "AnnualDividendsPerShareSummaryOfBusinessResults",
    "CashDividendsPerShareAnnual",
    "CashDividendsPerShareSummaryOfBusinessResultsAnnual",
    "DividendPerShareAnnual",
    "DividendsPerShareAnnual",
    "TotalAnnualDividendsPerShare",
)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _valid_ratio(value: float | None, maximum: float) -> float | None:
    if value is None:
        return None
    if value < 0 or value > maximum:
        return None
    return value


def _growth(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return current / previous - 1


def parse_financial_metrics_from_xbrl(zip_bytes: bytes) -> dict[str, object]:
    with zipfile.ZipFile(BytesIO(zip_bytes)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".xbrl")]
        if not names:
            raise EdinetError("EDINET ZIP内にPublicDocのXBRLがありません。")
        def priority(name: str) -> tuple[int, int, int, int]:
            lowered = name.lower().replace("\\", "/")
            return (
                0 if "/publicdoc/" in lowered else 1,
                0 if "jpcrp" in lowered else 1,
                1 if "/auditdoc/" in lowered else 0,
                len(name),
            )

        xbrl_name = sorted(names, key=priority)[0]
        root = ElementTree.fromstring(archive.read(xbrl_name))
        concept_labels = _concept_labels_from_linkbases(archive)

    contexts = _contexts(root)
    assets, instant_as_of = _find_best_value(root, contexts, (
        "Assets",
        "AssetsIFRS",
    ), duration=False)
    equity, equity_as_of = _find_best_value(root, contexts, (
        "Equity",
        "EquityAttributableToOwnersOfParent",
        "NetAssets",
        "NetAssetsSummaryOfBusinessResults",
    ), duration=False)
    profit, duration_as_of = _find_best_value(root, contexts, (
        "ProfitLossAttributableToOwnersOfParent",
        "ProfitLoss",
        "ProfitLossIFRS",
        "NetIncomeLoss",
        "NetIncomeLossAttributableToOwnersOfParent",
    ), duration=True)
    sales_values = _find_recent_best_values(root, contexts, (
        "NetSales",
        "NetSalesSummaryOfBusinessResults",
        "Revenue",
        "RevenueIFRS",
        "OperatingRevenue",
        "OperatingRevenueIFRS",
        "SalesRevenue",
    ), duration=True)
    profit_values = _find_recent_best_values(root, contexts, (
        "ProfitLossAttributableToOwnersOfParent",
        "ProfitLoss",
        "ProfitLossIFRS",
        "NetIncomeLoss",
        "NetIncomeLossAttributableToOwnersOfParent",
    ), duration=True)
    sales = sales_values[-1][1] if sales_values else None
    previous_sales = sales_values[-2][1] if len(sales_values) >= 2 else None
    previous_profit = profit_values[-2][1] if len(profit_values) >= 2 else None
    eps, _ = _find_best_value(root, contexts, (
        "BasicEarningsLossPerShare",
        "BasicEarningsLossPerShareSummaryOfBusinessResults",
        "BasicEarningsLossPerShareIFRS",
        "BasicEarningsLossPerShareUSGAAP",
    ), duration=True)
    bps, _ = _find_best_value(root, contexts, (
        "NetAssetsPerShare",
        "EquityAttributableToOwnersOfParentPerShare",
        "NetAssetsPerShareSummaryOfBusinessResults",
        "EquityAttributableToOwnersOfParentPerShareIFRS",
    ), duration=False)
    dps, _ = _find_dividend_per_share_by_label(root, contexts, concept_labels)
    if dps is None:
        dps, _ = _find_first_best_value_any_period(root, contexts, ANNUAL_DPS_CONCEPTS)
    if dps is None:
        dps, _ = _find_best_value_by_name_pattern(
            root,
            contexts,
            required_tokens=("Dividend", "PerShare", "Annual"),
            excluded_tokens=("Paid", "Payout", "Ratio", "Forecast", "Plan", "Forecasts", "Planned", "Interim", "Quarter", "YearEnd"),
        )
    issued_shares, _ = _find_best_value(root, contexts, (
        "TotalNumberOfIssuedShares",
        "TotalNumberOfIssuedSharesSummaryOfBusinessResults",
        "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock",
        "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYear",
        "IssuedSharesTotalNumberOfSharesEtc",
        "TotalNumberOfIssuedSharesAsOfFiscalYearEndIssuedSharesTotalNumberOfSharesEtc",
    ), duration=False)
    treasury_shares, _ = _find_best_value(root, contexts, (
        "NumberOfTreasuryStockAtTheEndOfFiscalYear",
        "NumberOfTreasuryStockAtTheEndOfFiscalYearTreasuryStockEtc",
        "TreasuryStockShares",
    ), duration=False)
    average_shares, _ = _find_best_value(root, contexts, (
        "AverageNumberOfShares",
        "AverageNumberOfSharesSummaryOfBusinessResults",
        "AverageNumberOfSharesDuringTheFiscalYear",
        "AverageNumberOfSharesDuringThePeriod",
    ), duration=True)
    shares_outstanding = None
    if issued_shares is not None:
        shares_outstanding = max(0, issued_shares - (treasury_shares or 0))
    if eps is None:
        eps = _ratio(profit, average_shares or shares_outstanding)
    if bps is None:
        bps = _ratio(equity, shares_outstanding)

    return {
        "assets": assets,
        "equity": equity,
        "sales": sales,
        "previousSales": previous_sales,
        "profit": profit,
        "previousProfit": previous_profit,
        "salesGrowth": _growth(sales, previous_sales),
        "profitGrowth": _growth(profit, previous_profit),
        "eps": eps,
        "bps": bps,
        "dps": dps,
        "issuedShares": issued_shares,
        "treasuryShares": treasury_shares,
        "sharesOutstanding": shares_outstanding,
        "averageShares": average_shares,
        "roe": _ratio(profit, equity),
        "roa": _ratio(profit, assets),
        "equityRatio": _ratio(equity, assets),
        "asOf": instant_as_of or equity_as_of or duration_as_of,
        "xbrlFile": xbrl_name,
    }


def calculate_valuation_metrics(
    fundamentals: dict[str, object],
    latest_close: float | None,
) -> dict[str, object]:
    close = latest_close if isinstance(latest_close, (int, float)) and latest_close > 0 else None
    eps = fundamentals.get("eps")
    bps = fundamentals.get("bps")
    dps = fundamentals.get("dps")
    equity = fundamentals.get("equity")
    profit = fundamentals.get("profit")
    shares_outstanding = fundamentals.get("sharesOutstanding")
    provided_dividend_yield = fundamentals.get("dividendYield")
    dividend_yield = (
        provided_dividend_yield
        if isinstance(provided_dividend_yield, (int, float))
        else _ratio(dps if isinstance(dps, (int, float)) else None, close)
    )
    dividend_payout_ratio = _ratio(
        dps if isinstance(dps, (int, float)) else None,
        eps if isinstance(eps, (int, float)) else None,
    )
    doe = _ratio(profit, equity) if dps is None else _ratio(
        dps if isinstance(dps, (int, float)) else None,
        bps if isinstance(bps, (int, float)) else None,
    )
    return {
        **fundamentals,
        "marketCap": (
            close * shares_outstanding
            if close is not None and isinstance(shares_outstanding, (int, float)) and shares_outstanding > 0
            else None
        ),
        "per": _ratio(close, eps if isinstance(eps, (int, float)) else None),
        "pbr": _ratio(close, bps if isinstance(bps, (int, float)) else None),
        "dividendYield": _valid_ratio(dividend_yield, 0.25),
        "dividendPayoutRatio": _valid_ratio(dividend_payout_ratio, 3.0),
        "doe": _valid_ratio(doe, 0.25),
    }
