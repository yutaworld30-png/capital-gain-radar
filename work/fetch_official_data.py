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
    fetch_yahoo_history,
    fetch_yahoo_spark_histories,
    YAHOO_SPARK_URL,
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


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs" / "data" / "latest-candidates.json"
PDF_INSPECTION_DIR = ROOT / "work" / "tmp" / "pdfs"
NIKKEI_URL = "https://indexes.nikkei.co.jp/en/nkave/index/component?idx=nk225"
JPX_LIST_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
JPX_LIST_FILE_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
JPX_MARGIN_URL = "https://www.jpx.co.jp/markets/statistics-equities/margin/05.html"
JPX_MARGIN_INDEX_URL = "https://www.jpx.co.jp/markets/statistics-equities/margin/index.html"


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
        if re.fullmatch(r"[0-9A-Z]{4}", code) and name:
            industry = ""
            for header_name in ("33業種区分", "17業種区分", "規模区分"):
                if header_name in headers:
                    industry = str(row[headers.index(header_name)]).strip()
                    if industry and industry != "-":
                        break
            components.append({
                "code": code,
                "name": name,
                "market": "東証プライム",
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
    components = dataset.get("primeMarketComponents")
    if not isinstance(components, list) or len(components) < 1000:
        price_source["status"] = "blocked"
        price_source["reason"] = "東証プライム上場銘柄の検証が完了していません。"
        return

    today = date.today()
    start = today - timedelta(days=760)
    validated: list[dict[str, object]] = []
    errors: list[str] = []
    primary_histories: dict[str, dict[str, object]] = {}
    mirror_histories: dict[str, dict[str, object]] = {}
    codes = [str(component.get("code", "")) for component in components]

    def collect_history_batch(
        batch: list[str],
        target: dict[str, dict[str, object]],
        mirror: bool = False,
    ) -> None:
        try:
            target.update(fetch_yahoo_spark_histories(batch, YAHOO_SPARK_MIRROR_URL if mirror else YAHOO_SPARK_URL))
        except FreeMarketDataError as error:
            if len(batch) > 1:
                midpoint = len(batch) // 2
                collect_history_batch(batch[:midpoint], target, mirror)
                collect_history_batch(batch[midpoint:], target, mirror)
            else:
                errors.append(f"{batch[0]}: 価格履歴を取得できません（{error}）")

    for offset in range(0, len(codes), 20):
        batch = codes[offset:offset + 20]
        collect_history_batch(batch, primary_histories)
        collect_history_batch(batch, mirror_histories, True)
        time.sleep(0.15)

    component_map = {str(component.get("code", "")): component for component in components}
    for code in codes:
        component = component_map.get(code, {})
        name = str(component.get("name", ""))
        primary = primary_histories.get(code)
        mirror = mirror_histories.get(code)
        if not primary or not mirror:
            errors.append(f"{code}: 価格履歴または照合履歴を取得できません。")
            continue
        primary_rows = primary.get("rows")
        mirror_rows = mirror.get("rows")
        if not isinstance(primary_rows, list) or not isinstance(mirror_rows, list):
            errors.append(f"{code}: 価格履歴の形式を確認できません。")
            continue
        if len(primary_rows) < 120 or len(mirror_rows) < 120:
            errors.append(f"{code}: 価格履歴が120営業日未満です。")
            continue
        mirror_by_date = {str(item["Date"]): item for item in mirror_rows if isinstance(item, dict) and item.get("Date")}
        common_dates = [
            str(item["Date"]) for item in reversed(primary_rows)
            if isinstance(item, dict) and str(item.get("Date")) in mirror_by_date
        ]
        if not common_dates:
            errors.append(f"{code}: 2つの配信経路で共通する取引日がありません。")
            continue
        validation_date = common_dates[0]
        primary_match = next(item for item in reversed(primary_rows) if item["Date"] == validation_date)
        mirror_match = mirror_by_date[validation_date]
        primary_close = float(primary_match["C"])
        mirror_close = float(mirror_match["C"])
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
            "isNikkei225": bool(component.get("isNikkei225")),
            **metrics,
            "chartHistory": [
                {
                    "date": str(row.get("Date")),
                    "close": round(float(row.get("C") or 0), 4),
                }
                for row in primary_rows[-260:]
                if isinstance(row, dict) and row.get("Date") and row.get("C")
            ],
            "validationDate": validation_date,
            "validationCloseYahoo": primary_close,
            "validationCloseMirror": mirror_close,
            "validationDifference": round(close_difference, 6),
            "priceBasis": "close",
            "sources": {
                "priceHistory": {
                    "url": str(primary.get("url") or YAHOO_INFO_URL),
                    "updatedAt": str(metrics["asOf"]),
                    "provider": "Yahoo Finance spark",
                },
                "priceValidation": {
                    "url": str(mirror.get("url") or YAHOO_SPARK_MIRROR_URL),
                    "updatedAt": validation_date,
                    "provider": "Yahoo Finance mirror spark",
                },
            },
        })

    validated.sort(key=lambda item: str(item["code"]))
    nikkei_codes = {
        str(item.get("code", ""))
        for item in dataset.get("nikkei225Components", [])
        if isinstance(item, dict)
    }
    dataset["primeMarketPrices"] = validated
    dataset["nikkei225Prices"] = [item for item in validated if str(item["code"]) in nikkei_codes]
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
        f"Yahoo Financeの2配信経路で終値を照合し、東証プライム{len(validated)}/{len(components)}銘柄の価格履歴を確認しました。"
        if coverage >= 0.95
        else f"2つの無料経路で検証できた価格は{len(validated)}/{len(components)}件です。未検証銘柄は候補から除外します。"
    )

    financial_source["provider"] = "TDnet"
    financial_source["url"] = "https://www.release.tdnet.info/inbs/I_main_00.html"
    financial_source["status"] = "not-connected"
    financial_source["reason"] = "TDnetの決算・業績修正開示を使う無料判定へ切替中です。"


def augment_candidate_chart_histories(dataset: dict[str, object], generated_at: str) -> None:
    candidates = dataset.get("candidates")
    prices = dataset.get("primeMarketPrices")
    if not isinstance(candidates, list) or not isinstance(prices, list):
        return

    candidate_codes = sorted({
        str(item.get("code", ""))
        for item in candidates
        if isinstance(item, dict) and item.get("code")
    })
    if not candidate_codes:
        return

    price_map = {
        str(item.get("code", "")): item
        for item in prices
        if isinstance(item, dict) and item.get("code")
    }
    today = date.today()
    start = today - timedelta(days=760)
    detailed_count = 0
    errors: list[str] = []

    for code in candidate_codes:
        target = price_map.get(code)
        if not target:
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
    components = dataset.get("primeMarketComponents")
    prices = dataset.get("primeMarketPrices")
    margins = dataset.get("primeMarketMargin")
    if not isinstance(components, list) or not isinstance(prices, list) or not isinstance(margins, list):
        theme_source["status"] = "blocked"
        theme_source["reason"] = "東証プライム・価格・信用倍率の検証完了を待っています。"
        financial_source["status"] = "blocked"
        financial_source["reason"] = "東証プライム・価格・信用倍率の検証完了を待っています。"
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

    return_values = sorted(
        (
            (code, float(item.get("return20", 0)))
            for code, item in price_map.items()
        ),
        key=lambda pair: pair[1],
    )
    relative_map = {
        code: round(index / max(1, len(return_values) - 1) * 100)
        for index, (code, _) in enumerate(return_values)
    }
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
        latest_disclosure = disclosures_by_code.get(code, [])[-1] if disclosures_by_code.get(code) else None
        theme_score = max((theme_score_map.get(theme, 0) for theme in assigned_themes), default=0)
        primary_theme = max(
            assigned_themes,
            key=lambda theme: theme_score_map.get(theme, 0),
        ) if assigned_themes else ""
        primary_theme_detail = theme_detail_map.get(primary_theme, {})
        price_sources = price.get("sources", {})
        candidate_name = str(margin.get("name") or price.get("name") or code)
        component = component_map.get(code, {})
        search_universe.append({
            "code": code,
            "name": candidate_name,
            "market": "東証プライム",
            "industry": str(component.get("industry") or "業種未分類"),
            "isNikkei225": code in nikkei_codes,
            "themes": assigned_themes or ["テーマ未分類"],
            "theme": theme_score,
            "margin": round(float(margin_ratio), 2),
            "monthsFromHigh": float(price.get("monthsFromHigh", 0)),
            "technical": int(price.get("technical", 0)),
            "liquidity": int(price.get("liquidity", 0)),
            "relative": int(relative_map.get(code, 0)),
            "earnings": int(financial.get("earnings", 50)),
            "risk": int(price.get("risk", 100)),
            "latestClose": price.get("latestClose"),
            "priceAsOf": price.get("asOf"),
            "supplyMemo": (
                f"売残 {int(margin.get('outstandingSales', 0)):,}株 / "
                f"買残 {int(margin.get('outstandingPurchases', 0)):,}株"
            ),
            "signals": financial.get("signals", []),
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
    dataset["candidates"] = [
        item for item in search_universe
        if str(item["code"]) in theme_map
    ]
    augment_candidate_chart_histories(dataset, generated_at)

    theme_source["url"] = TDNET_MAIN_URL
    theme_source["provider"] = "Google News RSS + TDnet"
    theme_source["status"] = "available" if themes else "partial"
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
        "TDnet直近31日の業績修正・増配・減配等を東証プライム全体で確認しました。"
        if len(financials) == len(codes)
        else f"TDnet業績判定は{len(financials)}/{len(codes)}件です。"
    )


def _supply_score(item: dict[str, object]) -> int:
    margin = float(item.get("margin") or 99)
    months = float(item.get("monthsFromHigh") or 0)
    ratio_score = max(0, 100 - (margin - 0.5) * 16)
    high_score = min(100, months / 12 * 100)
    return round(ratio_score * 0.6 + high_score * 0.4)


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
    per_score = 50 if per is None or per <= 0 else _range_score(per, 8, 35, lower_is_better=True)
    pbr_score = 50 if pbr is None or pbr <= 0 else _range_score(pbr, 0.8, 5, lower_is_better=True)
    roe_score = _range_score(roe, 0.0, 0.18, lower_is_better=False)
    return round(per_score * 0.35 + pbr_score * 0.25 + roe_score * 0.4)


def _total_score(item: dict[str, object]) -> int:
    liquidity = int(item.get("liquidity") or 60)
    relative = int(item.get("relative") or 60)
    earnings = int(item.get("earnings") or 50)
    risk = int(item.get("risk") or 50)
    valuation = int(item.get("valuation") or _valuation_score(item))
    return round(
        int(item.get("theme") or 0) * 0.20
        + _supply_score(item) * 0.20
        + int(item.get("technical") or 0) * 0.15
        + relative * 0.13
        + earnings * 0.13
        + liquidity * 0.07
        + valuation * 0.08
        + (100 - risk) * 0.04
    )


def _count_numeric(metrics: list[dict[str, object]], key: str) -> int:
    return sum(1 for item in metrics if isinstance(item.get(key), (int, float)))


def _attach_scores(dataset: dict[str, object]) -> None:
    for key in ("searchUniverse", "candidates"):
        rows = dataset.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                row["supply"] = _supply_score(row)
                row["valuation"] = _valuation_score(row)
                row["score"] = _total_score(row)


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

    def attach(metrics: list[dict[str, object]]) -> None:
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
                    row["fundamentals"] = metric
                    for field in ("per", "pbr", "roe", "marketCap", "dividendYield", "equityRatio"):
                        row[field] = metric.get(field)
                    row.setdefault("sources", {})["edinet"] = {  # type: ignore[index]
                        "url": metric.get("url"),
                        "updatedAt": metric.get("asOf") or metric.get("submitDateTime"),
                    }

    previous_metrics = previous_dataset.get("edinetFundamentals")
    reusable_metrics = previous_metrics if isinstance(previous_metrics, list) else []
    raw_api_key = os.environ.get("EDINET_API_KEY", "")
    ascii_tokens = re.findall(r"[A-Za-z0-9_-]{20,}", raw_api_key)
    api_key = max(ascii_tokens, key=len) if ascii_tokens else ""
    if not api_key:
        if reusable_metrics:
            dataset["edinetFundamentals"] = reusable_metrics
            attach(reusable_metrics)  # type: ignore[arg-type]
            source["status"] = "api-key-required"
            source["recordCount"] = len(reusable_metrics)
            source["perCount"] = _count_numeric(reusable_metrics, "per")
            source["pbrCount"] = _count_numeric(reusable_metrics, "pbr")
            source["roeCount"] = _count_numeric(reusable_metrics, "roe")
            source["checkedAt"] = generated_at
            source["reason"] = "EDINET_API_KEY未設定のため、前回取得済みのEDINET財務指標を再利用しました。"
        else:
            source["status"] = "api-key-required"
            source["checkedAt"] = generated_at
            source["reason"] = "EDINET API v2は無料登録のAPIキーが必要です。EDINET_API_KEYを設定するとPER/PBR/ROE等を算出します。"
        return

    max_downloads = max(1, int(os.environ.get("EDINET_MAX_DOWNLOADS", "120")))
    lookback_days = max(30, int(os.environ.get("EDINET_LOOKBACK_DAYS", "430")))
    ranked = sorted(
        [item for item in search_universe if isinstance(item, dict) and re.fullmatch(r"\d{4}", str(item.get("code", "")))],
        key=_total_score,
        reverse=True,
    )
    target_codes = {str(item.get("code")) for item in ranked[:max_downloads]}
    metrics: list[dict[str, object]] = []
    errors: list[str] = []
    try:
        reports, report_errors = fetch_recent_securities_reports(target_codes, api_key, lookback_days=lookback_days)
        errors.extend(report_errors[:10])
    except EdinetError as error:
        source["status"] = "error"
        source["checkedAt"] = generated_at
        source["reason"] = str(error)
        return

    price_map = {str(item.get("code")): item for item in search_universe if isinstance(item, dict)}
    for code in [str(item.get("code")) for item in ranked[:max_downloads]]:
        report = reports.get(code)
        if not report:
            continue
        try:
            zip_bytes, download_url = download_xbrl_zip(str(report["docID"]), api_key)
            parsed = parse_financial_metrics_from_xbrl(zip_bytes)
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
    source["perCount"] = _count_numeric(metrics, "per")
    source["pbrCount"] = _count_numeric(metrics, "pbr")
    source["roeCount"] = _count_numeric(metrics, "roe")
    source["checkedAt"] = generated_at
    source["errors"] = errors[:10]
    source["reason"] = (
        f"EDINET有価証券報告書XBRLから{len(metrics)}/{len(target_codes)}銘柄の財務指標を算出しました。"
        if metrics
        else "EDINET APIには接続できましたが、対象銘柄の財務指標を算出できませんでした。"
    )


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
        "schemaVersion": 1,
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
        source = dataset["sources"]["primeMarket"]  # type: ignore[index]
        source["status"] = "available" if len(prime_components) >= 1000 else "invalid-count"
        source["recordCount"] = len(prime_components)
        source["asOf"] = prime_as_of
        source["reason"] = f"JPX公式一覧から東証プライム{len(prime_components)}銘柄を確認しました。"
        dataset["primeMarketComponents"] = prime_components
    except (URLError, TimeoutError, OSError, ValueError, xlrd.XLRDError) as error:
        source = dataset["sources"]["primeMarket"]  # type: ignore[index]
        source["reason"] = str(error)
        previous_source = previous_dataset.get("sources", {}).get("primeMarket", {})  # type: ignore[union-attr]
        previous_components = previous_dataset.get("primeMarketComponents")
        if (
            isinstance(previous_source, dict)
            and previous_source.get("status") == "available"
            and isinstance(previous_components, list)
            and len(previous_components) >= 1000
        ):
            dataset["sources"]["primeMarket"] = {
                **previous_source,
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
        prime_codes = {
            str(item["code"])
            for item in dataset["primeMarketComponents"]  # type: ignore[union-attr]
        }
        prime_margin_records = [
            item for item in margin_records
            if str(item["code"]) in prime_codes
        ]
        prime_coverage = len(prime_margin_records) / max(1, len(prime_codes))
        source = dataset["sources"]["marginWeekly"]  # type: ignore[index]
        source["status"] = "available" if prime_coverage >= 0.95 else ("partial" if margin_records else "file-index-only")
        source["updatedAt"] = extract_latest_date(margin_html)
        source["fileCount"] = len(file_links)
        source["files"] = file_links[:12]
        source["relatedPages"] = page_links
        source["pdfInspection"] = pdf_inspection
        source["asOf"] = pdf_inspection.get("asOf")
        source["scope"] = "per-issue"
        source["hasPerIssueData"] = prime_coverage >= 0.95
        source["recordCount"] = len(margin_records)
        source["nikkei225MatchCount"] = len(nikkei_margin_records)
        source["primeMarketMatchCount"] = len(prime_margin_records)
        source["primeMarketCoverage"] = round(prime_coverage, 4)
        source["reason"] = (
            f"JPX公式PDFから東証プライム{len(prime_margin_records)}/{len(prime_codes)}銘柄の売残・買残を確認しました。"
            if prime_coverage >= 0.95
            else f"JPX公式PDFを解析しましたが、東証プライムとの照合は{len(prime_margin_records)}/{len(prime_codes)}件です。"
        )
        dataset["nikkei225Margin"] = nikkei_margin_records
        dataset["primeMarketMargin"] = prime_margin_records
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
                "refreshStatus": "error",
                "refreshCheckedAt": generated_at,
                "refreshReason": str(error),
            }  # type: ignore[index]
            dataset["nikkei225Margin"] = previous_margin
            if isinstance(previous_prime_margin, list):
                dataset["primeMarketMargin"] = previous_prime_margin

    if not collect_jquants_metrics(dataset, generated_at):
        collect_free_market_metrics(dataset, generated_at, previous_dataset)
    collect_tdnet_and_build_candidates(dataset, generated_at)
    collect_edinet_fundamentals(dataset, generated_at, previous_dataset)
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
    dataset["qualityChecks"] = [
        {
            "label": "東証プライム上場銘柄",
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
            "label": "価格履歴・終値高値",
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
    ]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(dataset, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUTPUT}")
    print(f"Nikkei 225 components: {len(dataset['nikkei225Components'])}")
    print(f"Prime Market components: {len(dataset['primeMarketComponents'])}")
    print(f"Margin status: {dataset['sources']['marginWeekly']['status']}")  # type: ignore[index]
    print(f"Margin PDF: {dataset['sources']['marginWeekly'].get('pdfInspection', {}).get('status', 'not-checked')}")  # type: ignore[index]


if __name__ == "__main__":
    main()
