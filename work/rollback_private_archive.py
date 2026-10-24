"""One-time, verified Pages rollback before recovering a missing valuation archive."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from valuation_history import BASIS


ROOT = Path(__file__).resolve().parents[1]
TARGET_SHORT_ID = "81983c8d"
SITE_URL = "https://capital-gain-radar-private.pages.dev"


def select_target(deployments: list[dict], short_id: str = TARGET_SHORT_ID) -> dict:
    matches = [item for item in deployments
               if item.get("short_id") == short_id
               and item.get("environment") == "production"
               and (item.get("latest_stage") or {}).get("status") == "success"]
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-f-]{36}", str(matches[0].get("id"))):
        raise RuntimeError("復元対象の成功済み本番デプロイを一意に特定できません。")
    return matches[0]


def main() -> None:
    account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    project = os.environ["CLOUDFLARE_PAGES_PROJECT"]
    token = os.environ["CLOUDFLARE_API_TOKEN"]
    service_token = os.environ["PRIVATE_SERVICE_TOKEN"]
    if (not re.fullmatch(r"[0-9a-f]{32}", account)
            or not re.fullmatch(r"[a-z0-9-]+", project)
            or os.environ.get("PRIVATE_SITE_URL", "").rstrip("/") != SITE_URL):
        raise RuntimeError("復元先のCloudflare設定を確認できません。")
    api_base = f"https://api.cloudflare.com/client/v4/accounts/{account}/pages/projects/{project}"

    def api(method: str, path: str) -> dict:
        request = Request(api_base + path, data=b"" if method == "POST" else None,
                          headers={"Authorization": "Bearer " + token,
                                   "Accept": "application/json"}, method=method)
        try:
            with urlopen(request, timeout=60) as response:
                payload = json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"Cloudflare Pages API HTTP {error.code}") from None
        if not payload.get("success") or not isinstance(payload.get("result"), (dict, list)):
            raise RuntimeError("Cloudflare Pages APIの応答を確認できません。")
        return payload

    def site(path: str) -> dict:
        request = Request(f"{SITE_URL}/data/{path}", headers={
            "Accept": "application/json", "X-Capital-Radar-Service": service_token,
        })
        with urlopen(request, timeout=60) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise RuntimeError("本人限定版のデータ形式が不正です。")
        return payload

    current = api("GET", "")["result"].get("latest_deployment") or {}
    current_id = str(current.get("id") or "")
    if not re.fullmatch(r"[0-9a-f-]{36}", current_id):
        raise RuntimeError("現在の本番デプロイを特定できません。")
    deployments: list[dict] = []
    for page in range(1, 21):
        query = urlencode({"env": "production", "per_page": 20, "page": page})
        batch = api("GET", "/deployments?" + query)["result"]
        if not isinstance(batch, list):
            raise RuntimeError("Cloudflare Pagesの配信履歴を確認できません。")
        deployments.extend(batch)
        if len(batch) < 20:
            break
    target = select_target(deployments)
    if target["id"] == current_id:
        raise RuntimeError("復元対象が現在の本番版と同じです。")

    us_market = site("us-market.json")
    if us_market.get("schemaVersion") != 1 or not us_market.get("asOf"):
        raise RuntimeError("現在の米国市場データを退避できません。")
    backup = ROOT / "work" / "tmp" / "us-market-before-rollback.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(us_market, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    api("POST", f"/deployments/{target['id']}/rollback")
    try:
        for _ in range(12):
            try:
                candidates = site("latest-candidates.json")
                manifest = site("valuation-history-v1.json")
                shards = manifest.get("shards")
                if (candidates.get("valuationHistorySummary")
                        and manifest.get("basis") == BASIS
                        and isinstance(shards, list) and shards):
                    first_path = shards[0].get("url") if isinstance(shards[0], dict) else None
                    if isinstance(first_path, str) and re.fullmatch(r"valuation-history/[0-9A-Z]{2}\.json", first_path):
                        first = site(first_path)
                        if first.get("basis") == BASIS:
                            print(f"Verified restored valuation archive: {len(shards)} shards")
                            return
            except (OSError, ValueError):
                pass
            time.sleep(5)
        raise RuntimeError("復元後の本人限定版でバリュエーション履歴を確認できません。")
    except Exception:
        api("POST", f"/deployments/{current_id}/rollback")
        raise


if __name__ == "__main__":
    main()
