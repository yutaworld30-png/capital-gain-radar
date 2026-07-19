from __future__ import annotations

import json
import os
import re
import time
import zipfile
import xlrd
from io import BytesIO
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from jquants_connector import (
    FINANCIAL_DOCS_URL,
    PRICE_DOCS_URL,
    JQuantsError,
    calculate_price_metrics,
    fetch_financial_metrics,
    fetch_price_metrics,
)
from free_market_connector import (
    GOOGLE_FINANCE_URL,
    YAHOO_INFO_URL,
    YAHOO_MIRROR_URL,
    FreeMarketDataError,
    fetch_yahoo_dividend_forecast,
    fetch_yahoo_history,
    fetch_yahoo_mirror_latest,
    YAHOO_SPARK_MIRROR_URL,
)
from tdnet_connector import (
    TDNET_MAIN_URL,
    TDnetError,
    analyze_disclosures,
    fetch_recent_disclosures,
    fetch_theme_news_counts,
)
from edinet_connector import (
    EDINET_DOCUMENTS_URL,
    EdinetError,
    calculate_valuation_metrics,
    download_xbrl_zip,
    fetch_recent_securities_reports,
    parse_financial_metrics_from_xbrl,
)
from market_breadth import build_nikkei225_breadth


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs" / "data" / "latest-candidates.json"
SCORE_HISTORY_OUTPUT = ROOT / "outputs" / "data" / "score-history-v2.json"
PDF_INSPECTION_DIR = ROOT / "work" / "tmp" / "pdfs"
PAGES_BASE_URL = "https://yutaworld30-png.github.io/capital-gain-radar"
NIKKEI_URL = "https://indexes.nikkei.co.jp/en/nkave/index/component?idx=nk225"
JPX_LIST_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
JPX_LIST_FILE_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
JPX_MARGIN_URL = "https://www.jpx.co.jp/markets/statistics-equities/margin/05.html"
JPX_MARGIN_INDEX_URL = "https://www.jpx.co.jp/markets/statistics-equities/margin/index.html"
SCHEMA_VERSION = 2
SCORE_VERSION = "2.1.0"
FACTOR_VERSION = "nikkei225-capital-gain-v2.1"
PRICE_BASIS = "adjusted-ohlc"
HIGH_LOOKBACK_DAYS = 252
SOURCE_FRESHNESS_DAYS = {
    "priceHistory": 7,
    "marginRatio": 14,
    "theme": 10,
    "earnings": 35,
    "edinet": 550,
    "dividend": 45,
}
IMPORTANT_DISCLOSURE_KEYWORDS = (
    "決算短信",
    "業績予想",
    "上方修正",
    "下方修正",
    "増配",
    "減配",
    "自己株式",
    "公開買付",
    "TOB",
    "合併",
    "買収",
    "訴訟",
    "不正",
    "特別損失",
)


def scoring_contract_metadata() -> dict[str, object]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "universe": {
            "id": "nikkei225",
            "label": "日経225",
            "expectedCount": 225,
        },
        "scoreVersion": SCORE_VERSION,
        "factorVersion": FACTOR_VERSION,
        "priceBasis": PRICE_BASIS,
        "highLookbackDays": HIGH_LOOKBACK_DAYS,
    }


class NikkeiComponentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_td = False
        self.current = ""
        self.cells: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "td":
            self.in_td = True
            self.current = ""

    def handle_data(self, data: str) -> None:
        if self.in_td:
            self.current += data

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "td" and self.in_td:
            text = re.sub(r"\s+", " ", self.current).strip()
            if text:
                self.cells.append(text)
            self.in_td = False
            self.current = ""


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self.in_a = False
        self.current_href = ""
        self.current_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            values = dict(attrs)
            href = values.get("href")
            if href:
                self.in_a = True
                self.current_href = href
                self.current_text = ""

    def handle_data(self, data: str) -> None:
        if self.in_a:
            self.current_text += data

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self.in_a:
            label = re.sub(r"\s+", " ", self.current_text).strip()
            self.links.append({"href": self.current_href, "label": label})
            self.in_a = False
            self.current_href = ""
            self.current_text = ""


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "CapitalGainRadar/0.1"})
    with urlopen(request, timeout=30) as response:
        data = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return data.decode(charset, errors="replace")


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "CapitalGainRadar/0.1"})
    with urlopen(request, timeout=60) as response:
        return response.read()


def load_json_url(url: str) -> dict[str, object]:
    return json.loads(fetch_text(url))


def parse_nikkei_components(html: str) -> list[dict[str, str]]:
    parser = NikkeiComponentParser()
    parser.feed(html)
    components: list[dict[str, str]] = []
    cells = parser.cells
    for index in range(0, len(cells) - 1, 2):
        cell = cells[index]
        if re.fullmatch(r"[0-9A-Z]{4}", cell):
            name = cells[index + 1] if index + 1 < len(cells) else ""
            if name and not re.fullmatch(r"[0-9A-Z]{4}", name):
                components.append({"code": cell, "name": name})

    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for item in components:
        if item["code"] not in seen:
            unique.append(item)
            seen.add(item["code"])
    return unique


def parse_prime_components(content: bytes, nikkei_codes: set[str]) -> tuple[list[dict[str, object]], str]:
    workbook = xlrd.open_workbook(file_contents=content)
    sheet = workbook.sheet_by_index(0)
    headers = [str(value).strip() for value in sheet.row_values(0)]
    required = {"日付", "コード", "銘柄名", "市場・商品区分"}
    if not required.issubset(headers):
        raise ValueError("JPX上場銘柄一覧の列構成を確認できません。")
    indexes = {name: headers.index(name) for name in required}
    components: list[dict[str, object]] = []
    as_of = ""
    for row_index in range(1, sheet.nrows):
        row = sheet.row_values(row_index)
        market = str(row[indexes["市場・商品区分"]]).strip()
        if not market.startswith("プライム"):
            continue
        raw_code = row[indexes["コード"]]
        code = str(int(raw_code)) if isinstance(raw_code, float) and raw_code.is_integer() else str(raw_code).strip()
        name = str(row[indexes["銘柄名"]]).strip()
        raw_date = row[indexes["日付"]]
        date_text = str(int(raw_date)) if isinstance(raw_date, float) and raw_date.is_integer() else str(raw_date)
        if re.fullmatch(r"\d{8}", date_text):
            as_of = f"{date_text[:4]}-{date_text[4:6]}-{date_text[6:]}"
        if code in nikkei_codes and re.fullmatch(r"[0-9A-Z]{4}", code) and name:
            industry = ""
            for header_name in ("33業種区分", "17業種区分", "規模区分"):
                if header_name in headers:
                    industry = str(row[headers.index(header_name)]).strip()
                    if industry and industry != "-":
                        break
            components.append({
                "code": code,
                "name": name,
                "market": "日経225",
                "industry": industry or "業種未分類",
                "isNikkei225": code in nikkei_codes,
            })
    return components, as_of


def parse_margin_file_links(html: str) -> list[dict[str, str]]:
    parser = LinkParser()
    parser.feed(html)
    candidates: list[dict[str, str]] = []
    for link in parser.links:
        href = link["href"]
        label = link["label"]
        haystack = f"{href} {label}".lower()
        has_file_extension = any(ext in haystack for ext in [".csv", ".xls", ".xlsx", ".pdf", ".zip"])
        looks_related = any(keyword in haystack for keyword in ["margin", "credit", "shinyo", "taisyaku", "信用", "残高"])
        if has_file_extension and looks_related:
            candidates.append({
                "url": urljoin(JPX_MARGIN_URL, href),
                "label": label or href.rsplit("/", 1)[-1],
            })
    return candidates


def parse_margin_page_links(html: str) -> list[dict[str, str]]:
    parser = LinkParser()
    parser.feed(html)
    pages: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in parser.links:
        label = link["label"]
        url = urljoin(JPX_MARGIN_INDEX_URL, link["href"])
        if "/markets/statistics-equities/margin/" not in url:
            continue
        if not any(keyword in label for keyword in ["信用", "残高", "銘柄", "取引"]):
            continue
        if url in seen:
            continue
        pages.append({"url": url, "label": label or url.rsplit("/", 1)[-1]})
        seen.add(url)
    return pages


def parse_number(token: str) -> int:
    if not re.fullmatch(r"\d[\d,]*", token):
        raise ValueError(f"Invalid numeric token: {token}")
    return int(token.replace(",", ""))


def parse_margin_rows(reader: object) -> tuple[list[dict[str, object]], int]:
    row_pattern = re.compile(
        r"^[A-Z]\s+(?P<name>.+?)\s+(?P<raw_code>[0-9A-Z]{5})\s+"
        r"(?P<isin>JP[0-9A-Z]{10})\s+(?P<values>.+)$"
    )
    records: list[dict[str, object]] = []
    failures = 0
    for page in reader.pages:  # type: ignore[attr-defined]
        page_text = page.extract_text() or ""
        for raw_line in page_text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            match = row_pattern.match(line)
            if not match:
                continue
            tokens = match.group("values").split()
            try:
                sales = parse_number(tokens[0])
                index = 1
                if tokens[index] == "▲":
                    index += 1
                parse_number(tokens[index])
                index += 1
                purchases = parse_number(tokens[index])
            except (IndexError, ValueError):
                failures += 1
                continue

            raw_code = match.group("raw_code")
            code = raw_code[:-1] if raw_code.endswith("0") else raw_code
            name = re.sub(r"\s*普通株式$", "", match.group("name")).strip()
            records.append({
                "code": code,
                "name": name,
                "isin": match.group("isin"),
                "outstandingSales": sales,
                "outstandingPurchases": purchases,
                "marginRatio": round(purchases / sales, 4) if sales > 0 else None,
            })

    unique: dict[str, dict[str, object]] = {}
    for record in records:
        unique[str(record["code"])] = record
    return list(unique.values()), failures


def inspect_latest_margin_pdf(file_links: list[dict[str, str]]) -> dict[str, object]:
    dated_files: list[tuple[str, dict[str, str]]] = []
    for link in file_links:
        match = re.search(r"syumatsu(\d{8})\d*\.pdf", link["url"], flags=re.IGNORECASE)
        if match:
            dated_files.append((match.group(1), link))
    if not dated_files:
        return {"status": "not-found", "reason": "日付付きの銘柄別信用取引週末残高PDFが見つかりません。"}

    date_text, latest = max(dated_files, key=lambda item: item[0])
    pdf_bytes = fetch_bytes(latest["url"])
    result: dict[str, object] = {
        "status": "downloaded",
        "url": latest["url"],
        "asOf": f"{date_text[:4]}-{date_text[4:6]}-{date_text[6:]}",
        "byteLength": len(pdf_bytes),
    }
    PDF_INSPECTION_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = PDF_INSPECTION_DIR / "jpx-margin-latest.pdf"
    pdf_path.write_bytes(pdf_bytes)

    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(pdf_bytes))
        first_page_text = reader.pages[0].extract_text() or ""
        first_page_layout = reader.pages[0].extract_text(extraction_mode="layout") or ""
        fragments: list[dict[str, object]] = []

        def collect_fragment(text: str, cm: list[float], tm: list[float], font: object, size: float) -> None:
            cleaned = re.sub(r"\s+", " ", text).strip()
            if cleaned:
                fragments.append({
                    "text": cleaned,
                    "cm": [round(value, 2) for value in cm],
                    "tm": [round(value, 2) for value in tm],
                    "size": round(size, 2),
                })

        reader.pages[0].extract_text(visitor_text=collect_fragment)
        margin_records, parse_failures = parse_margin_rows(reader)
        text_path = PDF_INSPECTION_DIR / "jpx-margin-first-page.txt"
        text_path.write_text(first_page_text, encoding="utf-8")
        layout_path = PDF_INSPECTION_DIR / "jpx-margin-first-page-layout.txt"
        layout_path.write_text(first_page_layout, encoding="utf-8")
        fragments_path = PDF_INSPECTION_DIR / "jpx-margin-first-page-fragments.json"
        fragments_path.write_text(json.dumps(fragments, ensure_ascii=False, indent=2), encoding="utf-8")
        result["status"] = "text-extracted"
        result["pageCount"] = len(reader.pages)
        result["firstPageTextLength"] = len(first_page_text)
        result["recordCount"] = len(margin_records)
        result["parseFailureCount"] = parse_failures
        result["records"] = margin_records
    except ImportError:
        result["reason"] = "pypdfが未導入のため、PDFの表抽出は未確認です。"
    except Exception as error:
        result["status"] = "text-extraction-error"
        result["reason"] = str(error)
    return result


def extract_latest_date(text: str) -> str | None:
    patterns = [
        r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})",
        r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日",
    ]
    dates: list[datetime] = []
    for pattern in patterns:
        for year, month, day in re.findall(pattern, text):
            try:
                dates.append(datetime(int(year), int(month), int(day)))
            except ValueError:
                pass
    if not dates:
        return None
    return max(dates).date().isoformat()


def collect_jquants_metrics(dataset: dict[str, object], generated_at: str) -> bool:
    sources = dataset["sources"]  # type: ignore[assignment]
    price_source = sources["priceHistory"]  # type: ignore[index]
    financial_source = sources["fundamentals"]  # type: ignore[index]
    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        return False

    components = dataset.get("nikkei225Components")
    if not isinstance(components, list) or len(components) != 225:
        reason = "日経225構成銘柄の検証が完了していないため、J-Quants取得を停止しました。"
        price_source["status"] = "blocked"
        price_source["reason"] = reason
        financial_source["status"] = "blocked"
        financial_source["reason"] = reason
        return True

    today = date.today()
    from_date = (today - timedelta(days=760)).isoformat()
    to_date = today.isoformat()
    delay_seconds = max(0.0, float(os.environ.get("JQUANTS_REQUEST_DELAY_SECONDS", "1.05")))
    price_metrics: list[dict[str, object]] = []
    financial_metrics: list[dict[str, object]] = []
    price_errors: list[str] = []
    financial_errors: list[str] = []

    for index, component in enumerate(components):
        code = str(component.get("code", ""))
        name = str(component.get("name", ""))
        try:
            metrics = fetch_price_metrics(code, api_key, from_date, to_date)
            price_metrics.append({"code": code, "name": name, **metrics})
        except JQuantsError as error:
            price_errors.append(f"{code}: {error}")
        time.sleep(delay_seconds)

        try:
            metrics = fetch_financial_metrics(code, api_key)
            financial_metrics.append({"code": code, "name": name, **metrics})
        except JQuantsError as error:
            financial_errors.append(f"{code}: {error}")
        if index < len(components) - 1:
            time.sleep(delay_seconds)

    stale_prices = [
        item for item in price_metrics
        if not item.get("asOf") or (today - date.fromisoformat(str(item["asOf"]))).days > 7
    ]
    price_ok = len(price_metrics) == 225 and not stale_prices and not price_errors
    financial_ok = len(financial_metrics) == 225 and not financial_errors
    dataset["nikkei225Prices"] = price_metrics
    dataset["nikkei225Financials"] = financial_metrics

    price_source["status"] = "available" if price_ok else ("stale-data" if stale_prices else "partial")
    price_source["recordCount"] = len(price_metrics)
    price_source["staleCount"] = len(stale_prices)
    price_source["checkedAt"] = generated_at
    price_source["asOf"] = max((str(item.get("asOf") or "") for item in price_metrics), default=None)
    price_source["errors"] = price_errors[:10]
    price_source["reason"] = (
        "日経225全銘柄の調整済み株価と売買代金を確認しました。"
        if price_ok
        else f"価格データは{len(price_metrics)}/225件、鮮度不足は{len(stale_prices)}件です。"
    )

    financial_source["status"] = "available" if financial_ok else "partial"
    financial_source["recordCount"] = len(financial_metrics)
    financial_source["checkedAt"] = generated_at
    financial_source["asOf"] = max((str(item.get("asOf") or "") for item in financial_metrics), default=None)
    financial_source["errors"] = financial_errors[:10]
    financial_source["reason"] = (
        "日経225全銘柄の財務情報サマリーを確認しました。"
        if financial_ok
        else f"財務データは{len(financial_metrics)}/225件です。"
    )
    return True


def collect_free_market_metrics(
    dataset: dict[str, object],
    generated_at: str,
    previous_dataset: dict[str, object],
) -> None:
    sources = dataset["sources"]  # type: ignore[assignment]
    price_source = sources["priceHistory"]  # type: ignore[index]
    financial_source = sources["fundamentals"]  # type: ignore[index]
    components = dataset.get("nikkei225Components")
    if not isinstance(components, list) or len(components) != 225:
        price_source["status"] = "blocked"
        price_source["reason"] = "日経225採用225銘柄の検証が完了していません。"
        return

    today = date.today()
    start = today - timedelta(days=760)
    validated: list[dict[str, object]] = []
    errors: list[str] = []
    codes = [str(component.get("code", "")) for component in components]
    mirror_latest: dict[str, dict[str, object]] = {}

    def collect_mirror_batch(
        batch: list[str],
    ) -> None:
        try:
            mirror_latest.update(fetch_yahoo_mirror_latest(batch))
        except FreeMarketDataError as error:
            if len(batch) > 1:
                midpoint = len(batch) // 2
                collect_mirror_batch(batch[:midpoint])
                collect_mirror_batch(batch[midpoint:])
            else:
                errors.append(f"{batch[0]}: 終値照合データを取得できません（{error}）")

    for offset in range(0, len(codes), 50):
        collect_mirror_batch(codes[offset:offset + 50])
        time.sleep(0.15)

    component_map = {str(component.get("code", "")): component for component in components}
    for code in codes:
        component = component_map.get(code, {})
        name = str(component.get("name", ""))
        mirror = mirror_latest.get(code)
        if not mirror:
            errors.append(f"{code}: 終値照合データを取得できません。")
            continue
        try:
            primary_rows, primary_url = fetch_yahoo_history(code, start, today)
        except FreeMarketDataError as error:
            errors.append(f"{code}: OHLCV価格履歴を取得できません（{error}）")
            continue
        if len(primary_rows) < HIGH_LOOKBACK_DAYS:
            errors.append(f"{code}: OHLCV価格履歴が{HIGH_LOOKBACK_DAYS}営業日未満です。")
            continue
        validation_date = str(mirror.get("date") or "")
        primary_match = next(
            (
                item for item in reversed(primary_rows)
                if isinstance(item, dict) and str(item.get("Date")) == validation_date
            ),
            None,
        )
        if not primary_match:
            errors.append(f"{code}: OHLCV履歴と照合経路で共通する最新取引日がありません。")
            continue
        primary_close = float(primary_match["C"])
        mirror_close = float(mirror["close"])
        close_difference = abs(primary_close - mirror_close) / max(primary_close, mirror_close)
        if close_difference > 0.001:
            errors.append(f"{code}: 終値の差が許容範囲を超えています（{close_difference:.2%}）。")
            continue
        try:
            metrics = calculate_price_metrics(primary_rows)
        except JQuantsError as error:
            errors.append(f"{code}: {error}")
            continue
        latest_date = date.fromisoformat(str(metrics["asOf"]))
        if (today - latest_date).days > 7:
            errors.append(f"{code}: 最新株価が7日以上更新されていません。")
            continue
        validated.append({
            "code": code,
            "name": name,
            "isNikkei225": True,
            **metrics,
            "chartHistory": [
                {
                    "date": str(row.get("Date")),
                    "open": round(float(row.get("O") or 0), 4),
                    "high": round(float(row.get("H") or 0), 4),
                    "low": round(float(row.get("L") or 0), 4),
                    "close": round(float(row.get("C") or 0), 4),
                    "volume": int(float(row.get("V") or 0)),
                }
                for row in primary_rows[-260:]
                if (
                    isinstance(row, dict)
                    and row.get("Date")
                    and row.get("O")
                    and row.get("H")
                    and row.get("L")
                    and row.get("C")
                )
            ],
            "chartType": "ohlcv",
            "validationDate": validation_date,
            "validationCloseYahoo": primary_close,
            "validationCloseMirror": mirror_close,
            "validationDifference": round(close_difference, 6),
            "priceBasis": PRICE_BASIS,
            "highLookbackDays": HIGH_LOOKBACK_DAYS,
            "sources": {
                "priceHistory": {
                    "url": primary_url,
                    "updatedAt": str(metrics["asOf"]),
                    "provider": "Yahoo Finance chart OHLCV",
                },
                "priceValidation": {
                    "url": str(mirror.get("url") or YAHOO_SPARK_MIRROR_URL),
                    "updatedAt": validation_date,
                    "provider": "Yahoo Finance mirror spark",
                },
            },
        })

    validated.sort(key=lambda item: str(item["code"]))
    dataset["nikkei225Prices"] = validated
    dataset["primeMarketPrices"] = validated
    coverage = len(validated) / max(1, len(components))
    price_source["provider"] = "Yahoo Finance（2配信経路）"
    price_source["url"] = YAHOO_INFO_URL
    price_source["validationUrl"] = YAHOO_MIRROR_URL
    price_source["manualValidationUrl"] = GOOGLE_FINANCE_URL
    price_source["status"] = "available" if coverage >= 0.95 else "partial"
    price_source["recordCount"] = len(validated)
    price_source["universeCount"] = len(components)
    price_source["coverage"] = round(coverage, 4)
    price_source["checkedAt"] = generated_at
    price_source["asOf"] = max((str(item.get("asOf") or "") for item in validated), default=None)
    price_source["errors"] = errors[:25]
    price_source["reason"] = (
        f"Yahoo FinanceのOHLCVと別配信経路の終値を照合し、日経225の{len(validated)}/{len(components)}銘柄を確認しました。"
        if coverage >= 0.95
        else f"無料経路でOHLCVと終値を検証できた価格は{len(validated)}/{len(components)}件です。未検証銘柄は候補から除外します。"
    )

    financial_source["provider"] = "TDnet"
    financial_source["url"] = "https://www.release.tdnet.info/inbs/I_main_00.html"
    financial_source["status"] = "not-connected"
    financial_source["reason"] = "TDnetの決算・業績修正開示を使う無料判定へ切替中です。"


def augment_candidate_chart_histories(dataset: dict[str, object], generated_at: str) -> None:
    candidates = dataset.get("candidates")
    price_groups = [
        group for group in (
            dataset.get("primeMarketPrices"),
            dataset.get("nikkei225Prices"),
        )
        if isinstance(group, list)
    ]
    if not isinstance(candidates, list) or not price_groups:
        return

    candidate_codes = {
        str(item.get("code", ""))
        for item in candidates
        if isinstance(item, dict) and item.get("code")
    }
    nikkei_codes = {
        str(item.get("code", ""))
        for item in dataset.get("nikkei225Components", [])
        if isinstance(item, dict) and item.get("code")
    }
    detail_codes = sorted(candidate_codes | nikkei_codes)
    if not detail_codes:
        return

    price_maps = [
        {
            str(item.get("code", "")): item
            for item in prices
            if isinstance(item, dict) and item.get("code")
        }
        for prices in price_groups
    ]
    today = date.today()
    start = today - timedelta(days=760)
    detailed_count = 0
    errors: list[str] = []

    for code in detail_codes:
        targets = [price_map[code] for price_map in price_maps if code in price_map]
        if not targets:
            continue
        if all(
            target.get("chartType") == "ohlcv"
            and isinstance(target.get("chartHistory"), list)
            and len(target["chartHistory"]) >= HIGH_LOOKBACK_DAYS
            for target in targets
        ):
            detailed_count += 1
            continue
        try:
            rows, url = fetch_yahoo_history(code, start, today)
        except FreeMarketDataError as error:
            errors.append(f"{code}: OHLCV履歴を取得できませんでした（{error}）")
            continue
        chart_history = [
            {
                "date": str(row.get("Date")),
                "open": round(float(row.get("O") or 0), 4),
                "high": round(float(row.get("H") or 0), 4),
                "low": round(float(row.get("L") or 0), 4),
                "close": round(float(row.get("C") or 0), 4),
                "volume": int(float(row.get("V") or 0)),
            }
            for row in rows[-260:]
            if (
                isinstance(row, dict)
                and row.get("Date")
                and row.get("O")
                and row.get("H")
                and row.get("L")
                and row.get("C")
            )
        ]
        if len(chart_history) < 50:
            errors.append(f"{code}: OHLCV履歴が50営業日未満です。")
            continue
        for target in targets:
            target["chartHistory"] = chart_history
            target["chartType"] = "ohlcv"
            sources = target.setdefault("sources", {})
            if isinstance(sources, dict):
                sources["chartDetail"] = {
                    "url": url,
                    "updatedAt": chart_history[-1]["date"],
                    "provider": "Yahoo Finance chart",
                }
        detailed_count += 1
        time.sleep(0.08)

    sources = dataset.get("sources")
    if isinstance(sources, dict) and isinstance(sources.get("priceHistory"), dict):
        price_source = sources["priceHistory"]
        price_source["candidateOhlcvCount"] = detailed_count
        price_source["candidateOhlcvUniverse"] = len(candidate_codes)
        price_source["candidateOhlcvCheckedAt"] = generated_at
        price_source["candidateOhlcvErrors"] = errors[:10]


def collect_tdnet_and_build_candidates(dataset: dict[str, object], generated_at: str) -> None:
    sources = dataset["sources"]  # type: ignore[assignment]
    theme_source = sources["themeNews"]  # type: ignore[index]
    financial_source = sources["fundamentals"]  # type: ignore[index]
    components = dataset.get("nikkei225Components")
    prices = dataset.get("nikkei225Prices")
    margins = dataset.get("nikkei225Margin")
    required_source_keys = ("nikkei225", "primeMarket", "marginWeekly", "priceHistory")
    required_sources_available = all(
        isinstance(sources.get(key), dict)
        and sources[key].get("status") == "available"  # type: ignore[index]
        and sources[key].get("refreshStatus") != "error"  # type: ignore[index]
        for key in required_source_keys
    )
    if (
        not required_sources_available
        or not isinstance(components, list)
        or not isinstance(prices, list)
        or not isinstance(margins, list)
    ):
        theme_source["status"] = "blocked"
        theme_source["reason"] = "日経225・市場区分・価格・信用倍率の最新データ検証完了を待っています。"
        financial_source["status"] = "blocked"
        financial_source["reason"] = "必須取得元に更新失敗または鮮度不足があるため候補生成を停止しました。"
        return

    codes = {str(item.get("code", "")) for item in components}
    nikkei_codes = {
        str(item.get("code", ""))
        for item in dataset.get("nikkei225Components", [])
        if isinstance(item, dict)
    }
    try:
        disclosures, errors = fetch_recent_disclosures(codes)
        news_counts, news_errors = fetch_theme_news_counts()
        errors.extend(news_errors)
    except TDnetError as error:
        theme_source["status"] = "error"
        theme_source["reason"] = str(error)
        financial_source["status"] = "error"
        financial_source["reason"] = str(error)
        return

    price_map = {str(item["code"]): item for item in prices}
    margin_map = {str(item["code"]): item for item in margins}
    component_map = {str(item.get("code", "")): item for item in components if isinstance(item, dict)}
    financials, themes, theme_map = analyze_disclosures(
        codes,
        disclosures,
        price_map,
        news_counts,
    )
    financial_map = {str(item["code"]): item for item in financials}
    theme_score_map = {str(item["name"]): int(item["score"]) for item in themes}
    theme_detail_map = {str(item["name"]): item for item in themes}

    disclosures_by_code: dict[str, list[dict[str, str]]] = {}
    for disclosure in disclosures:
        disclosures_by_code.setdefault(disclosure["code"], []).append(disclosure)

    search_universe: list[dict[str, object]] = []
    for code in price_map:
        assigned_themes = theme_map.get(code, [])
        price = price_map.get(code)
        margin = margin_map.get(code)
        financial = financial_map.get(code)
        if not price or not margin or not financial:
            continue
        margin_ratio = margin.get("marginRatio")
        if not isinstance(margin_ratio, (int, float)):
            continue
        code_disclosures = disclosures_by_code.get(code, [])
        latest_disclosure = code_disclosures[-1] if code_disclosures else None
        important_disclosures = [
            {
                "date": disclosure.get("date"),
                "time": disclosure.get("time"),
                "title": disclosure.get("title"),
                "url": disclosure.get("url"),
            }
            for disclosure in reversed(code_disclosures)
            if any(keyword.upper() in str(disclosure.get("title") or "").upper() for keyword in IMPORTANT_DISCLOSURE_KEYWORDS)
        ][:3]
        theme_score = max((theme_score_map.get(theme, 0) for theme in assigned_themes), default=0)
        primary_theme = max(
            assigned_themes,
            key=lambda theme: theme_score_map.get(theme, 0),
        ) if assigned_themes else ""
        primary_theme_detail = theme_detail_map.get(primary_theme, {})
        price_sources = price.get("sources", {})
        candidate_name = str(margin.get("name") or price.get("name") or code)
        component = component_map.get(code, {})
        latest_close = price.get("latestClose")
        previous_high = price.get("high52w") or price.get("previousHigh")
        high_distance = None
        if (
            isinstance(latest_close, (int, float))
            and isinstance(previous_high, (int, float))
            and latest_close > 0
            and previous_high > 0
        ):
            high_distance = max(0.0, (float(previous_high) - float(latest_close)) / float(previous_high))
        search_universe.append({
            "code": code,
            "name": candidate_name,
            "market": "日経225",
            "industry": str(component.get("industry") or "業種未分類"),
            "isNikkei225": code in nikkei_codes,
            "themes": assigned_themes or ["テーマ未分類"],
            "theme": theme_score,
            "margin": round(float(margin_ratio), 2),
            "monthsFromHigh": float(price.get("monthsFromHigh", 0)),
            "high52wDistance": high_distance,
            "isNewHigh52w": price.get("isNewHigh52w") is True,
            "latestHigh": price.get("latestHigh"),
            "high52w": previous_high,
            "previousHigh": price.get("previousHigh"),
            "previousHighDate": price.get("previousHighDate"),
            "priceBasis": price.get("priceBasis", PRICE_BASIS),
            "highLookbackDays": price.get("highLookbackDays", HIGH_LOOKBACK_DAYS),
            "technical": int(price.get("technical", 0)),
            "averageTurnover20": price.get("averageTurnover20"),
            "atr14": price.get("atr14"),
            "recentLow20": price.get("recentLow20"),
            "suggestedStopPrice": price.get("suggestedStopPrice"),
            "suggestedStopWidth": price.get("suggestedStopWidth"),
            "suggestedStopBasis": price.get("suggestedStopBasis"),
            "executionEase": price.get("executionEase"),
            "onePercentTurnoverYen": price.get("onePercentTurnoverYen"),
            "return20": price.get("return20"),
            "liquidity": int(price.get("liquidity", 0)),
            "relative": 0,
            "earnings": int(financial.get("earnings", 50)),
            "risk": int(price.get("risk", 100)),
            "latestClose": price.get("latestClose"),
            "priceAsOf": price.get("asOf"),
            "supplyMemo": (
                f"売残 {int(margin.get('outstandingSales', 0)):,}株 / "
                f"買残 {int(margin.get('outstandingPurchases', 0)):,}株"
            ),
            "signals": financial.get("signals", []),
            "importantDisclosures": important_disclosures,
            "sources": {
                "marginRatio": {
                    "url": sources["marginWeekly"].get("pdfInspection", {}).get("url"),  # type: ignore[index]
                    "updatedAt": sources["marginWeekly"].get("asOf"),  # type: ignore[index]
                },
                "priceHistory": price_sources.get("priceHistory"),
                "priceValidation": price_sources.get("priceValidation"),
                "theme": {
                    "url": (
                        latest_disclosure["url"]
                        if latest_disclosure
                        else primary_theme_detail.get("newsUrl", TDNET_MAIN_URL)
                    ),
                    "updatedAt": primary_theme_detail.get("asOf", generated_at),
                },
                "earnings": {
                    "url": latest_disclosure["url"] if latest_disclosure else TDNET_MAIN_URL,
                    "updatedAt": latest_disclosure["date"] if latest_disclosure else generated_at,
                },
            },
        })

    dataset["tdnetDisclosures"] = disclosures
    dataset["themeNewsCounts"] = news_counts
    dataset["primeMarketFinancials"] = financials
    dataset["nikkei225Financials"] = [
        item for item in financials
        if str(item["code"]) in nikkei_codes
    ]
    dataset["themes"] = themes
    dataset["searchUniverse"] = search_universe
    dataset["candidates"] = search_universe
    dataset["candidatePolicy"] = {
        "scope": "nikkei225-only",
        "description": "日経225採用銘柄だけを対象とし、テーマ未分類銘柄も総合ランキングへ含めます。",
        "themeAssignedCount": sum(1 for item in search_universe if str(item["code"]) in theme_map),
        "unclassifiedCount": sum(1 for item in search_universe if str(item["code"]) not in theme_map),
    }
    augment_candidate_chart_histories(dataset, generated_at)

    theme_source["url"] = TDNET_MAIN_URL
    theme_source["provider"] = "Google News RSS + TDnet"
    theme_source["status"] = "available"
    theme_source["recordCount"] = len(disclosures)
    theme_source["themeCount"] = len(themes)
    theme_source["checkedAt"] = generated_at
    theme_source["errors"] = errors[:10]
    theme_source["reason"] = (
        f"直近7日のニュース件数とTDnet開示から{len(themes)}テーマを算出しました。"
        if themes
        else "TDnet直近31日の開示にテーマキーワードがありませんでした。"
    )

    financial_source["url"] = TDNET_MAIN_URL
    financial_source["provider"] = "TDnet"
    financial_source["status"] = "available" if len(financials) == len(codes) else "partial"
    financial_source["recordCount"] = len(financials)
    financial_source["checkedAt"] = generated_at
    financial_source["errors"] = errors[:10]
    financial_source["reason"] = (
        "TDnet直近31日の業績修正・増配・減配等を日経225全体で確認しました。"
        if len(financials) == len(codes)
        else f"TDnet業績判定は{len(financials)}/{len(codes)}件です。"
    )


def _supply_score(item: dict[str, object]) -> int:
    margin_value = item.get("margin")
    margin = float(margin_value) if isinstance(margin_value, (int, float)) else 99.0
    return round(max(0, min(100, 100 - (margin - 0.5) * 16)))


def _numeric_value(item: dict[str, object], key: str) -> float | None:
    value = item.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    fundamentals = item.get("fundamentals")
    if isinstance(fundamentals, dict):
        value = fundamentals.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _range_score(value: float | None, low: float, high: float, *, lower_is_better: bool) -> int:
    if value is None:
        return 50
    if lower_is_better:
        if value <= low:
            return 100
        if value >= high:
            return 0
        return round(100 - (value - low) / (high - low) * 100)
    if value >= high:
        return 100
    if value <= low:
        return 0
    return round((value - low) / (high - low) * 100)


def _valuation_score(item: dict[str, object]) -> int:
    per = _numeric_value(item, "per")
    pbr = _numeric_value(item, "pbr")
    roe = _numeric_value(item, "roe")
    per_score = 0 if per is None or per <= 0 else _range_score(per, 8, 35, lower_is_better=True)
    pbr_score = 0 if pbr is None or pbr <= 0 else _range_score(pbr, 0.8, 5, lower_is_better=True)
    roe_score = 0 if roe is None else _range_score(roe, 0.0, 0.18, lower_is_better=False)
    return round(per_score * 0.35 + pbr_score * 0.25 + roe_score * 0.4)


def _percentile_map(
    rows: list[dict[str, object]],
    key: str,
    *,
    higher_is_better: bool,
    positive_only: bool = False,
) -> dict[int, int]:
    values = [
        float(value)
        for row in rows
        if isinstance((value := _numeric_value(row, key)), (int, float))
        and (not positive_only or float(value) > 0)
    ]
    if not values:
        return {}
    scores: dict[int, int] = {}
    for row in rows:
        value = _numeric_value(row, key)
        if value is None or (positive_only and value <= 0):
            continue
        if len(values) == 1:
            percentile = 50.0
        else:
            less = sum(candidate < value for candidate in values)
            equal = sum(candidate == value for candidate in values)
            percentile = (less + max(0, equal - 1) / 2) / (len(values) - 1) * 100
        if not higher_is_better:
            percentile = 100 - percentile
        scores[id(row)] = round(max(0, min(100, percentile)))
    return scores


def _blended_peer_percentiles(
    rows: list[dict[str, object]],
    key: str,
    *,
    higher_is_better: bool,
    positive_only: bool = False,
) -> tuple[dict[int, int], dict[int, int]]:
    universe_scores = _percentile_map(
        rows,
        key,
        higher_is_better=higher_is_better,
        positive_only=positive_only,
    )
    industry_groups: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        industry_groups.setdefault(str(row.get("industry") or "業種未分類"), []).append(row)
    blended: dict[int, int] = {}
    peer_counts: dict[int, int] = {}
    for group_rows in industry_groups.values():
        industry_scores = _percentile_map(
            group_rows,
            key,
            higher_is_better=higher_is_better,
            positive_only=positive_only,
        )
        valid_count = len(industry_scores)
        for row in group_rows:
            row_id = id(row)
            universe_score = universe_scores.get(row_id)
            if universe_score is None:
                continue
            industry_score = industry_scores.get(row_id)
            if valid_count >= 5 and industry_score is not None:
                blended[row_id] = round(universe_score * 0.4 + industry_score * 0.6)
                peer_counts[row_id] = valid_count
            else:
                blended[row_id] = universe_score
                peer_counts[row_id] = len(universe_scores)
    return blended, peer_counts


def _apply_peer_factor_scores(rows: list[dict[str, object]]) -> None:
    relative_scores, relative_peers = _blended_peer_percentiles(
        rows,
        "return20",
        higher_is_better=True,
    )
    liquidity_scores, liquidity_peers = _blended_peer_percentiles(
        rows,
        "averageTurnover20",
        higher_is_better=True,
        positive_only=True,
    )
    per_scores, per_peers = _blended_peer_percentiles(
        rows,
        "per",
        higher_is_better=False,
        positive_only=True,
    )
    pbr_scores, pbr_peers = _blended_peer_percentiles(
        rows,
        "pbr",
        higher_is_better=False,
        positive_only=True,
    )
    roe_scores, roe_peers = _blended_peer_percentiles(
        rows,
        "roe",
        higher_is_better=True,
    )
    for row in rows:
        row_id = id(row)
        row["relative"] = relative_scores.get(row_id, 0)
        row["relativePeerCount"] = relative_peers.get(row_id, 0)
        row["liquidity"] = liquidity_scores.get(row_id, 0)
        row["liquidityPeerCount"] = liquidity_peers.get(row_id, 0)
        row["valuation"] = round(
            per_scores.get(row_id, 0) * 0.35
            + pbr_scores.get(row_id, 0) * 0.25
            + roe_scores.get(row_id, 0) * 0.40
        )
        row["valuationPeerCount"] = min(
            count for count in (
                per_peers.get(row_id, 0),
                pbr_peers.get(row_id, 0),
                roe_peers.get(row_id, 0),
            ) if count > 0
        ) if any((per_peers.get(row_id), pbr_peers.get(row_id), roe_peers.get(row_id))) else 0
        row["factorBasis"] = {
            "relative": "20営業日騰落率の全体40%・同業種60%パーセンタイル",
            "liquidity": "20営業日平均売買代金の全体40%・同業種60%パーセンタイル",
            "valuation": "PER/PBR/ROEの全体40%・同業種60%パーセンタイル",
            "supply": "JPX信用倍率のみ",
        }


def _has_numeric_metric(item: dict[str, object], key: str) -> bool:
    return _numeric_value(item, key) is not None


def _parse_source_date(value: object) -> date | None:
    if not value:
        return None
    text = str(value).strip()[:10].replace("/", "-")
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _source_status(source: object, *, max_age_days: int | None = None) -> str:
    if not isinstance(source, dict):
        return "missing"
    if source.get("refreshStatus") == "error":
        return "stale"
    explicit_status = str(source.get("status") or "")
    if explicit_status and explicit_status not in ("available", "reused"):
        return explicit_status
    source_date = _parse_source_date(source.get("updatedAt") or source.get("asOf") or source.get("checkedAt"))
    if not source.get("url") or source_date is None:
        return "missing"
    if max_age_days is not None and (date.today() - source_date).days > max_age_days:
        return "stale"
    return "reused" if explicit_status == "reused" else "available"


def _detail_status(label: str, status: str, message: str, *, as_of: object = None, source: str = "") -> dict[str, object]:
    return {
        "label": label,
        "status": status,
        "message": message,
        "asOf": as_of,
        "source": source,
    }


def _metric_basis(item: dict[str, object]) -> dict[str, object]:
    fundamentals = item.get("fundamentals") if isinstance(item.get("fundamentals"), dict) else {}
    return {
        "per": "実績EPSベース",
        "pbr": "実績BPSベース",
        "roe": "EDINET有価証券報告書ベース",
        "dividendYield": (
            "Yahoo Finance会社予想"
            if item.get("dividendYieldKind") == "forecast" or fundamentals.get("dividendYieldKind") == "forecast"  # type: ignore[union-attr]
            else "EDINET実績DPS/株価"
        ),
        "dps": (
            "Yahoo Finance会社予想"
            if item.get("dpsSource") or fundamentals.get("dpsSource")  # type: ignore[union-attr]
            else "EDINET実績DPS"
        ),
        "dividendPayoutRatio": (
            "非算出: 予想DPSと実績EPSが混在"
            if item.get("dividendPayoutRatioStatus") == "not-calculated-mixed-basis"
            or fundamentals.get("dividendPayoutRatioStatus") == "not-calculated-mixed-basis"  # type: ignore[union-attr]
            else "実績DPS/実績EPS"
        ),
    }


def _detect_anomalies(item: dict[str, object]) -> list[str]:
    anomalies: list[str] = []
    per = _numeric_value(item, "per")
    pbr = _numeric_value(item, "pbr")
    roe = _numeric_value(item, "roe")
    dividend_yield = _numeric_value(item, "dividendYield")
    payout = _numeric_value(item, "dividendPayoutRatio")
    if per is not None and per <= 0:
        anomalies.append("PERが0以下のため要確認")
    if pbr is not None and pbr <= 0:
        anomalies.append("PBRが0以下のため要確認")
    if roe is not None and abs(roe) >= 1:
        anomalies.append("ROEが±100%以上のため要確認")
    if dividend_yield is not None and dividend_yield >= 0.15:
        anomalies.append("配当利回りが15%以上のため要確認")
    if payout is not None and payout >= 3:
        anomalies.append("配当性向が300%以上のため要確認")
    return anomalies


def _data_quality_details(item: dict[str, object]) -> tuple[list[dict[str, object]], list[str]]:
    sources = item.get("sources") if isinstance(item.get("sources"), dict) else {}
    fundamentals = item.get("fundamentals") if isinstance(item.get("fundamentals"), dict) else None
    price_status = _source_status(sources.get("priceHistory"), max_age_days=SOURCE_FRESHNESS_DAYS["priceHistory"])
    margin_status = _source_status(sources.get("marginRatio"), max_age_days=SOURCE_FRESHNESS_DAYS["marginRatio"])
    edinet_status = _source_status(sources.get("edinet"), max_age_days=SOURCE_FRESHNESS_DAYS["edinet"])
    dividend_source = sources.get("dividend") if isinstance(sources.get("dividend"), dict) else {}
    dividend_status = _source_status(
        dividend_source,
        max_age_days=int(dividend_source.get("maxAgeDays", SOURCE_FRESHNESS_DAYS["dividend"])),
    )
    details = [
        _detail_status(
            "株価",
            price_status if isinstance(item.get("latestClose"), (int, float)) else "missing",
            (
                "価格履歴を取得済み"
                if price_status == "available"
                else "価格履歴が古い、または更新確認に失敗"
                if isinstance(item.get("latestClose"), (int, float))
                else "株価未取得"
            ),
            as_of=item.get("priceAsOf"),
            source="Yahoo Finance / J-Quants fallback",
        ),
        _detail_status(
            "財務",
            edinet_status if fundamentals else "missing",
            (
                "EDINET財務指標を取得済み"
                if edinet_status == "available"
                else "前回取得したEDINET財務指標を再利用"
                if edinet_status == "reused"
                else "EDINET財務指標が古い、または未取得"
            ),
            as_of=(fundamentals or {}).get("asOf") or (fundamentals or {}).get("submitDateTime") if fundamentals else None,
            source="EDINET API v2",
        ),
        _detail_status(
            "配当",
            dividend_status if _has_numeric_metric(item, "dividendYield") else "missing",
            (
                "Yahoo会社予想の配当利回りを取得済み"
                if item.get("dividendYieldKind") == "forecast" or (fundamentals or {}).get("dividendYieldKind") == "forecast"
                else "EDINET実績DPSと株価から配当利回りを算出済み"
            ),
            as_of=item.get("dividendYieldAsOf") or (fundamentals or {}).get("dividendYieldAsOf") if fundamentals else item.get("dividendYieldAsOf"),
            source="Yahoo Finance / EDINET",
        ),
        _detail_status(
            "信用",
            margin_status if isinstance(item.get("margin"), (int, float)) else "missing",
            (
                "JPX信用取引週末残高を取得済み"
                if margin_status == "available"
                else "信用倍率が古い、または更新確認に失敗"
                if isinstance(item.get("margin"), (int, float))
                else "信用倍率未取得"
            ),
            as_of=(sources.get("marginRatio") or {}).get("updatedAt") if isinstance(sources.get("marginRatio"), dict) else None,
            source="JPX",
        ),
    ]
    issues = [f"{detail['label']}: {detail['message']}" for detail in details if detail["status"] != "available"]
    return details, issues


def _data_quality(item: dict[str, object]) -> tuple[int, list[str]]:
    warnings: list[str] = []
    core_fields = (
        "margin",
        "monthsFromHigh",
        "technical",
        "liquidity",
        "relative",
        "earnings",
        "latestClose",
    )
    core_count = sum(1 for key in core_fields if isinstance(item.get(key), (int, float)))
    core_score = core_count / len(core_fields) * 70

    valuation_fields = ("per", "pbr", "roe")
    valuation_count = sum(1 for key in valuation_fields if _has_numeric_metric(item, key))
    valuation_score = valuation_count / len(valuation_fields) * 25
    if valuation_count < len(valuation_fields):
        missing = [key.upper() for key in valuation_fields if not _has_numeric_metric(item, key)]
        warnings.append("未取得: " + "/".join(missing))

    dividend_bonus = 5 if _has_numeric_metric(item, "dividendYield") else 0
    fundamentals = item.get("fundamentals")
    if isinstance(fundamentals, dict) and fundamentals.get("dividendPayoutRatioStatus") == "not-calculated-mixed-basis":
        warnings.append("配当性向は予想DPSと実績EPSが混在するため非算出")
    elif not _has_numeric_metric(item, "dividendPayoutRatio"):
        warnings.append("配当性向未取得")

    anomalies = _detect_anomalies(item)
    warnings.extend(anomalies)
    if anomalies:
        valuation_score = max(0, valuation_score - 10)

    details, source_issues = _data_quality_details(item)
    critical_labels = {"株価", "信用"}
    source_penalty = sum(
        20 if detail["label"] in critical_labels else 7
        for detail in details
        if detail["status"] not in ("available",)
    )
    warnings = [*source_issues, *warnings]
    score = round(core_score + valuation_score + dividend_bonus - source_penalty)
    return max(0, min(100, score)), warnings[:5]


def _total_score(item: dict[str, object]) -> int:
    liquidity = int(item["liquidity"]) if isinstance(item.get("liquidity"), (int, float)) else 60
    relative = int(item["relative"]) if isinstance(item.get("relative"), (int, float)) else 60
    earnings = int(item["earnings"]) if isinstance(item.get("earnings"), (int, float)) else 50
    risk = int(item["risk"]) if isinstance(item.get("risk"), (int, float)) else 50
    valuation = int(item["valuation"]) if isinstance(item.get("valuation"), (int, float)) else _valuation_score(item)
    base_score = round(
        int(item.get("theme") or 0) * 0.20
        + _supply_score(item) * 0.20
        + int(item.get("technical") or 0) * 0.15
        + relative * 0.13
        + earnings * 0.13
        + liquidity * 0.07
        + valuation * 0.08
        + (100 - risk) * 0.04
    )
    quality_score, _warnings = _data_quality(item)
    quality_penalty = max(0, 80 - quality_score) * 0.20
    return max(0, min(100, round(base_score - quality_penalty)))


def _score_explanation(item: dict[str, object]) -> dict[str, list[dict[str, object]] | list[str]]:
    factors = (
        ("theme", "テーマ性", 0.20, int(item.get("theme") or 0)),
        ("supply", "需給", 0.20, int(item.get("supply") or 0)),
        ("technical", "テクニカル", 0.15, int(item.get("technical") or 0)),
        ("relative", "相対強度", 0.13, int(item.get("relative") or 0)),
        ("earnings", "業績", 0.13, int(item.get("earnings") or 0)),
        ("liquidity", "流動性", 0.07, int(item.get("liquidity") or 0)),
        ("valuation", "割安・収益性", 0.08, int(item.get("valuation") or 0)),
        ("lowRisk", "低リスク性", 0.04, 100 - int(item.get("risk") or 0)),
    )
    positive = sorted(
        (
            {
                "factor": key,
                "label": label,
                "value": value,
                "impact": round(value * weight, 1),
                "text": f"{label} {value}点（総合へ+{value * weight:.1f}点）",
            }
            for key, label, weight, value in factors
        ),
        key=lambda reason: float(reason["impact"]),
        reverse=True,
    )[:3]
    negative = sorted(
        (
            {
                "factor": key,
                "label": label,
                "value": value,
                "impact": round((100 - value) * weight, 1),
                "text": f"{label} {value}点（満点比-{(100 - value) * weight:.1f}点）",
            }
            for key, label, weight, value in factors
        ),
        key=lambda reason: float(reason["impact"]),
        reverse=True,
    )[:3]
    quality_messages = [f"データ信頼度 {int(item.get('dataQuality') or 0)}点"]
    quality_messages.extend(str(value) for value in item.get("dataWarnings", []) if value)
    quality_messages.extend(str(value) for value in item.get("dataAnomalies", []) if value)
    quality_messages = list(dict.fromkeys(quality_messages))
    if len(quality_messages) < 3:
        quality_messages.append(f"株価基準日 {item.get('priceAsOf') or '未取得'}")
    if len(quality_messages) < 3:
        quality_messages.append(f"スコア版 {SCORE_VERSION}")
    return {
        "positive": positive,
        "negative": negative,
        "quality": quality_messages[:3],
    }


def _event_days(event_date: object, reference_date: date) -> int | None:
    parsed = _parse_source_date(event_date)
    return (parsed - reference_date).days if parsed else None


def _attach_event_summaries(dataset: dict[str, object], generated_at: str) -> None:
    reference_date = _parse_source_date(generated_at) or date.today()
    rows = dataset.get("searchUniverse")
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        events: list[dict[str, object]] = []
        for event_type, label, field, source_field in (
            ("earnings", "決算発表予定", "earningsAnnouncementDate", "earningsAnnouncementSource"),
            ("exDividend", "権利落ち", "exDividendDate", "exDividendSource"),
        ):
            event_date = row.get(field)
            days = _event_days(event_date, reference_date)
            if days is not None:
                events.append({
                    "type": event_type,
                    "label": label,
                    "date": event_date,
                    "daysFromNow": days,
                    "source": row.get(source_field),
                    "status": "upcoming" if days > 0 else "today" if days == 0 else "past",
                })
        disclosures = row.get("importantDisclosures")
        if isinstance(disclosures, list):
            for disclosure in disclosures[:3]:
                if not isinstance(disclosure, dict):
                    continue
                days = _event_days(disclosure.get("date"), reference_date)
                events.append({
                    "type": "disclosure",
                    "label": "重要開示",
                    "date": disclosure.get("date"),
                    "daysFromNow": days,
                    "title": disclosure.get("title"),
                    "url": disclosure.get("url"),
                    "status": "past",
                })
        events.sort(key=lambda event: (abs(int(event.get("daysFromNow") or 0)), str(event.get("date") or "")))
        row["events"] = events[:5]
        upcoming_days = [
            int(event["daysFromNow"])
            for event in events
            if isinstance(event.get("daysFromNow"), int) and int(event["daysFromNow"]) >= 0
        ]
        row["nextEventDays"] = min(upcoming_days) if upcoming_days else None
        row["eventWarning"] = bool(upcoming_days and min(upcoming_days) <= 7)


def _count_numeric(metrics: list[dict[str, object]], key: str) -> int:
    return sum(1 for item in metrics if isinstance(item.get(key), (int, float)))


def _attach_scores(dataset: dict[str, object]) -> None:
    unique_rows: list[dict[str, object]] = []
    seen: set[int] = set()
    for key in ("searchUniverse", "candidates"):
        rows = dataset.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and id(row) not in seen:
                seen.add(id(row))
                unique_rows.append(row)
    _apply_peer_factor_scores(unique_rows)
    for row in unique_rows:
        row["supply"] = _supply_score(row)
        row["dataQuality"], row["dataWarnings"] = _data_quality(row)
        row["dataQualityDetails"], row["acquisitionIssues"] = _data_quality_details(row)
        row["dataAnomalies"] = _detect_anomalies(row)
        row["metricBasis"] = _metric_basis(row)
        row["score"] = _total_score(row)
        row["scoreReasons"] = _score_explanation(row)
        row["scoreVersion"] = SCORE_VERSION
        row["factorVersion"] = FACTOR_VERSION
        row["priceBasis"] = row.get("priceBasis", PRICE_BASIS)
        row["highLookbackDays"] = HIGH_LOOKBACK_DAYS


def _attach_history_changes(
    rows: list[dict[str, object]],
    snapshots: list[dict[str, object]],
    snapshot_date: str,
) -> None:
    ranked_rows = sorted(
        [row for row in rows if isinstance(row.get("score"), (int, float))],
        key=lambda row: (-float(row["score"]), str(row.get("code") or "")),
    )
    current_ranks = {str(row.get("code")): index + 1 for index, row in enumerate(ranked_rows)}
    previous_snapshot = next(
        (
            snapshot for snapshot in reversed(snapshots)
            if isinstance(snapshot, dict)
            and str(snapshot.get("date") or "") < snapshot_date
            and isinstance(snapshot.get("rows"), list)
        ),
        None,
    )
    previous_rows = previous_snapshot.get("rows", []) if isinstance(previous_snapshot, dict) else []
    previous_map = {
        str(row.get("code")): row
        for row in previous_rows
        if isinstance(row, dict) and row.get("code")
    }
    previous_ranked = sorted(
        [row for row in previous_map.values() if isinstance(row.get("score"), (int, float))],
        key=lambda row: (-float(row["score"]), str(row.get("code") or "")),
    )
    previous_ranks = {str(row.get("code")): index + 1 for index, row in enumerate(previous_ranked)}
    factor_keys = ("theme", "supply", "technical", "relative", "earnings", "liquidity", "valuation", "risk")
    for row in rows:
        code = str(row.get("code") or "")
        current_rank = current_ranks.get(code)
        previous = previous_map.get(code)
        previous_rank = previous_ranks.get(code)
        row["rank"] = current_rank
        row["previousRank"] = previous_rank
        row["rankChange"] = previous_rank - current_rank if previous_rank and current_rank else None
        row["previousSnapshotDate"] = previous_snapshot.get("date") if isinstance(previous_snapshot, dict) else None
        row["scoreChange"] = (
            round(float(row["score"]) - float(previous["score"]), 1)
            if previous and isinstance(row.get("score"), (int, float)) and isinstance(previous.get("score"), (int, float))
            else None
        )
        factor_changes = {
            key: round(float(row[key]) - float(previous[key]), 1)
            for key in factor_keys
            if previous and isinstance(row.get(key), (int, float)) and isinstance(previous.get(key), (int, float))
        }
        row["factorChanges"] = factor_changes
        alerts: list[str] = []
        score_change = row.get("scoreChange")
        if isinstance(score_change, (int, float)) and abs(float(score_change)) >= 5:
            alerts.append(f"スコア{'急上昇' if score_change > 0 else '急低下'} {score_change:+.0f}点")
        if previous and row.get("isNewHigh52w") is True and previous.get("isNewHigh52w") is not True:
            alerts.append("52週高値を更新")
        if (
            previous
            and isinstance(row.get("dataQuality"), (int, float))
            and isinstance(previous.get("dataQuality"), (int, float))
            and float(row["dataQuality"]) - float(previous["dataQuality"]) <= -10
        ):
            alerts.append(f"データ品質が{float(row['dataQuality']) - float(previous['dataQuality']):.0f}点低下")
        if isinstance(row.get("rankChange"), int) and int(row["rankChange"]) >= 5:
            alerts.append(f"順位が{int(row['rankChange'])}位上昇")
        row["changeAlerts"] = alerts[:3]


def _compact_score_row(row: dict[str, object]) -> dict[str, object]:
    fields = (
        "code",
        "name",
        "industry",
        "isNikkei225",
        "score",
        "supply",
        "valuation",
        "theme",
        "technical",
        "relative",
        "earnings",
        "liquidity",
        "risk",
        "margin",
        "monthsFromHigh",
        "high52wDistance",
        "isNewHigh52w",
        "dataQuality",
        "dataWarnings",
        "dataAnomalies",
        "latestClose",
        "priceAsOf",
        "per",
        "pbr",
        "roe",
        "salesGrowth",
        "profitGrowth",
        "scoreVersion",
        "factorVersion",
        "priceBasis",
        "highLookbackDays",
        "rank",
        "previousRank",
        "rankChange",
        "scoreChange",
        "factorChanges",
        "changeAlerts",
        "previousSnapshotDate",
    )
    return {
        key: row[key]
        for key in fields
        if key in row and row[key] is not None
    }


def _load_existing_score_history() -> dict[str, object]:
    if SCORE_HISTORY_OUTPUT.exists():
        try:
            loaded = json.loads(SCORE_HISTORY_OUTPUT.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
        except (OSError, json.JSONDecodeError):
            pass
    history_url = os.environ.get("SCORE_HISTORY_URL", f"{PAGES_BASE_URL}/data/score-history-v2.json")
    try:
        loaded = load_json_url(history_url)
        if isinstance(loaded, dict):
            return loaded
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        pass
    return {"schemaVersion": SCHEMA_VERSION, "snapshots": []}


def update_score_history(dataset: dict[str, object], generated_at: str) -> dict[str, object]:
    rows = dataset.get("searchUniverse")
    if not isinstance(rows, list):
        rows = dataset.get("candidates")
    history = _load_existing_score_history()
    snapshots = history.get("snapshots")
    if not isinstance(snapshots, list):
        snapshots = []
    snapshots = [
        item for item in snapshots
        if isinstance(item, dict)
        and item.get("scoreVersion") == SCORE_VERSION
        and item.get("factorVersion") == FACTOR_VERSION
    ]

    snapshot_date = generated_at[:10]
    typed_rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    _attach_history_changes(typed_rows, snapshots, snapshot_date)
    compact_rows = [
        _compact_score_row(row)
        for row in typed_rows
        if row.get("code")
    ]
    snapshots = [
        item for item in snapshots
        if isinstance(item, dict) and item.get("date") != snapshot_date
    ]
    snapshots.append({
        "date": snapshot_date,
        "generatedAt": generated_at,
        "scoreVersion": SCORE_VERSION,
        "factorVersion": FACTOR_VERSION,
        "rowCount": len(compact_rows),
        "scoreMax": max((int(row["score"]) for row in compact_rows if isinstance(row.get("score"), int)), default=None),
        "scoreMin": min((int(row["score"]) for row in compact_rows if isinstance(row.get("score"), int)), default=None),
        "buy75Count": sum(1 for row in compact_rows if isinstance(row.get("score"), int) and int(row["score"]) >= 75),
        "sell65Count": sum(1 for row in compact_rows if isinstance(row.get("score"), int) and int(row["score"]) <= 65),
        "rows": compact_rows,
    })

    max_days = max(30, int(os.environ.get("SCORE_HISTORY_MAX_DAYS", "400")))
    snapshots.sort(key=lambda item: str(item.get("date", "")))
    snapshots = snapshots[-max_days:]

    updated = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": generated_at,
        "scoreVersion": SCORE_VERSION,
        "factorVersion": FACTOR_VERSION,
        "priceBasis": PRICE_BASIS,
        "highLookbackDays": HIGH_LOOKBACK_DAYS,
        "retentionDays": max_days,
        "snapshots": snapshots,
    }
    SCORE_HISTORY_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    SCORE_HISTORY_OUTPUT.write_text(json.dumps(updated, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    dataset["scoreHistorySummary"] = {
        "status": "available" if compact_rows else "empty",
        "snapshotCount": len(snapshots),
        "latestDate": snapshot_date,
        "latestRowCount": len(compact_rows),
        "url": "data/score-history-v2.json",
    }
    return updated


def collect_edinet_fundamentals(
    dataset: dict[str, object],
    generated_at: str,
    previous_dataset: dict[str, object],
) -> None:
    sources = dataset["sources"]  # type: ignore[assignment]
    source = sources["edinetFundamentals"]  # type: ignore[index]
    search_universe = dataset.get("searchUniverse")
    if not isinstance(search_universe, list) or not search_universe:
        source["status"] = "blocked"
        source["reason"] = "ランキング対象銘柄の生成後にEDINET財務指標を取得します。"
        return

    yahoo_company_delay = max(0.0, float(os.environ.get("YAHOO_DIVIDEND_REQUEST_DELAY_SECONDS", "0.45")))
    yahoo_company_limit = max(1, int(os.environ.get("YAHOO_COMPANY_MAX_DOWNLOADS", "225")))
    yahoo_company_data: dict[str, dict[str, object]] = {}
    yahoo_company_errors: list[str] = []
    company_rows = [
        row for row in search_universe
        if isinstance(row, dict) and re.fullmatch(r"\d{4}", str(row.get("code", "")))
    ][:yahoo_company_limit]
    for row in company_rows:
        code = str(row.get("code"))
        try:
            company_data = fetch_yahoo_dividend_forecast(code)
            yahoo_company_data[code] = company_data
            for field in (
                "dps",
                "dpsAsOf",
                "dpsSource",
                "dividendYield",
                "dividendYieldAsOf",
                "dividendYieldSource",
                "dividendYieldKind",
                "earningsAnnouncementDate",
                "earningsAnnouncementSource",
                "exDividendDate",
                "exDividendSource",
            ):
                if company_data.get(field) is not None:
                    row[field] = company_data[field]
            if any(company_data.get(field) for field in ("earningsAnnouncementDate", "exDividendDate")):
                row.setdefault("sources", {})["events"] = {  # type: ignore[index]
                    "url": company_data.get("dpsUrl", YAHOO_INFO_URL),
                    "updatedAt": generated_at,
                    "status": "available",
                }
            if isinstance(company_data.get("dividendYield"), (int, float)):
                row.setdefault("sources", {})["dividend"] = {  # type: ignore[index]
                    "url": company_data.get("dpsUrl", YAHOO_INFO_URL),
                    "updatedAt": company_data.get("dividendYieldAsOf") or generated_at,
                    "status": "available",
                    "basis": "Yahoo Finance会社予想",
                    "maxAgeDays": SOURCE_FRESHNESS_DAYS["dividend"],
                }
        except FreeMarketDataError as error:
            yahoo_company_errors.append(f"{code}: 配当予想・イベント日を取得できません（{error}）")
        if yahoo_company_delay:
            time.sleep(yahoo_company_delay)

    def attach(metrics: list[dict[str, object]], *, reused: bool = False) -> None:
        metric_map = {str(item.get("code")): item for item in metrics}
        for key in ("searchUniverse", "candidates"):
            rows = dataset.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                metric = metric_map.get(str(row.get("code")))
                if metric:
                    latest_close = row.get("latestClose")
                    row_metric = dict(metric)
                    row_metric.update(yahoo_company_data.get(str(row.get("code")), {}))
                    if isinstance(latest_close, (int, float)):
                        refreshed = calculate_valuation_metrics(row_metric, float(latest_close))
                        row_metric.update(refreshed)
                        row_metric["valuationPriceAsOf"] = row.get("priceAsOf")
                    row["fundamentals"] = row_metric
                    for field in (
                        "per",
                        "pbr",
                        "roe",
                        "marketCap",
                        "dps",
                        "dpsAsOf",
                        "dpsSource",
                        "dividendYield",
                        "dividendYieldAsOf",
                        "dividendYieldSource",
                        "dividendYieldKind",
                        "dividendYieldBasis",
                        "dividendPayoutRatio",
                        "dividendPayoutRatioStatus",
                        "dividendPayoutRatioNote",
                        "doe",
                        "equityRatio",
                        "salesGrowth",
                        "profitGrowth",
                        "earningsAnnouncementDate",
                        "earningsAnnouncementSource",
                        "exDividendDate",
                        "exDividendSource",
                    ):
                        row[field] = row_metric.get(field)
                    row.setdefault("sources", {})["edinet"] = {  # type: ignore[index]
                        "url": row_metric.get("url"),
                        "updatedAt": row_metric.get("asOf") or row_metric.get("submitDateTime"),
                        "status": "reused" if reused else "available",
                    }
                    if isinstance(row_metric.get("dividendYield"), (int, float)):
                        forecast_dividend = row_metric.get("dividendYieldKind") == "forecast"
                        dividend_reused = reused and str(row.get("code")) not in yahoo_company_data
                        row.setdefault("sources", {})["dividend"] = {  # type: ignore[index]
                            "url": YAHOO_INFO_URL if forecast_dividend else row_metric.get("url"),
                            "updatedAt": row_metric.get("dividendYieldAsOf") or row.get("priceAsOf"),
                            "status": "reused" if dividend_reused else "available",
                            "basis": "Yahoo Finance会社予想" if forecast_dividend else "EDINET実績DPS/株価",
                            "maxAgeDays": SOURCE_FRESHNESS_DAYS["dividend"] if forecast_dividend else SOURCE_FRESHNESS_DAYS["edinet"],
                        }

    previous_metrics = previous_dataset.get("edinetFundamentals")
    reusable_metrics = previous_metrics if isinstance(previous_metrics, list) else []
    raw_api_key = os.environ.get("EDINET_API_KEY", "")
    ascii_tokens = re.findall(r"[A-Za-z0-9_-]{20,}", raw_api_key)
    api_key = max(ascii_tokens, key=len) if ascii_tokens else ""
    if not api_key:
        if reusable_metrics:
            dataset["edinetFundamentals"] = reusable_metrics
            attach(reusable_metrics, reused=True)  # type: ignore[arg-type]
            source["status"] = "api-key-required"
            source["recordCount"] = len(reusable_metrics)
            source["perCount"] = _count_numeric(reusable_metrics, "per")
            source["pbrCount"] = _count_numeric(reusable_metrics, "pbr")
            source["roeCount"] = _count_numeric(reusable_metrics, "roe")
            source["checkedAt"] = generated_at
            source["errors"] = yahoo_company_errors[:10]
            source["reason"] = "EDINET_API_KEY未設定のため、前回取得済みのEDINET財務指標を再利用しました。"
        else:
            source["status"] = "api-key-required"
            source["checkedAt"] = generated_at
            source["errors"] = yahoo_company_errors[:10]
            source["reason"] = "EDINET API v2は無料登録のAPIキーが必要です。EDINET_API_KEYを設定するとPER/PBR/ROE等を算出します。"
        return

    max_downloads = max(1, int(os.environ.get("EDINET_MAX_DOWNLOADS", "225")))
    lookback_days = max(30, int(os.environ.get("EDINET_LOOKBACK_DAYS", "430")))
    ranked = sorted(
        [item for item in search_universe if isinstance(item, dict) and re.fullmatch(r"\d{4}", str(item.get("code", "")))],
        key=_total_score,
        reverse=True,
    )
    nikkei_codes = [
        str(item.get("code"))
        for item in dataset.get("nikkei225Components", [])
        if isinstance(item, dict) and re.fullmatch(r"\d{4}", str(item.get("code", "")))
    ]
    ranked_codes = [str(item.get("code")) for item in ranked]
    target_codes = list(dict.fromkeys([*nikkei_codes, *ranked_codes]))[:max_downloads]
    metrics: list[dict[str, object]] = []
    errors: list[str] = list(yahoo_company_errors)
    try:
        reports, report_errors = fetch_recent_securities_reports(set(target_codes), api_key, lookback_days=lookback_days)
        errors.extend(report_errors[:10])
    except EdinetError as error:
        source["status"] = "error"
        source["checkedAt"] = generated_at
        source["reason"] = str(error)
        return

    price_map = {str(item.get("code")): item for item in search_universe if isinstance(item, dict)}
    for code in target_codes:
        report = reports.get(code)
        if not report:
            continue
        try:
            zip_bytes, download_url = download_xbrl_zip(str(report["docID"]), api_key)
            parsed = parse_financial_metrics_from_xbrl(zip_bytes)
            parsed.update(yahoo_company_data.get(code, {}))
            latest_close = price_map.get(code, {}).get("latestClose")
            valuation = calculate_valuation_metrics(
                parsed,
                float(latest_close) if isinstance(latest_close, (int, float)) else None,
            )
            metrics.append({
                "code": code,
                "name": price_map.get(code, {}).get("name"),
                **valuation,
                "docID": report.get("docID"),
                "edinetCode": report.get("edinetCode"),
                "submitDateTime": report.get("submitDateTime"),
                "docDescription": report.get("docDescription"),
                "url": download_url,
                "provider": "EDINET API v2",
            })
        except (EdinetError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as error:
            errors.append(f"{code}: {error}")

    if metrics:
        dataset["edinetFundamentals"] = metrics
        attach(metrics)
    source["status"] = "available" if metrics else "partial"
    source["recordCount"] = len(metrics)
    source["targetCount"] = len(target_codes)
    source["targetPolicy"] = "nikkei225-first"
    source["perCount"] = _count_numeric(metrics, "per")
    source["pbrCount"] = _count_numeric(metrics, "pbr")
    source["roeCount"] = _count_numeric(metrics, "roe")
    source["checkedAt"] = generated_at
    source["errors"] = errors[:10]
    source["reason"] = (
        f"日経225を優先し、EDINET有価証券報告書XBRLから{len(metrics)}/{len(target_codes)}銘柄の財務指標を算出しました。"
        if metrics
        else "EDINET APIには接続できましたが、対象銘柄の財務指標を算出できませんでした。"
    )


def _attach_candidate_source_statuses(dataset: dict[str, object]) -> None:
    sources = dataset.get("sources") if isinstance(dataset.get("sources"), dict) else {}
    status_map = {
        "priceHistory": sources.get("priceHistory", {}),
        "marginRatio": sources.get("marginWeekly", {}),
        "theme": sources.get("themeNews", {}),
        "earnings": sources.get("fundamentals", {}),
    }
    rows = dataset.get("searchUniverse")
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_sources = row.get("sources")
        if not isinstance(row_sources, dict):
            continue
        for item_key, global_source in status_map.items():
            item_source = row_sources.get(item_key)
            if not isinstance(item_source, dict) or not isinstance(global_source, dict):
                continue
            item_source["status"] = global_source.get("status", "missing")
            if global_source.get("refreshStatus"):
                item_source["refreshStatus"] = global_source.get("refreshStatus")
            if global_source.get("refreshCheckedAt"):
                item_source["refreshCheckedAt"] = global_source.get("refreshCheckedAt")


def main() -> None:
    generated_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    previous_dataset: dict[str, object] = {}
    if OUTPUT.exists():
        try:
            loaded = json.loads(OUTPUT.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous_dataset = loaded
        except (OSError, json.JSONDecodeError):
            pass
    dataset: dict[str, object] = {
        **scoring_contract_metadata(),
        "generatedAt": generated_at,
        "nikkei225Components": [],
        "primeMarketComponents": [],
        "candidates": [],
        "sources": {
            "nikkei225": {
                "url": NIKKEI_URL,
                "status": "error",
                "checkedAt": generated_at,
            },
            "primeMarket": {
                "url": JPX_LIST_URL,
                "fileUrl": JPX_LIST_FILE_URL,
                "status": "error",
                "checkedAt": generated_at,
            },
            "marginWeekly": {
                "url": JPX_MARGIN_URL,
                "status": "error",
                "checkedAt": generated_at,
            },
            "priceHistory": {
                "url": PRICE_DOCS_URL,
                "provider": "J-Quants V2",
                "status": "not-checked",
                "checkedAt": generated_at,
            },
            "fundamentals": {
                "url": FINANCIAL_DOCS_URL,
                "provider": "J-Quants V2",
                "status": "not-checked",
                "checkedAt": generated_at,
            },
            "edinetFundamentals": {
                "url": EDINET_DOCUMENTS_URL,
                "provider": "EDINET API v2",
                "status": "not-checked",
                "checkedAt": generated_at,
                "reason": "EDINET_API_KEYが設定されている場合に有価証券報告書XBRLから財務指標を算出します。",
            },
            "themeNews": {
                "url": "https://jpx-jquants.com/ja",
                "status": "source-selection-required",
                "checkedAt": generated_at,
                "reason": "テーマ人気には公式の単一指標がないため、ニュース件数と検索関心の採用基準を確定する必要があります。",
            },
        },
        "notes": [
            "個別候補は、価格、信用倍率、テーマ、業績、リスク指標が検証できるまで出力しません。",
            "信用取引週末残高はJPX公式ページを確認しますが、銘柄別の倍率へ変換できるまで候補判定には使いません。",
        ],
        "qualityChecks": [],
    }

    try:
        nikkei_html = fetch_text(NIKKEI_URL)
        components = parse_nikkei_components(nikkei_html)
        source = dataset["sources"]["nikkei225"]  # type: ignore[index]
        source["status"] = "available" if len(components) == 225 else "invalid-count"
        source["updatedAt"] = extract_latest_date(nikkei_html)
        source["recordCount"] = len(components)
        if len(components) != 225:
            source["reason"] = "日経225構成銘柄数が225件ではありません。"
        dataset["nikkei225Components"] = components
    except (URLError, TimeoutError, OSError) as error:
        source = dataset["sources"]["nikkei225"]  # type: ignore[index]
        source["reason"] = str(error)
        previous_source = previous_dataset.get("sources", {}).get("nikkei225", {})  # type: ignore[union-attr]
        previous_components = previous_dataset.get("nikkei225Components")
        if (
            isinstance(previous_source, dict)
            and previous_source.get("status") == "available"
            and isinstance(previous_components, list)
            and len(previous_components) == 225
        ):
            dataset["sources"]["nikkei225"] = {
                **previous_source,
                "status": "stale-fallback",
                "previousStatus": previous_source.get("status"),
                "refreshStatus": "error",
                "refreshCheckedAt": generated_at,
                "refreshReason": str(error),
            }  # type: ignore[index]
            dataset["nikkei225Components"] = previous_components

    try:
        nikkei_codes = {
            str(item.get("code", ""))
            for item in dataset["nikkei225Components"]  # type: ignore[union-attr]
        }
        prime_components, prime_as_of = parse_prime_components(
            fetch_bytes(JPX_LIST_FILE_URL),
            nikkei_codes,
        )
        metadata_by_code = {str(item.get("code", "")): item for item in prime_components}
        enriched_components = [
            {
                **item,
                **metadata_by_code.get(str(item.get("code", "")), {}),
                "market": "日経225",
                "isNikkei225": True,
            }
            for item in dataset["nikkei225Components"]  # type: ignore[union-attr]
            if isinstance(item, dict)
        ]
        source = dataset["sources"]["primeMarket"]  # type: ignore[index]
        source["status"] = "available" if len(prime_components) == 225 else "partial"
        source["recordCount"] = len(prime_components)
        source["asOf"] = prime_as_of
        source["reason"] = f"JPX公式一覧で日経225の市場区分・業種を{len(prime_components)}/225銘柄確認しました。"
        dataset["nikkei225Components"] = enriched_components
        dataset["primeMarketComponents"] = enriched_components
    except (URLError, TimeoutError, OSError, ValueError, xlrd.XLRDError) as error:
        source = dataset["sources"]["primeMarket"]  # type: ignore[index]
        source["reason"] = str(error)
        previous_source = previous_dataset.get("sources", {}).get("primeMarket", {})  # type: ignore[union-attr]
        previous_components = previous_dataset.get("primeMarketComponents")
        if (
            isinstance(previous_source, dict)
            and previous_source.get("status") == "available"
            and isinstance(previous_components, list)
            and len(previous_components) == 225
        ):
            dataset["sources"]["primeMarket"] = {
                **previous_source,
                "status": "stale-fallback",
                "previousStatus": previous_source.get("status"),
                "refreshStatus": "error",
                "refreshCheckedAt": generated_at,
                "refreshReason": str(error),
            }  # type: ignore[index]
            dataset["primeMarketComponents"] = previous_components

    try:
        margin_html = fetch_text(JPX_MARGIN_URL)
        margin_index_html = fetch_text(JPX_MARGIN_INDEX_URL)
        file_links = parse_margin_file_links(margin_html)
        page_links = parse_margin_page_links(margin_index_html)
        pdf_inspection = inspect_latest_margin_pdf(file_links)
        margin_records = pdf_inspection.pop("records", [])
        nikkei_codes = {
            str(item["code"])
            for item in dataset["nikkei225Components"]  # type: ignore[union-attr]
        }
        nikkei_margin_records = [
            item for item in margin_records
            if str(item["code"]) in nikkei_codes
        ]
        nikkei_coverage = len(nikkei_margin_records) / max(1, len(nikkei_codes))
        source = dataset["sources"]["marginWeekly"]  # type: ignore[index]
        source["status"] = "available" if nikkei_coverage >= 0.95 else ("partial" if margin_records else "file-index-only")
        source["updatedAt"] = extract_latest_date(margin_html)
        source["fileCount"] = len(file_links)
        source["files"] = file_links[:12]
        source["relatedPages"] = page_links
        source["pdfInspection"] = pdf_inspection
        source["asOf"] = pdf_inspection.get("asOf")
        source["scope"] = "per-issue"
        source["hasPerIssueData"] = nikkei_coverage >= 0.95
        source["recordCount"] = len(margin_records)
        source["nikkei225MatchCount"] = len(nikkei_margin_records)
        source["primeMarketMatchCount"] = len(nikkei_margin_records)
        source["primeMarketCoverage"] = round(nikkei_coverage, 4)
        source["reason"] = (
            f"JPX公式PDFから日経225の{len(nikkei_margin_records)}/{len(nikkei_codes)}銘柄の売残・買残を確認しました。"
            if nikkei_coverage >= 0.95
            else f"JPX公式PDFを解析しましたが、日経225との照合は{len(nikkei_margin_records)}/{len(nikkei_codes)}件です。"
        )
        dataset["nikkei225Margin"] = nikkei_margin_records
        dataset["primeMarketMargin"] = nikkei_margin_records
    except (URLError, TimeoutError, OSError) as error:
        source = dataset["sources"]["marginWeekly"]  # type: ignore[index]
        source["reason"] = str(error)
        previous_source = previous_dataset.get("sources", {}).get("marginWeekly", {})  # type: ignore[union-attr]
        previous_margin = previous_dataset.get("nikkei225Margin")
        previous_prime_margin = previous_dataset.get("primeMarketMargin")
        if (
            isinstance(previous_source, dict)
            and previous_source.get("status") == "available"
            and isinstance(previous_margin, list)
            and len(previous_margin) == 225
        ):
            dataset["sources"]["marginWeekly"] = {
                **previous_source,
                "status": "stale-fallback",
                "previousStatus": previous_source.get("status"),
                "refreshStatus": "error",
                "refreshCheckedAt": generated_at,
                "refreshReason": str(error),
            }  # type: ignore[index]
            dataset["nikkei225Margin"] = previous_margin
            if isinstance(previous_prime_margin, list):
                dataset["primeMarketMargin"] = previous_prime_margin

    if not collect_jquants_metrics(dataset, generated_at):
        collect_free_market_metrics(dataset, generated_at, previous_dataset)
    dataset["nikkei225Breadth"] = build_nikkei225_breadth(
        dataset.get("nikkei225Prices"),
    )
    collect_tdnet_and_build_candidates(dataset, generated_at)
    collect_edinet_fundamentals(dataset, generated_at, previous_dataset)
    _attach_candidate_source_statuses(dataset)
    _attach_event_summaries(dataset, generated_at)
    _attach_scores(dataset)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    sources = dataset["sources"]  # type: ignore[assignment]
    nikkei_source = sources["nikkei225"]  # type: ignore[index]
    prime_source = sources["primeMarket"]  # type: ignore[index]
    margin_source = sources["marginWeekly"]  # type: ignore[index]
    price_source = sources["priceHistory"]  # type: ignore[index]
    theme_source = sources["themeNews"]  # type: ignore[index]
    financial_source = sources["fundamentals"]  # type: ignore[index]
    edinet_source = sources["edinetFundamentals"]  # type: ignore[index]
    ranked_rows = dataset.get("searchUniverse")
    quality_rows = [item for item in ranked_rows if isinstance(item, dict)] if isinstance(ranked_rows, list) else []
    low_quality_rows = [
        item for item in quality_rows
        if isinstance(item.get("dataQuality"), (int, float)) and float(item["dataQuality"]) < 70
    ]
    issue_rows = [
        item for item in quality_rows
        if isinstance(item.get("acquisitionIssues"), list) and item["acquisitionIssues"]
    ]
    anomaly_rows = [
        item for item in quality_rows
        if isinstance(item.get("dataAnomalies"), list) and item["dataAnomalies"]
    ]
    dataset["dataProviderPolicy"] = {
        "price": ["J-Quants", "Yahoo Finance chart", "Yahoo Finance mirror"],
        "fundamentals": ["EDINET API v2", "previous EDINET snapshot reuse", "TDnet disclosure signals"],
        "dividend": ["Yahoo Finance company forecast", "EDINET actual DPS"],
        "listing": ["JPX listed company file"],
        "margin": ["JPX weekly margin balance PDF"],
        "note": "無料・公式優先で複数取得元を使い、未取得や異常値は銘柄別の品質情報として表示します。",
    }
    dataset["acquisitionIssueSummary"] = {
        "issueCount": len(issue_rows),
        "anomalyCount": len(anomaly_rows),
        "examples": [
            {
                "code": item.get("code"),
                "name": item.get("name"),
                "issues": item.get("acquisitionIssues"),
            }
            for item in issue_rows[:20]
        ],
    }
    dataset["qualityChecks"] = [
        {
            "label": "日経225市場・業種情報",
            "status": prime_source["status"],
            "required": True,
            "message": prime_source.get("reason", "未確認"),
            "url": prime_source.get("url"),
        },
        {
            "label": "日経225採用銘柄",
            "status": nikkei_source["status"],
            "required": True,
            "message": "225銘柄を確認済み" if nikkei_source.get("status") == "available" else nikkei_source.get("reason", "未確認"),
        },
        {
            "label": "信用倍率",
            "status": margin_source["status"],
            "required": True,
            "message": margin_source.get("reason", "未確認"),
        },
        {
            "label": "価格履歴・52週日中高値",
            "status": price_source["status"],
            "required": True,
            "message": price_source.get("reason", "未確認"),
            "url": price_source.get("url"),
        },
        {
            "label": "テーマ人気・ニュース",
            "status": theme_source["status"],
            "required": True,
            "message": theme_source.get("reason", "未確認"),
            "url": theme_source.get("url"),
        },
        {
            "label": "業績・リスク",
            "status": financial_source["status"],
            "required": True,
            "message": financial_source.get("reason", "未確認"),
            "url": financial_source.get("url"),
        },
        {
            "label": "EDINET財務指標",
            "status": edinet_source["status"],
            "required": False,
            "message": edinet_source.get("reason", "未確認"),
            "url": edinet_source.get("url"),
        },
        {
            "label": "候補データ信頼度",
            "status": "available" if quality_rows and not low_quality_rows else ("partial" if quality_rows else "not-connected"),
            "required": True,
            "message": (
                f"信頼度70点未満は{len(low_quality_rows)}件です。欠損指標はスコアに控えめな減点として反映します。"
                if quality_rows
                else "候補データが生成されていません。"
            ),
        },
        {
            "label": "銘柄別取得失敗・異常値",
            "status": "available" if not issue_rows and not anomaly_rows else "partial",
            "required": False,
            "message": f"取得課題は{len(issue_rows)}件、異常値注意は{len(anomaly_rows)}件です。銘柄詳細のデータ品質で確認できます。",
        },
    ]
    score_history = update_score_history(dataset, generated_at)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(dataset, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUTPUT}")
    print(f"Wrote {SCORE_HISTORY_OUTPUT}")
    print(f"Score history snapshots: {len(score_history.get('snapshots', []))}")
    print(f"Nikkei 225 components: {len(dataset['nikkei225Components'])}")
    print(f"Screening universe: Nikkei 225 ({len(dataset['nikkei225Components'])})")
    print(f"Margin status: {dataset['sources']['marginWeekly']['status']}")  # type: ignore[index]
    print(f"Margin PDF: {dataset['sources']['marginWeekly'].get('pdfInspection', {}).get('status', 'not-checked')}")  # type: ignore[index]


if __name__ == "__main__":
    main()
