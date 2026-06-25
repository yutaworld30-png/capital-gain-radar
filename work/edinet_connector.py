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


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


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
    dps, _ = _find_best_value(root, contexts, (
        "DividendPaidPerShare",
        "AnnualDividendsPerShare",
        "CashDividendsPerShare",
        "CashDividendsPerShareSummaryOfBusinessResults",
        "DividendPerShare",
    ), duration=True)
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
        "profit": profit,
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
    return {
        **fundamentals,
        "marketCap": (
            close * shares_outstanding
            if close is not None and isinstance(shares_outstanding, (int, float)) and shares_outstanding > 0
            else None
        ),
        "per": _ratio(close, eps if isinstance(eps, (int, float)) else None),
        "pbr": _ratio(close, bps if isinstance(bps, (int, float)) else None),
        "dividendYield": _ratio(dps if isinstance(dps, (int, float)) else None, close),
        "dividendPayoutRatio": _ratio(
            dps if isinstance(dps, (int, float)) else None,
            eps if isinstance(eps, (int, float)) else None,
        ),
        "doe": _ratio(profit, equity) if dps is None else _ratio(
            dps if isinstance(dps, (int, float)) else None,
            bps if isinstance(bps, (int, float)) else None,
        ),
    }
