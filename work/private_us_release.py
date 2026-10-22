from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from prepare_cloudflare_private import PRIVATE_SITE, prepare_private_site
from validate_cloudflare_private import validate_private_site


ROOT = Path(__file__).resolve().parents[1]
DATA_FILES = (
    "backtest-summary.json", "fundamentals-state-v1.json", "industry-market.json",
    "latest-candidates.json", "margin-history-v1.json", "market-environment-history.json",
    "market-environment.json", "nikkei225-analysis.json", "score-history-v2.json",
    "score-history.json", "weekly-accuracy-summary-v1.json", "weekly-predictions-v1.json",
)
SHARD_PATTERN = re.compile(r"data/search-universe/part-\d{3}\.json\Z")
MISSING_ASSET_FALLBACK = b'<meta http-equiv="refresh" content="0; url=./investment-candidate-app.html">'


def private_base(url: str) -> str:
    parsed = urlparse(url.rstrip("/"))
    if (parsed.scheme != "https" or parsed.hostname != "capital-gain-radar-private.pages.dev"
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise ValueError("本人限定版のURLが想定したCloudflare Pagesのホストではありません。")
    return f"https://{parsed.hostname}"


def remote_json(base: str, relative: str, *, optional: bool = False) -> tuple[bytes, dict] | None:
    token = os.environ.get("PRIVATE_SERVICE_TOKEN", "").strip()
    if not token:
        raise ValueError("PRIVATE_SERVICE_TOKENが未設定です。")
    request = Request(f"{base}/{relative}", headers={
        "Accept": "application/json", "X-Capital-Radar-Service": token,
        "User-Agent": "capital-gain-radar-us-release/1.0",
    })
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read()
    except HTTPError as error:
        if optional and error.code == 404:
            return None
        raise RuntimeError(f"本人限定版の{relative}を取得できません: HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"本人限定版の{relative}を取得できません。") from error
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        if optional and relative == "data/us-market.json" and MISSING_ASSET_FALLBACK in raw:
            return None
        raise RuntimeError(f"本人限定版の{relative}がJSONではありません。") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"本人限定版の{relative}がオブジェクトではありません。")
    return raw, payload


def restore_us_market(base: str, destination: Path = PRIVATE_SITE) -> bool:
    response = remote_json(base, "data/us-market.json", optional=True)
    if response is None:
        return False
    raw, payload = response
    if payload.get("schemaVersion") != 1 or not isinstance(payload.get("asOf"), str):
        raise RuntimeError("前回の米国市場データの形式が不正です。")
    target = destination / "data" / "us-market.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return True


def prepare_us_release(base: str, us_market: Path, destination: Path = PRIVATE_SITE) -> Path:
    if not us_market.is_file():
        raise RuntimeError("米国市場データがありません。")
    us_payload = json.loads(us_market.read_text(encoding="utf-8"))
    if (us_payload.get("schemaVersion") != 1 or not us_payload.get("asOf")
            or us_payload.get("usSectors", {}).get("status") != "available"):
        raise RuntimeError("米国市場データの検証に失敗しました。")
    base = private_base(base)
    remote = {name: remote_json(base, f"data/{name}") for name in DATA_FILES}
    candidates = remote["latest-candidates.json"][1]
    bundle = candidates.get("searchUniverseBundle")
    shards: dict[str, bytes] = {}
    if isinstance(bundle, dict) and bundle.get("status") == "available":
        for shard in bundle.get("shards") or []:
            relative = shard.get("url") if isinstance(shard, dict) else None
            if not isinstance(relative, str) or not SHARD_PATTERN.fullmatch(relative):
                raise RuntimeError("本人限定版のランキング分割ファイル指定が不正です。")
            shards[relative] = remote_json(base, relative)[0]

    with tempfile.TemporaryDirectory() as folder:
        private_analysis = Path(folder) / "nikkei225-analysis.json"
        private_analysis.write_bytes(remote["nikkei225-analysis.json"][0])
        prepare_private_site(private_analysis=private_analysis, destination=destination,
                             validate_public_analysis=False)

    shard_dir = (destination / "data" / "search-universe").resolve()
    if shard_dir.is_dir():
        if not shard_dir.is_relative_to(destination.resolve()):
            raise RuntimeError("ランキング分割ファイルの置き場所が不正です。")
        shutil.rmtree(shard_dir)
    for name, (raw, _payload) in remote.items():
        (destination / "data" / name).write_bytes(raw)
    for relative, raw in shards.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    (destination / "data" / "us-market.json").write_bytes(us_market.read_bytes())

    errors = validate_private_site(destination, validate_public_analysis=False)
    if errors:
        raise RuntimeError("本人限定版の検証に失敗しました: " + " / ".join(errors))
    if remote["fundamentals-state-v1.json"][1].get("generatedAt") != candidates.get("generatedAt"):
        raise RuntimeError("財務状態とランキングの生成日時が一致しません。")
    print(f"Prepared private US release: JP={candidates.get('generatedAt')} US={us_payload['asOf']}")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="本人限定版の米国市場データを巻き戻さず配布")
    parser.add_argument("--mode", choices=("release", "restore"), required=True)
    parser.add_argument("--site-url", default=os.getenv("PRIVATE_SITE_URL", ""))
    parser.add_argument("--us-market", type=Path)
    args = parser.parse_args()
    base = private_base(args.site_url)
    if args.mode == "restore":
        print("Restored US market data" if restore_us_market(base) else "No prior US market data")
    else:
        if args.us_market is None:
            parser.error("--us-market is required for release")
        prepare_us_release(base, args.us_market)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
