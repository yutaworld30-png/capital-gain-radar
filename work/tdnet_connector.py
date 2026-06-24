from __future__ import annotations

import re
from datetime import date, timedelta
from html.parser import HTMLParser
from xml.etree import ElementTree
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urljoin
from urllib.request import Request, urlopen


TDNET_BASE_URL = "https://www.release.tdnet.info/inbs/"
TDNET_MAIN_URL = urljoin(TDNET_BASE_URL, "I_main_00.html")

THEMES: dict[str, tuple[str, ...]] = {
    "AI・生成AI": ("生成AI", "人工知能", "AI ", "AI・", "LLM", "大規模言語モデル"),
    "半導体": ("半導体", "HBM", "DRAM", "NAND", "シリコンウエハ", "テスター"),
    "データセンター": ("データセンター", "クラウド基盤", "光通信", "光ファイバ"),
    "防衛・宇宙": ("防衛", "宇宙", "衛星", "ロケット", "航空機"),
    "ロボティクス・自動化": ("ロボット", "ロボティクス", "FA ", "自動化", "省人化"),
    "GX・電力": ("脱炭素", "再生可能エネルギー", "蓄電池", "水素", "原子力", "GX"),
    "インバウンド": ("インバウンド", "訪日", "観光需要", "免税"),
}

THEME_NEWS_QUERIES: dict[str, str] = {
    "AI・生成AI": '"生成AI" OR "人工知能"',
    "半導体": "半導体",
    "データセンター": '"データセンター" OR "光通信"',
    "防衛・宇宙": "防衛 OR 宇宙 OR 衛星",
    "ロボティクス・自動化": "ロボット OR 自動化 OR 省人化",
    "GX・電力": "脱炭素 OR 蓄電池 OR 原子力 OR 水素",
    "インバウンド": "インバウンド OR 訪日 OR 観光",
}

THEME_CODE_MAP: dict[str, set[str]] = {
    "AI・生成AI": {"9984", "9432", "6758", "6098", "9613", "6701", "6702", "6501", "6857"},
    "半導体": {"285A", "4062", "6526", "6723", "6762", "6857", "6920", "8035", "3436", "5803"},
    "データセンター": {"5803", "6501", "6503", "6701", "6702", "9432", "9433", "9613"},
    "防衛・宇宙": {"6503", "6701", "6702", "7011", "7012", "7013"},
    "ロボティクス・自動化": {"6273", "6301", "6326", "6506", "6645", "6954"},
    "GX・電力": {"3407", "4005", "4188", "6501", "6503", "7011", "9501", "9502", "9503"},
    "インバウンド": {"3099", "4661", "8233", "9020", "9021", "9022", "9201", "9202", "9602"},
}

POSITIVE_SIGNALS: dict[str, int] = {
    "上方修正": 25,
    "増配": 15,
    "最高益": 15,
    "自己株式の取得": 8,
    "自己株式取得": 8,
}

NEGATIVE_SIGNALS: dict[str, int] = {
    "下方修正": -30,
    "減配": -20,
    "赤字": -20,
    "特別損失": -12,
    "業績予想の撤回": -20,
}


class TDnetError(RuntimeError):
    pass


class TDnetTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_row = False
        self.in_cell = False
        self.cell_text = ""
        self.cell_href = ""
        self.current_row: list[dict[str, str]] = []
        self.rows: list[list[dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self.in_row = True
            self.current_row = []
        elif tag == "td" and self.in_row:
            self.in_cell = True
            self.cell_text = ""
            self.cell_href = ""
        elif tag == "a" and self.in_cell:
            href = dict(attrs).get("href")
            if href:
                self.cell_href = href

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell_text += data

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "td" and self.in_cell:
            self.current_row.append({
                "text": re.sub(r"\s+", " ", self.cell_text).strip(),
                "href": self.cell_href,
            })
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = []
            self.in_row = False


def _fetch_text(url: str) -> str:
    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 CapitalGainRadar/0.3"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            data = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
        return data.decode(charset, errors="replace")
    except HTTPError as error:
        if error.code == 404:
            return ""
        raise TDnetError(f"TDnet HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise TDnetError("TDnetの取得に失敗しました。") from error


def parse_disclosures(html: str, disclosure_date: date) -> list[dict[str, str]]:
    parser = TDnetTableParser()
    parser.feed(html)
    disclosures: list[dict[str, str]] = []
    for row in parser.rows:
        texts = [cell["text"] for cell in row]
        code_index = next(
            (index for index, text in enumerate(texts) if re.fullmatch(r"[0-9A-Z]{5}", text)),
            None,
        )
        if code_index is None or code_index + 2 >= len(row):
            continue
        time_text = texts[code_index - 1] if code_index > 0 else ""
        if not re.fullmatch(r"\d{2}:\d{2}", time_text):
            continue
        raw_code = texts[code_index]
        title_cell = row[code_index + 2]
        if not title_cell["text"]:
            continue
        code = raw_code[:-1] if raw_code.endswith("0") else raw_code
        disclosures.append({
            "date": disclosure_date.isoformat(),
            "time": time_text,
            "code": code,
            "company": texts[code_index + 1],
            "title": title_cell["text"],
            "url": urljoin(TDNET_BASE_URL, title_cell["href"]),
        })
    return disclosures


def fetch_recent_disclosures(
    codes: set[str],
    days: int = 31,
    max_pages: int = 5,
) -> tuple[list[dict[str, str]], list[str]]:
    today = date.today()
    disclosures: list[dict[str, str]] = []
    errors: list[str] = []
    for offset in range(days):
        target = today - timedelta(days=offset)
        date_text = target.strftime("%Y%m%d")
        for page in range(1, max_pages + 1):
            url = urljoin(TDNET_BASE_URL, f"I_list_{page:03d}_{date_text}.html")
            try:
                html = _fetch_text(url)
            except TDnetError as error:
                errors.append(f"{target.isoformat()}: {error}")
                break
            if not html:
                break
            page_rows = parse_disclosures(html, target)
            if not page_rows:
                break
            disclosures.extend(item for item in page_rows if item["code"] in codes)
            if len(page_rows) < 100:
                break
    unique = {
        (item["date"], item["time"], item["code"], item["title"]): item
        for item in disclosures
    }
    return sorted(unique.values(), key=lambda item: (item["date"], item["time"])), errors


def fetch_theme_news_counts() -> tuple[dict[str, dict[str, object]], list[str]]:
    results: dict[str, dict[str, object]] = {}
    errors: list[str] = []
    for theme, query in THEME_NEWS_QUERIES.items():
        url = (
            "https://news.google.com/rss/search?q="
            + quote_plus(query + " when:7d")
            + "&hl=ja&gl=JP&ceid=JP:ja"
        )
        try:
            xml = _fetch_text(url)
            root = ElementTree.fromstring(xml)
            titles = {
                (item.findtext("title") or "").strip()
                for item in root.findall("./channel/item")
                if (item.findtext("title") or "").strip()
            }
            results[theme] = {
                "articleCount": len(titles),
                "url": url,
                "asOf": date.today().isoformat(),
            }
        except (TDnetError, ElementTree.ParseError) as error:
            errors.append(f"{theme}: {error}")
            results[theme] = {
                "articleCount": 0,
                "url": url,
                "asOf": date.today().isoformat(),
            }
    return results, errors


def analyze_disclosures(
    codes: set[str],
    disclosures: list[dict[str, str]],
    price_metrics: dict[str, dict[str, object]],
    news_counts: dict[str, dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, list[str]]]:
    by_code: dict[str, list[dict[str, str]]] = {code: [] for code in codes}
    code_themes: dict[str, set[str]] = {code: set() for code in codes}
    theme_events: dict[str, list[dict[str, str]]] = {theme: [] for theme in THEMES}

    for disclosure in disclosures:
        code = disclosure["code"]
        by_code.setdefault(code, []).append(disclosure)
        title_upper = disclosure["title"].upper()
        for theme, keywords in THEMES.items():
            if any(keyword.upper() in title_upper for keyword in keywords):
                code_themes.setdefault(code, set()).add(theme)
                theme_events[theme].append(disclosure)

    financials: list[dict[str, object]] = []
    for code in sorted(codes):
        events = by_code.get(code, [])
        score = 55
        signals: list[str] = []
        for event in events:
            title = event["title"]
            for keyword, value in POSITIVE_SIGNALS.items():
                if keyword in title:
                    score += value
                    signals.append(keyword)
            for keyword, value in NEGATIVE_SIGNALS.items():
                if keyword in title:
                    score += value
                    signals.append(keyword)
        score = max(0, min(100, score))
        latest = events[-1] if events else None
        financials.append({
            "code": code,
            "earnings": score,
            "signals": sorted(set(signals)),
            "asOf": latest["date"] if latest else None,
            "latestDisclosure": latest,
        })

    themes: list[dict[str, object]] = []
    for theme, events in theme_events.items():
        unique_codes = sorted(
            ({event["code"] for event in events} | THEME_CODE_MAP.get(theme, set()))
            & codes
        )
        article_count = int(news_counts.get(theme, {}).get("articleCount", 0))
        if not events and article_count == 0:
            continue
        momentum_values = [
            float(price_metrics[code].get("return20", 0))
            for code in unique_codes
            if code in price_metrics
        ]
        positive_share = (
            sum(value > 0 for value in momentum_values) / len(momentum_values)
            if momentum_values else 0
        )
        popularity = min(
            95,
            40
            + min(30, article_count)
            + min(5, len(events) * 2)
            + round(20 * positive_share),
        )
        themes.append({
            "name": theme,
            "score": popularity,
            "newsArticleCount": article_count,
            "disclosureCount": len(events),
            "companyCount": len(unique_codes),
            "positiveMomentumShare": round(positive_share, 3),
            "asOf": (
                max(event["date"] for event in events)
                if events
                else news_counts.get(theme, {}).get("asOf")
            ),
            "newsUrl": news_counts.get(theme, {}).get("url"),
        })
        for code in unique_codes:
            code_themes.setdefault(code, set()).add(theme)

    theme_map = {
        code: sorted(values)
        for code, values in code_themes.items()
        if values
    }
    themes.sort(key=lambda item: (-int(item["score"]), str(item["name"])))
    return financials, themes, theme_map
