from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from market_analysis import (
    PRIVATE_CLOUD_DISTRIBUTION_MODE,
    PRIVATE_OUTPUT,
    PUBLIC_DISTRIBUTION_MODE,
    validate_analysis,
)


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SITE = ROOT / "outputs"
PRIVATE_SITE = ROOT / ".private-deploy"
AUTH_SOURCE = ROOT / "cloudflare" / "functions"
PRIVATE_MARKER = "private-deployment.json"


class PrivateDeploymentError(RuntimeError):
    pass


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PrivateDeploymentError(f"{label}を読み込めません: {error}") from error
    if not isinstance(payload, dict):
        raise PrivateDeploymentError(f"{label}がJSONオブジェクトではありません。")
    return payload


def validate_private_analysis(payload: object) -> list[str]:
    errors = validate_analysis(payload)
    if not isinstance(payload, dict):
        return errors
    if payload.get("distributionMode") != PRIVATE_CLOUD_DISTRIBUTION_MODE:
        errors.append("日経225分析がprivate-cloudモードではありません。")
    per = payload.get("per") if isinstance(payload.get("per"), dict) else {}
    reference = per.get("reference") if isinstance(per.get("reference"), dict) else {}
    bands = reference.get("bandLevels") if isinstance(reference.get("bandLevels"), dict) else {}
    if per.get("status") != "available" or not bands:
        errors.append("本人限定版に表示するPER整数倍ラインが利用可能ではありません。")
    return list(dict.fromkeys(errors))


def prepare_private_site(
    *,
    public_site: Path = PUBLIC_SITE,
    private_analysis: Path = PRIVATE_OUTPUT,
    destination: Path = PRIVATE_SITE,
    auth_source: Path = AUTH_SOURCE,
    generated_at: str | None = None,
) -> Path:
    public_analysis_path = public_site / "data" / "nikkei225-analysis.json"
    public_analysis = _load_json(public_analysis_path, "公開用日経225分析JSON")
    public_errors = validate_analysis(public_analysis, public_only=True)
    if public_analysis.get("distributionMode") != PUBLIC_DISTRIBUTION_MODE:
        public_errors.append("公開成果物に非公開用データが混入しています。")
    if public_errors:
        raise PrivateDeploymentError(" / ".join(dict.fromkeys(public_errors)))

    private_payload = _load_json(private_analysis, "本人限定用日経225分析JSON")
    private_errors = validate_private_analysis(private_payload)
    if private_errors:
        raise PrivateDeploymentError(" / ".join(private_errors))

    if not (public_site / "investment-candidate-app.html").is_file():
        raise PrivateDeploymentError("アプリHTMLが公開成果物にありません。")
    middleware = auth_source / "_middleware.js"
    if not middleware.is_file():
        raise PrivateDeploymentError("Cloudflare本人認証ミドルウェアがありません。")

    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(public_site, destination)
    shutil.copytree(auth_source, destination / "functions")
    target_analysis = destination / "data" / "nikkei225-analysis.json"
    target_analysis.write_text(
        json.dumps(private_payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    marker = {
        "schemaVersion": 1,
        "visibility": "private-auth-required",
        "accessProvider": "cloudflare-pages-password",
        "distributionMode": PRIVATE_CLOUD_DISTRIBUTION_MODE,
        "generatedAt": generated_at
        or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "restrictedDataPolicy": "confirmation-gated",
    }
    (destination / PRIVATE_MARKER).write_text(
        json.dumps(marker, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    (destination / "_headers").write_text(
        "/*\n"
        "  Cache-Control: private, no-store, max-age=0\n"
        "  X-Robots-Tag: noindex, nofollow, noarchive\n"
        "  Referrer-Policy: no-referrer\n",
        encoding="ascii",
    )
    (destination / "robots.txt").write_text(
        "User-agent: *\nDisallow: /\n",
        encoding="ascii",
    )
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cloudflare Pages合言葉認証版の配布フォルダーを生成"
    )
    parser.add_argument("--public-site", type=Path, default=PUBLIC_SITE)
    parser.add_argument("--private-analysis", type=Path, default=PRIVATE_OUTPUT)
    parser.add_argument("--destination", type=Path, default=PRIVATE_SITE)
    parser.add_argument("--auth-source", type=Path, default=AUTH_SOURCE)
    args = parser.parse_args(argv)
    try:
        destination = prepare_private_site(
            public_site=args.public_site,
            private_analysis=args.private_analysis,
            destination=args.destination,
            auth_source=args.auth_source,
        )
    except PrivateDeploymentError as error:
        print(f"ERROR: {error}")
        return 1
    print(f"OK: Cloudflare本人限定版を生成しました: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
