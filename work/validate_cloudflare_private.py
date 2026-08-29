from __future__ import annotations

import argparse
import json
from pathlib import Path

from market_analysis import PRIVATE_CLOUD_DISTRIBUTION_MODE, validate_analysis
from prepare_cloudflare_private import (
    PRIVATE_MARKER,
    PRIVATE_SITE,
    PUBLIC_SITE,
    validate_private_analysis,
)


def _json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_private_site(
    site: Path = PRIVATE_SITE,
    *,
    public_site: Path = PUBLIC_SITE,
) -> list[str]:
    errors: list[str] = []
    try:
        marker = _json(site / PRIVATE_MARKER)
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"本人限定配布マーカーを読み込めません: {error}")
        marker = {}
    if not isinstance(marker, dict) or (
        marker.get("visibility") != "private-auth-required"
        or marker.get("accessProvider") != "cloudflare-pages-password"
        or marker.get("distributionMode") != PRIVATE_CLOUD_DISTRIBUTION_MODE
    ):
        errors.append("本人限定配布マーカーが不正です。")

    try:
        private_analysis = _json(site / "data" / "nikkei225-analysis.json")
        errors.extend(validate_private_analysis(private_analysis))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"本人限定用日経225分析JSONを読み込めません: {error}")

    try:
        public_analysis = _json(public_site / "data" / "nikkei225-analysis.json")
        errors.extend(
            f"公開成果物: {error}"
            for error in validate_analysis(public_analysis, public_only=True)
        )
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"公開用日経225分析JSONを読み込めません: {error}")

    headers = site / "_headers"
    robots = site / "robots.txt"
    try:
        header_text = headers.read_text(encoding="ascii")
    except OSError:
        header_text = ""
    if "Cache-Control: private, no-store" not in header_text or "X-Robots-Tag: noindex" not in header_text:
        errors.append("本人限定版のキャッシュ・検索除外ヘッダーがありません。")
    try:
        robots_text = robots.read_text(encoding="ascii")
    except OSError:
        robots_text = ""
    if "Disallow: /" not in robots_text:
        errors.append("本人限定版のrobots検索除外設定がありません。")
    if not (site / "investment-candidate-app.html").is_file():
        errors.append("本人限定版のアプリHTMLがありません。")
    middleware = site / "functions" / "_middleware.js"
    try:
        middleware_text = middleware.read_text(encoding="utf-8")
    except OSError:
        middleware_text = ""
    for required in (
        "PRIVATE_APP_PASSWORD",
        "PRIVATE_SESSION_SECRET",
        "PRIVATE_SERVICE_TOKEN",
        "HttpOnly; Secure; SameSite=Strict",
        "crypto.subtle",
        "X-Capital-Radar-Service",
    ):
        if required not in middleware_text:
            errors.append(f"本人認証ミドルウェアの必須要素がありません: {required}")
    return list(dict.fromkeys(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cloudflare本人限定版の漏えい防止検査")
    parser.add_argument("--site", type=Path, default=PRIVATE_SITE)
    parser.add_argument("--public-site", type=Path, default=PUBLIC_SITE)
    args = parser.parse_args(argv)
    errors = validate_private_site(args.site, public_site=args.public_site)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("OK: Cloudflare本人限定版と公開版の分離チェックに合格しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
