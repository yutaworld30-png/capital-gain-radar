from __future__ import annotations

import math
import re
import time
from datetime import date, timedelta
from io import BytesIO
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import xlrd


MARGIN_HISTORY_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/margin/06.html"
MARGIN_CURRENT_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/margin/04.html"
MARGIN_HISTORY_FALLBACK = (
    "https://www.jpx.co.jp/markets/statistics-equities/margin/"
    "tvdivq0000001rq1-att/tvdivq0000015969.xls"
)
INVESTOR_ARCHIVE_BASE = (
    "https://www.jpx.co.jp/markets/statistics-equities/"
    "investor-type/00-00-archives-{index:02d}.html"
)
INVESTOR_CATEGORIES = {
    "individual": ("個人",),
    "foreign": ("海外投資家", "外国人"),
    "investmentTrust": ("投資信託",),
    "businessCorporation": ("事業法人",),
    "trustBank": ("信託銀行",),
}


class JpxWeeklyError(RuntimeError):
    pass


def fetch_bytes(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 CapitalGainRadar/0.6",
            "Accept": "application/vnd.ms-excel,text/html,*/*",
        },
    )
    try:
        with urlopen(request, timeout=45) as response:
            return response.read()
    except HTTPError as error:
        raise JpxWeeklyError(f"HTTP {error.code}: {url}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise JpxWeeklyError(f"JPXデータを取得できませんでした: {url}") from error


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.replace(",", "").strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _cell(sheet: Any, row: int, column: int) -> object:
    if row < 0 or column < 0 or row >= sheet.nrows or column >= sheet.ncols:
        return None
    return sheet.cell_value(row, column)


def _excel_date(value: object, datemode: int) -> str | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    converter = getattr(xlrd, "xldate_as_datetime", None)
    try:
        if callable(converter):
            return converter(number, datemode).date().isoformat()
        base = date(1904, 1, 1) if datemode == 1 else date(1899, 12, 30)
        return (base + timedelta(days=int(number))).isoformat()
    except (ValueError, OverflowError):
        return None


def parse_margin_sheet(sheet: Any, *, datemode: int = 0) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row_index in range(10, sheet.nrows):
        week_end = _excel_date(_cell(sheet, row_index, 0), datemode)
        sell_balance = _number(_cell(sheet, row_index, 9))
        buy_balance = _number(_cell(sheet, row_index, 11))
        if not week_end or sell_balance is None or buy_balance is None:
            continue
        rows.append({
            "weekEnd": week_end,
            "sellBalanceThousandShares": round(sell_balance, 3),
            "buyBalanceThousandShares": round(buy_balance, 3),
            "marginRatio": round(buy_balance / sell_balance, 4) if sell_balance > 0 else None,
        })
    by_date = {str(row["weekEnd"]): row for row in rows}
    return [by_date[key] for key in sorted(by_date)]


def parse_margin_workbook(content: bytes) -> list[dict[str, Any]]:
    try:
        workbook = xlrd.open_workbook(file_contents=content)
    except xlrd.XLRDError as error:
        raise JpxWeeklyError("信用取引現在高Excelを開けませんでした。") from error
    preferred = next(
        (name for name in workbook.sheet_names() if name.strip() == "信用取引現在高"),
        None,
    )
    if preferred is None:
        preferred = next(
            (name for name in workbook.sheet_names() if "信用取引現在高" in name),
            None,
        )
    if preferred is None:
        raise JpxWeeklyError("信用取引現在高シートが見つかりません。")
    return parse_margin_sheet(workbook.sheet_by_name(preferred), datemode=workbook.datemode)


def _normalized_text(value: object) -> str:
    return re.sub(r"[\s　・･]+", "", str(value or ""))


def parse_current_margin_sheet(sheet: Any) -> dict[str, Any]:
    heading_text = " ".join(
        str(_cell(sheet, row, column) or "")
        for row in range(min(4, sheet.nrows))
        for column in range(sheet.ncols)
    )
    date_match = re.search(r"(20\d{2})/(\d{1,2})/(\d{1,2})", heading_text)
    if not date_match:
        raise JpxWeeklyError("直近信用残高Excelの基準日を判定できません。")
    week_end = date(*(int(date_match.group(index)) for index in range(1, 4))).isoformat()
    total_row = next(
        (
            row for row in range(sheet.nrows)
            if "二市場計" in _normalized_text(_cell(sheet, row, 1))
            and "株数" in _normalized_text(_cell(sheet, row, 2))
        ),
        None,
    )
    if total_row is None:
        raise JpxWeeklyError("直近信用残高Excelの二市場合計行を判定できません。")
    sell_balance = _number(_cell(sheet, total_row, 11))
    buy_balance = _number(_cell(sheet, total_row, 13))
    if sell_balance is None or buy_balance is None:
        raise JpxWeeklyError("直近信用残高Excelの売残・買残が数値ではありません。")
    return {
        "weekEnd": week_end,
        "sellBalanceThousandShares": round(sell_balance, 3),
        "buyBalanceThousandShares": round(buy_balance, 3),
        "marginRatio": round(buy_balance / sell_balance, 4) if sell_balance > 0 else None,
    }


def parse_current_margin_workbook(content: bytes) -> dict[str, Any]:
    try:
        workbook = xlrd.open_workbook(file_contents=content)
    except xlrd.XLRDError as error:
        raise JpxWeeklyError("直近信用取引現在高Excelを開けませんでした。") from error
    sheet_name = next(
        (name for name in workbook.sheet_names() if name.strip() == "レイアウト"),
        workbook.sheet_names()[0] if workbook.sheet_names() else None,
    )
    if sheet_name is None:
        raise JpxWeeklyError("直近信用取引現在高Excelにシートがありません。")
    return parse_current_margin_sheet(workbook.sheet_by_name(sheet_name))


def _period_dates(sheet: Any) -> tuple[str, str]:
    candidates = [
        str(_cell(sheet, row, column) or "")
        for row in range(min(8, sheet.nrows))
        for column in range(sheet.ncols)
    ]
    for text in candidates:
        year_match = re.search(r"(20\d{2})年", text)
        dates = re.findall(
            r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\d)",
            text,
        )
        if not year_match or len(dates) < 2:
            continue
        year = int(year_match.group(1))
        start_month, start_day = (int(value) for value in dates[-2])
        end_month, end_day = (int(value) for value in dates[-1])
        try:
            start_year = year - 1 if start_month > end_month else year
            return (
                date(start_year, start_month, start_day).isoformat(),
                date(year, end_month, end_day).isoformat(),
            )
        except ValueError:
            continue
    raise JpxWeeklyError("投資部門別Excelの対象期間を判定できません。")


def _category_row(sheet: Any, labels: tuple[str, ...]) -> int | None:
    normalized_labels = tuple(_normalized_text(label) for label in labels)
    for row_index in range(sheet.nrows):
        row_text = _normalized_text(
            "".join(str(_cell(sheet, row_index, column) or "") for column in range(min(4, sheet.ncols)))
        )
        if any(label in row_text for label in normalized_labels):
            return row_index
    return None


def parse_investor_sheet(sheet: Any) -> dict[str, Any]:
    period_start, period_end = _period_dates(sheet)
    current_value_column = 8
    flows: dict[str, dict[str, float | None]] = {}
    for key, labels in INVESTOR_CATEGORIES.items():
        row_index = _category_row(sheet, labels)
        if row_index is None:
            flows[key] = {"sales100mYen": None, "purchases100mYen": None, "net100mYen": None}
            continue
        sales = _number(_cell(sheet, row_index, current_value_column))
        purchases = _number(_cell(sheet, row_index + 1, current_value_column))
        divisor = 100_000.0
        flows[key] = {
            "sales100mYen": round(sales / divisor, 2) if sales is not None else None,
            "purchases100mYen": round(purchases / divisor, 2) if purchases is not None else None,
            "net100mYen": (
                round((purchases - sales) / divisor, 2)
                if sales is not None and purchases is not None
                else None
            ),
        }
    return {
        "periodStart": period_start,
        "periodEnd": period_end,
        "unit": "100m-yen",
        "flows": flows,
    }


def parse_investor_workbook(content: bytes) -> dict[str, Any]:
    try:
        workbook = xlrd.open_workbook(file_contents=content)
    except xlrd.XLRDError as error:
        raise JpxWeeklyError("投資部門別売買状況Excelを開けませんでした。") from error
    sheet_name = next(
        (name for name in workbook.sheet_names() if "Tokyo" in name and "Nagoya" in name),
        None,
    )
    if sheet_name is None:
        raise JpxWeeklyError("Tokyo & Nagoyaシートが見つかりません。")
    return parse_investor_sheet(workbook.sheet_by_name(sheet_name))


def extract_xls_links(html: str, base_url: str, *, contains: str = "") -> list[str]:
    links = re.findall(r"""href=["']([^"']+\.xls(?:\?[^"']*)?)["']""", html, flags=re.IGNORECASE)
    output: list[str] = []
    for link in links:
        absolute = urljoin(base_url, link)
        if contains and contains not in absolute:
            continue
        if absolute not in output:
            output.append(absolute)
    return output


def _merge_rows(
    existing: object,
    refreshed: list[dict[str, Any]],
    *,
    key: str,
    limit: int,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    if isinstance(existing, list):
        for row in existing:
            if isinstance(row, dict) and row.get(key):
                merged[str(row[key])] = row
    for row in refreshed:
        if row.get(key):
            merged[str(row[key])] = row
    return [merged[item] for item in sorted(merged)[-limit:]]


def fetch_margin_history(existing: object = None, *, limit: int = 160) -> tuple[list[dict[str, Any]], str]:
    page_html = fetch_bytes(MARGIN_HISTORY_PAGE).decode("utf-8", errors="replace")
    links = extract_xls_links(page_html, MARGIN_HISTORY_PAGE)
    links.append(MARGIN_HISTORY_FALLBACK)
    refreshed: list[dict[str, Any]] = []
    source_url = MARGIN_HISTORY_FALLBACK
    for link in dict.fromkeys(links):
        try:
            parsed = parse_margin_workbook(fetch_bytes(link))
        except JpxWeeklyError:
            continue
        if len(parsed) > len(refreshed):
            refreshed = parsed
            source_url = link
    if not refreshed:
        raise JpxWeeklyError("信用取引現在高の履歴を解析できませんでした。")
    current_html = fetch_bytes(MARGIN_CURRENT_PAGE).decode("utf-8", errors="replace")
    current_links = sorted(
        extract_xls_links(current_html, MARGIN_CURRENT_PAGE, contains="mtseisan"),
        key=_link_sort_key,
    )
    for link in current_links[-12:]:
        try:
            refreshed.append(parse_current_margin_workbook(fetch_bytes(link)))
            source_url = link
        except JpxWeeklyError:
            continue
    return _merge_rows(existing, refreshed, key="weekEnd", limit=limit), source_url


def _link_sort_key(url: str) -> str:
    filename = urlparse(url).path.rsplit("/", 1)[-1]
    match = re.search(r"(\d{6})(?=\.xls$)", filename)
    return match.group(1) if match else filename


def fetch_investor_history(
    existing: object = None,
    *,
    limit: int = 104,
    initial_downloads: int = 52,
    refresh_downloads: int = 8,
) -> tuple[list[dict[str, Any]], list[str]]:
    links: list[str] = []
    for index in range(3):
        page_url = INVESTOR_ARCHIVE_BASE.format(index=index)
        try:
            html = fetch_bytes(page_url).decode("utf-8", errors="replace")
        except JpxWeeklyError:
            continue
        links.extend(extract_xls_links(html, page_url, contains="stock_val_1_"))
    links = sorted(set(links), key=_link_sort_key)
    existing_count = len(existing) if isinstance(existing, list) else 0
    download_count = refresh_downloads if existing_count >= 26 else initial_downloads
    selected = links[-download_count:]
    refreshed: list[dict[str, Any]] = []
    successful_urls: list[str] = []
    for link in selected:
        try:
            refreshed.append(parse_investor_workbook(fetch_bytes(link)))
            successful_urls.append(link)
        except JpxWeeklyError:
            continue
        time.sleep(0.05)
    if not refreshed and not existing_count:
        raise JpxWeeklyError("投資部門別売買状況の履歴を解析できませんでした。")
    return (
        _merge_rows(existing, refreshed, key="periodEnd", limit=limit),
        successful_urls,
    )
