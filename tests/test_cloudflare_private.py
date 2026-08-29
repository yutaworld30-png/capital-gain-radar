from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

import fetch_official_data as pipeline  # noqa: E402
from market_analysis import (  # noqa: E402
    PRIVATE_CLOUD_DISTRIBUTION_MODE,
    PUBLIC_DISTRIBUTION_MODE,
    build_analysis_payload,
)
from prepare_cloudflare_private import (  # noqa: E402
    PrivateDeploymentError,
    prepare_private_site,
)
from validate_cloudflare_private import validate_private_site  # noqa: E402


def price_rows(count: int = 140) -> list[dict[str, float | str]]:
    start = date(2026, 1, 1)
    return [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "open": 40_000 + index,
            "high": 40_100 + index,
            "low": 39_900 + index,
            "close": 40_050 + index,
        }
        for index in range(count)
    ]


def analysis(mode: str) -> dict:
    rows = price_rows()
    latest_date = str(rows[-1]["date"])
    private = mode == PRIVATE_CLOUD_DISTRIBUTION_MODE
    return build_analysis_payload(
        rows,
        generated_at="2026-08-28T16:10:00+09:00",
        price_url="https://example.test/chart",
        per_rows=(
            [{"date": latest_date, "weightedPer": 18.0, "indexPer": 23.0}]
            if private
            else []
        ),
        per_source=(
            {"status": "available", "url": "https://example.test/per", "asOf": latest_date}
            if private
            else {"status": "permission-required", "url": "https://example.test/per"}
        ),
        margin={"status": "available" if private else "permission-required", "rows": []},
        investor={"status": "available" if private else "permission-required", "rows": []},
        breadth={"status": "available", "rows": []},
        distribution_mode=mode,
    )


class CloudflarePrivateTests(unittest.TestCase):
    def test_service_header_is_attached_only_when_configured(self) -> None:
        with patch.dict(
            pipeline.os.environ,
            {"PRIVATE_SERVICE_TOKEN": "service-secret"},
        ):
            headers = pipeline._remote_auth_headers()
        self.assertEqual(headers["X-Capital-Radar-Service"], "service-secret")

        with patch.dict(
            pipeline.os.environ,
            {"PRIVATE_SERVICE_TOKEN": ""},
        ):
            self.assertEqual(pipeline._remote_auth_headers(), {})

    def test_private_stage_overrides_analysis_and_keeps_public_source_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public_site = root / "outputs"
            (public_site / "data").mkdir(parents=True)
            (public_site / "investment-candidate-app.html").write_text(
                "<!doctype html><title>Capital Gain Radar</title>",
                encoding="utf-8",
            )
            (public_site / "data" / "nikkei225-analysis.json").write_text(
                json.dumps(analysis(PUBLIC_DISTRIBUTION_MODE)),
                encoding="utf-8",
            )
            private_analysis = root / "private-analysis.json"
            private_analysis.write_text(
                json.dumps(analysis(PRIVATE_CLOUD_DISTRIBUTION_MODE)),
                encoding="utf-8",
            )
            destination = root / "private-site"

            prepare_private_site(
                public_site=public_site,
                private_analysis=private_analysis,
                destination=destination,
                generated_at="2026-08-28T16:10:00+09:00",
            )

            deployed = json.loads(
                (destination / "data" / "nikkei225-analysis.json").read_text(encoding="utf-8")
            )
            still_public = json.loads(
                (public_site / "data" / "nikkei225-analysis.json").read_text(encoding="utf-8")
            )
            self.assertEqual(deployed["distributionMode"], PRIVATE_CLOUD_DISTRIBUTION_MODE)
            self.assertEqual(still_public["distributionMode"], PUBLIC_DISTRIBUTION_MODE)
            self.assertEqual(validate_private_site(destination, public_site=public_site), [])

    def test_private_stage_rejects_public_analysis_as_private_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public_site = root / "outputs"
            (public_site / "data").mkdir(parents=True)
            (public_site / "investment-candidate-app.html").write_text("app", encoding="utf-8")
            payload = analysis(PUBLIC_DISTRIBUTION_MODE)
            public_path = public_site / "data" / "nikkei225-analysis.json"
            public_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(PrivateDeploymentError, "private-cloud"):
                prepare_private_site(
                    public_site=public_site,
                    private_analysis=public_path,
                    destination=root / "private-site",
                )

    def test_password_middleware_uses_signed_secure_cookie_and_fails_closed(self) -> None:
        source = (
            ROOT / "cloudflare" / "functions" / "_middleware.js"
        ).read_text(encoding="utf-8")
        for expected in (
            "PRIVATE_APP_PASSWORD",
            "PRIVATE_SESSION_SECRET",
            "PRIVATE_SERVICE_TOKEN",
            "crypto.subtle.sign",
            "constantTimeEqual",
            "HttpOnly; Secure; SameSite=Strict",
            'return response("Private application authentication is not configured.", 503)',
            'url.pathname === "/_auth/logout"',
        ):
            self.assertIn(expected, source)
        self.assertNotIn("PRIVATE_APP_PASSWORD =", source)

        html = (ROOT / "outputs" / "investment-candidate-app.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('action="/_auth/logout"', html)
        self.assertIn("el.privateLogoutForm.hidden = !privateCloud", html)

    def test_workflow_is_disabled_until_explicit_setup_and_uses_password_secret(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "deploy-cloudflare-private.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("CLOUDFLARE_PRIVATE_DEPLOYMENT_ENABLED", workflow)
        self.assertIn("NIKKEI_PRIVATE_CLOUD_USE_CONFIRMED", workflow)
        self.assertIn("PRIVATE_SERVICE_TOKEN", workflow)
        self.assertIn("python work/validate_cloudflare_private.py", workflow)
        self.assertIn("workingDirectory: .private-deploy", workflow)
        self.assertIn("pages deploy .", workflow)


if __name__ == "__main__":
    unittest.main()
