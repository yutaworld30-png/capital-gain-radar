from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from local_private_server import (  # noqa: E402
    APP_ROUTE,
    AuthenticationState,
    LOOPBACK_HOST,
    MAX_LOGIN_FAILURES,
    LocalPrivateServerError,
    create_server,
    load_local_analysis,
    load_local_candidates,
    validate_bind_host,
)
from market_analysis import (  # noqa: E402
    LOCAL_PRIVATE_DISTRIBUTION_MODE,
    build_analysis_payload,
)


def sample_rows(count: int = 140) -> list[dict[str, float | str]]:
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


def analysis_payload(distribution_mode: str) -> dict[str, object]:
    return build_analysis_payload(
        sample_rows(),
        generated_at="2026-07-20T10:00:00+09:00",
        price_url="https://example.test/chart",
        per_rows=[],
        per_source={"status": "unavailable"},
        margin={"status": "unavailable", "rows": []},
        investor={"status": "unavailable", "rows": []},
        breadth={"status": "available", "rows": []},
        distribution_mode=distribution_mode,
    )


@contextmanager
def running_authenticated_server():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        outputs = root / "outputs"
        outputs.mkdir()
        (outputs / "investment-candidate-app.html").write_text(
            "<!doctype html><title>local app</title>",
            encoding="utf-8",
        )
        analysis = root / "analysis.json"
        candidates = root / "candidates.json"
        analysis.write_text("{}", encoding="utf-8")
        candidates.write_text("{}", encoding="utf-8")
        with (
            patch("local_private_server.load_local_analysis"),
            patch("local_private_server.load_local_candidates"),
        ):
            server = create_server(
                host=LOOPBACK_HOST,
                port=0,
                outputs=outputs,
                analysis_path=analysis,
                candidate_path=candidates,
                access_password="correct-password",
            )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class LocalPrivateServerTests(unittest.TestCase):
    def test_server_is_fixed_to_ipv4_loopback(self) -> None:
        self.assertEqual(LOOPBACK_HOST, "127.0.0.1")

    def test_loader_accepts_only_local_private_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analysis.json"
            path.write_text(
                json.dumps(analysis_payload(LOCAL_PRIVATE_DISTRIBUTION_MODE)),
                encoding="utf-8",
            )
            payload = load_local_analysis(path)
        self.assertEqual(payload["distributionMode"], LOCAL_PRIVATE_DISTRIBUTION_MODE)

    def test_loader_rejects_public_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analysis.json"
            path.write_text(
                json.dumps(analysis_payload("public")),
                encoding="utf-8",
            )
            with self.assertRaises(LocalPrivateServerError):
                load_local_analysis(path)

    def test_candidate_loader_accepts_validated_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            path.write_text(json.dumps({"candidates": []}), encoding="utf-8")
            with patch(
                "local_private_server.validate_local_candidates",
                return_value=[],
            ):
                payload = load_local_candidates(path)
        self.assertEqual(payload, {"candidates": []})

    def test_candidate_loader_rejects_invalid_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            path.write_text(json.dumps({"candidates": []}), encoding="utf-8")
            with patch(
                "local_private_server.validate_local_candidates",
                return_value=["候補データが不正です。"],
            ):
                with self.assertRaises(LocalPrivateServerError):
                    load_local_candidates(path)

    def test_bind_host_accepts_only_loopback_or_private_ipv4(self) -> None:
        self.assertTrue(validate_bind_host("127.0.0.1").is_loopback)
        self.assertTrue(validate_bind_host("192.168.1.20").is_private)
        for host in ("0.0.0.0", "169.254.1.1", "8.8.8.8", "::1", "localhost"):
            with self.subTest(host=host):
                with self.assertRaises(LocalPrivateServerError):
                    validate_bind_host(host)

    def test_lan_server_requires_eight_character_password(self) -> None:
        for password in (None, "short"):
            with self.subTest(password=password):
                with self.assertRaises(LocalPrivateServerError):
                    create_server(
                        host="192.168.1.20",
                        port=8768,
                        access_password=password,
                    )

    def test_failed_login_is_temporarily_rate_limited(self) -> None:
        state = AuthenticationState("correct-password")
        for _ in range(MAX_LOGIN_FAILURES):
            state.record_failure("192.168.1.30")
        self.assertTrue(state.is_rate_limited("192.168.1.30"))
        state.clear_failures("192.168.1.30")
        self.assertFalse(state.is_rate_limited("192.168.1.30"))

    def test_password_login_gates_html_and_json(self) -> None:
        with running_authenticated_server() as server:
            port = server.server_address[1]
            connection = http.client.HTTPConnection(LOOPBACK_HOST, port, timeout=2)
            connection.request("GET", APP_ROUTE)
            response = connection.getresponse()
            self.assertEqual(response.status, 303)
            self.assertEqual(response.getheader("Location"), "/login")
            response.read()

            body = "password=correct-password"
            connection.request(
                "POST",
                "/login",
                body=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(body)),
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 303)
            cookie = response.getheader("Set-Cookie")
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Strict", cookie)
            response.read()

            connection.request(
                "GET",
                APP_ROUTE,
                headers={"Cookie": cookie.split(";", 1)[0]},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b"local app", response.read())
            connection.close()


if __name__ == "__main__":
    unittest.main()
