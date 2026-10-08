from __future__ import annotations

import math
import re
import time
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from zipfile import BadZipFile

import xlrd
import openpyxl


MARGIN_HISTORY_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/margin/05.html"
MARGIN_CURRENT_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/margin/04.html"
MARGIN_HISTORY_FALLBACK = (
    "https://www.jpx.co.jp/markets/statistics-equities/margin/"
    "tvdivq0000001rq1-att/tvdivq0000015969.xls"
)
INVESTOR_ARCHIVE_BASE = (
    "https://www.jpx.co.jp/markets/statistics-equities/"
    "investor-type/00-00-archives-{index:02d}.html"
)
INVESTOR_CURRENT_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/investor-type/index.html"
INVESTOR_CATEGORIES = {
    "individual": ("個人",),
    "foreign": ("海外投資家", "外国人"),
    "investmentTrust": ("投資信託",),
    "businessCorporation": ("事業法人",),
    "trustBank": ("信託銀行",),
    "proprietary": ("自己計",),
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
    if content.startswith(b"PK"):
        return parse_margin_xlsx_workbook(content)
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


def parse_margin_xlsx_workbook(content: bytes) -> list[dict[str, Any]]:
    try:
        workbook = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=True)
    except (OSError, ValueError, KeyError, BadZipFile) as error:
        raise JpxWeeklyError("信用取引現在高Excelを開けませんでした。") from error
    try:
        for sheet in workbook.worksheets:
            rows = sheet.iter_rows(values_only=True)
            header = [tuple(row) for _, row in zip(range(10), rows)]
            if not any("Tokyo & Nagoya" in str(cell) for row in header for cell in row):
                continue
            total_column = next(
                (column for row in header[:5] for column, cell in enumerate(row) if "Total" in str(cell)),
                None,
            )
            labels = next(
                (
                    row for row in header
                    if any("Shares Sold Short" in str(cell) for cell in row)
                    and any("Shares Bought on Margin" in str(cell) for cell in row)
                ),
                None,
            )
            if total_column is None or labels is None:
                continue
            sell_column = next(
                (index for index in range(total_column, len(labels)) if "Shares Sold Short" in str(labels[index])),
                None,
            )
            buy_column = next(
                (index for index in range(total_column, len(labels)) if "Shares Bought on Margin" in str(labels[index])),
                None,
            )
            if sell_column is None or buy_column is None or not any(
                "thous.shs." in str(row[sell_column]) and "thous.shs." in str(row[buy_column])
                for row in header if len(row) > buy_column
            ):
                continue
            parsed = []
            for row in rows:
                if not row or not isinstance(row[0], (date, datetime)) or len(row) <= buy_column:
                    continue
                sell_balance = _number(row[sell_column])
                buy_balance = _number(row[buy_column])
                if sell_balance is None or buy_balance is None:
                    continue
                parsed.append({
                    "weekEnd": row[0].date().isoformat() if isinstance(row[0], datetime) else row[0].isoformat(),
                    "sellBalanceThousandShares": round(sell_balance, 3),
                    "buyBalanceThousandShares": round(buy_balance, 3),
                    "marginRatio": round(buy_balance / sell_balance, 4) if sell_balance > 0 else None,
                })
            if parsed:
                by_date = {row["weekEnd"]: row for row in parsed}
                return [by_date[key] for key in sorted(by_date)]
        raise JpxWeeklyError("二市場合計の信用残高または単位を判定できません。")
    finally:
        workbook.close()


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


def parse_new_investor_workbook(content: bytes, source_url: str) -> dict[str, Any]:
    match = re.search(r"stock_1_w_(\d{8})_(\d{8})\.xlsx$", urlparse(source_url).path)
    if not match:
        raise JpxWeeklyError("投資部門別Excelの対象期間を確認できません。")
    try:
        start = date.fromisoformat(f"{match[1][:4]}-{match[1][4:6]}-{match[1][6:]}")
        end = date.fromisoformat(f"{match[2][:4]}-{match[2][4:6]}-{match[2][6:]}")
    except ValueError as error:
        raise JpxWeeklyError("投資部門別Excelの日付が不正です。") from error
    if not start <= end or (end - start).days > 7:
        raise JpxWeeklyError("投資部門別Excelの対象期間が不正です。")
    try:
        sheet = openpyxl.load_workbook(BytesIO(content), data_only=True).active
    except (OSError, ValueError, BadZipFile) as error:
        raise JpxWeeklyError("新形式の投資部門別Excelを開けませんでした。") from error
    cell = lambda row, column: sheet.cell(row, column).value
    if (
        "千円" not in str(cell(7, 3) or "")
        or "売" not in str(cell(7, 4) or "")
        or "買" not in str(cell(7, 5) or "")
        or "自己" not in str(cell(3, 4) or "")
        or "投資信託" not in str(cell(6, 32) or "")
        or "信託銀行" not in str(cell(6, 52) or "")
    ):
        raise JpxWeeklyError("投資部門別Excelの列配置または単位が想定外です。")
    market_row = next(
        (row for row in range(8, sheet.max_row or 8)
         if "二市場" in str(cell(row, 2) or "") and "株数" in str(cell(row, 3) or "")),
        None,
    )
    if market_row is None or "金額" not in str(cell(market_row + 1, 3) or ""):
        raise JpxWeeklyError("投資部門別Excelの二市場合計・金額行が見つかりません。")
    value_row = market_row + 1
    columns = {
        "foreign": (20, 24),
        "individual": (12, 16),
        "investmentTrust": (32,),
        "businessCorporation": (36,),
        "trustBank": (52,),
        "proprietary": (4, 8),
    }
    flows: dict[str, dict[str, float | None]] = {}
    for key, starts in columns.items():
        pairs = []
        for column in starts:
            sales = _number(cell(value_row, column))
            purchases = _number(cell(value_row, column + 1))
            balance = _number(cell(value_row, column + 2))
            if sales is None or purchases is None or balance is None or sales < 0 or purchases < 0:
                raise JpxWeeklyError(f"投資部門別Excelの{key}金額が欠損または不正です。")
            if abs((purchases - sales) - balance) > 1:
                raise JpxWeeklyError(f"投資部門別Excelの{key}売買差引が一致しません。")
            pairs.append((sales, purchases))
        total_sales = sum(pair[0] for pair in pairs)
        total_purchases = sum(pair[1] for pair in pairs)
        flows[key] = {
            "sales100mYen": round(total_sales / 100_000, 2),
            "purchases100mYen": round(total_purchases / 100_000, 2),
            "net100mYen": round((total_purchases - total_sales) / 100_000, 2),
        }
    return {
        "periodStart": start.isoformat(),
        "periodEnd": end.isoformat(),
        "unit": "100m-yen",
        "flows": flows,
    }


def parse_investor_workbook(content: bytes, source_url: str = "") -> dict[str, Any]:
    if content[:2] == b"PK":
        return parse_new_investor_workbook(content, source_url)
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
    links = re.findall(r"""href=["']([^"']+\.xlsx?(?:\?[^"']*)?)["']""", html, flags=re.IGNORECASE)
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
    newest_week = ""
    for link in sorted(dict.fromkeys(links), key=lambda item: item.lower().split("?", 1)[0].endswith(".xlsx")):
        try:
            parsed = parse_margin_workbook(fetch_bytes(link))
        except JpxWeeklyError:
            continue
        refreshed = _merge_rows(refreshed, parsed, key="weekEnd", limit=limit)
        if parsed and parsed[-1]["weekEnd"] >= newest_week:
            newest_week = parsed[-1]["weekEnd"]
            source_url = link
    if not refreshed:
        raise JpxWeeklyError("信用取引現在高の履歴を解析できませんでした。")
    try:
        current_html = fetch_bytes(MARGIN_CURRENT_PAGE).decode("utf-8", errors="replace")
    except JpxWeeklyError:
        current_html = ""
    current_links = sorted(
        extract_xls_links(current_html, MARGIN_CURRENT_PAGE, contains="mtseisan"),
        key=_link_sort_key,
    )
    for link in current_links[-12:]:
        try:
            row = parse_current_margin_workbook(fetch_bytes(link))
            refreshed.append(row)
            if row["weekEnd"] > newest_week:
                newest_week = row["weekEnd"]
                source_url = link
        except JpxWeeklyError:
            continue
    return _merge_rows(existing, refreshed, key="weekEnd", limit=limit), source_url


def _link_sort_key(url: str) -> str:
    filename = urlparse(url).path.rsplit("/", 1)[-1]
    match = re.search(r"(\d{6})(?=\.xls$)", filename)
    return match.group(1) if match else filename


def _investor_link_date(url: str) -> str:
    filename = urlparse(url).path.rsplit("/", 1)[-1]
    new = re.fullmatch(r"stock_1_w_\d{8}_(\d{8})\.xlsx", filename)
    if new:
        return new[1]
    old = re.fullmatch(r"stock_val_1_(\d{6})\.xlsx?", filename)
    return f"20{old[1]}" if old else ""


def fetch_investor_history(
    existing: object = None,
    *,
    limit: int = 104,
    initial_downloads: int = 52,
    refresh_downloads: int = 8,
) -> tuple[list[dict[str, Any]], list[str]]:
    links: list[str] = []
    for page_url in [INVESTOR_CURRENT_PAGE, *(INVESTOR_ARCHIVE_BASE.format(index=index) for index in range(3))]:
        try:
            html = fetch_bytes(page_url).decode("utf-8", errors="replace")
        except JpxWeeklyError:
            continue
        links.extend(extract_xls_links(html, page_url, contains="stock_val_1_"))
        links.extend(extract_xls_links(html, page_url, contains="stock_1_w_"))
    links = sorted({link for link in links if _investor_link_date(link)}, key=_investor_link_date)
    existing_count = len(existing) if isinstance(existing, list) else 0
    download_count = refresh_downloads if existing_count >= 26 else initial_downloads
    selected = links[-download_count:]
    refreshed: list[dict[str, Any]] = []
    successful_urls: list[str] = []
    for link in selected:
        try:
            refreshed.append(parse_investor_workbook(fetch_bytes(link), link))
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
