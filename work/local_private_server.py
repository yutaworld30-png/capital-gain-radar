from __future__ import annotations

import argparse
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from ipaddress import IPv4Address, ip_address
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from market_analysis import (
    LOCAL_CANDIDATE_OUTPUT,
    LOCAL_PRIVATE_DISTRIBUTION_MODE,
    LOCAL_OUTPUT,
    validate_analysis,
)
from prepare_local_private import validate_local_candidates


ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "outputs"
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
ANALYSIS_ROUTE = "/data/nikkei225-analysis.json"
CANDIDATE_ROUTE = "/data/latest-candidates.json"
LOGIN_ROUTE = "/login"
APP_ROUTE = "/investment-candidate-app.html"
PASSWORD_ENV = "CAPITAL_GAIN_RADAR_ACCESS_PASSWORD"
SESSION_COOKIE = "cgr_session"
MAX_LOGIN_FAILURES = 5
LOGIN_WINDOW_SECONDS = 60
MAX_LOGIN_BODY_BYTES = 2_048


class LocalPrivateServerError(RuntimeError):
    pass


@dataclass
class AuthenticationState:
    password: str | None
    session_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    failures: dict[str, list[float]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def required(self) -> bool:
        return self.password is not None

    def is_rate_limited(self, client: str) -> bool:
        now = time.monotonic()
        with self.lock:
            recent = [
                attempted_at
                for attempted_at in self.failures.get(client, [])
                if now - attempted_at < LOGIN_WINDOW_SECONDS
            ]
            self.failures[client] = recent
            return len(recent) >= MAX_LOGIN_FAILURES

    def record_failure(self, client: str) -> None:
        with self.lock:
            self.failures.setdefault(client, []).append(time.monotonic())

    def clear_failures(self, client: str) -> None:
        with self.lock:
            self.failures.pop(client, None)


def validate_bind_host(host: str) -> IPv4Address:
    try:
        address = ip_address(host)
    except ValueError as error:
        raise LocalPrivateServerError(
            "接続先はIPv4アドレスで指定してください。"
        ) from error
    if not isinstance(address, IPv4Address):
        raise LocalPrivateServerError("IPv6でのLAN配信には対応していません。")
    if address.is_unspecified or address.is_multicast or address.is_link_local:
        raise LocalPrivateServerError("安全でない接続先アドレスは指定できません。")
    if not address.is_loopback and not address.is_private:
        raise LocalPrivateServerError(
            "ループバックまたはプライベートIPv4アドレスだけ指定できます。"
        )
    return address


def load_local_analysis(path: Path = LOCAL_OUTPUT) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise LocalPrivateServerError(
            "ローカル分析JSONがありません。先に market_analysis.py --local-private を実行してください。"
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise LocalPrivateServerError(f"ローカル分析JSONを読み込めません: {error}") from error
    if not isinstance(payload, dict):
        raise LocalPrivateServerError("ローカル分析JSONのルートが不正です。")
    if payload.get("distributionMode") != LOCAL_PRIVATE_DISTRIBUTION_MODE:
        raise LocalPrivateServerError("ローカル個人利用モードのJSONではありません。")
    errors = validate_analysis(payload)
    if errors:
        raise LocalPrivateServerError(" / ".join(errors))
    return payload


def load_local_candidates(path: Path = LOCAL_CANDIDATE_OUTPUT) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise LocalPrivateServerError(
            "ローカル候補JSONがありません。先に prepare_local_private.py を実行してください。"
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise LocalPrivateServerError(f"ローカル候補JSONを読み込めません: {error}") from error
    errors = validate_local_candidates(payload)
    if errors:
        raise LocalPrivateServerError(" / ".join(errors))
    return payload


class LocalPrivateHandler(SimpleHTTPRequestHandler):
    server_version = "CapitalGainRadarLocal/1.0"

    def __init__(
        self,
        *args: object,
        analysis_path: Path,
        auth_state: AuthenticationState,
        candidate_path: Path,
        directory: str,
        **kwargs: object,
    ) -> None:
        self.analysis_path = analysis_path
        self.auth_state = auth_state
        self.candidate_path = candidate_path
        super().__init__(*args, directory=directory, **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def list_directory(self, path: str) -> None:
        self.send_error(403, "Directory listing is disabled")
        return None

    def _request_path(self) -> str:
        return unquote(urlsplit(self.path).path)

    def _is_authenticated(self) -> bool:
        if not self.auth_state.required:
            return True
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except ValueError:
            return False
        morsel = cookie.get(SESSION_COOKIE)
        return bool(
            morsel
            and secrets.compare_digest(
                morsel.value,
                self.auth_state.session_token,
            )
        )

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_login_page(
        self,
        *,
        status: int = 200,
        message: str = "",
    ) -> None:
        message_html = (
            f'<p class="error" role="alert">{message}</p>' if message else ""
        )
        body = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Capital Gain Radar ログイン</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; min-height: 100vh; display: grid; place-items: center;
      padding: 24px; color: #162238; background: #eef3f6;
      font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    }}
    main {{
      width: min(100%, 420px); padding: 28px; background: #fff;
      border: 1px solid #d6e0e8; border-radius: 8px;
    }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    p {{ margin: 0 0 20px; color: #617083; line-height: 1.6; }}
    label {{ display: block; margin-bottom: 8px; font-weight: 700; }}
    input {{
      width: 100%; min-height: 48px; padding: 10px 12px;
      border: 1px solid #aebdca; border-radius: 6px; font-size: 18px;
    }}
    button {{
      width: 100%; min-height: 48px; margin-top: 16px; border: 0;
      border-radius: 6px; color: #10233b; background: #8be0c3;
      font-size: 17px; font-weight: 700;
    }}
    .error {{ color: #a51d2d; font-weight: 700; }}
    .note {{ margin-top: 18px; margin-bottom: 0; font-size: 13px; }}
  </style>
</head>
<body>
  <main>
    <h1>Capital Gain Radar</h1>
    <p>同じWi-Fi内のスマホ専用ログインです。</p>
    {message_html}
    <form method="post" action="{LOGIN_ROUTE}">
      <label for="password">アクセスパスワード</label>
      <input id="password" name="password" type="password"
             autocomplete="current-password" required autofocus>
      <button type="submit">アプリを開く</button>
    </form>
    <p class="note">パスワードはPC側の起動画面に表示されています。</p>
  </main>
</body>
</html>"""
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(data)

    def _require_authentication(self) -> bool:
        if self._is_authenticated():
            return False
        self._redirect(LOGIN_ROUTE)
        return True

    def _local_data_path(self) -> Path | None:
        path = self._request_path()
        if path == ANALYSIS_ROUTE:
            return self.analysis_path
        if path == CANDIDATE_ROUTE:
            return self.candidate_path
        return None

    def _serve_local_json(self, path: Path, *, include_body: bool) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(404, "Local data not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if include_body:
            self.wfile.write(data)

    def do_GET(self) -> None:
        path = self._request_path()
        if path == LOGIN_ROUTE:
            if not self.auth_state.required or self._is_authenticated():
                self._redirect(APP_ROUTE)
                return
            self._send_login_page()
            return
        if self._require_authentication():
            return
        if path == "/":
            self._redirect(APP_ROUTE)
            return
        local_path = self._local_data_path()
        if local_path is not None:
            self._serve_local_json(local_path, include_body=True)
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        if self._require_authentication():
            return
        local_path = self._local_data_path()
        if local_path is not None:
            self._serve_local_json(local_path, include_body=False)
            return
        super().do_HEAD()

    def do_POST(self) -> None:
        if self._request_path() != LOGIN_ROUTE or not self.auth_state.required:
            self.send_error(404, "Not found")
            return
        client = self.client_address[0]
        if self.auth_state.is_rate_limited(client):
            self._send_login_page(
                status=429,
                message="試行回数が多すぎます。1分待ってから再度お試しください。",
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if not 0 < content_length <= MAX_LOGIN_BODY_BYTES:
            self.send_error(400, "Invalid request")
            return
        form = parse_qs(
            self.rfile.read(content_length).decode("utf-8", errors="replace"),
            keep_blank_values=True,
        )
        supplied = str(form.get("password", [""])[0])
        expected = self.auth_state.password or ""
        if not secrets.compare_digest(supplied, expected):
            self.auth_state.record_failure(client)
            time.sleep(0.2)
            self._send_login_page(
                status=401,
                message="パスワードが正しくありません。",
            )
            return
        self.auth_state.clear_failures(client)
        self.send_response(303)
        self.send_header("Location", APP_ROUTE)
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={self.auth_state.session_token}; "
            "Path=/; HttpOnly; SameSite=Strict",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()


class LoopbackThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def create_server(
    *,
    host: str = LOOPBACK_HOST,
    port: int = DEFAULT_PORT,
    outputs: Path = OUTPUTS,
    analysis_path: Path = LOCAL_OUTPUT,
    candidate_path: Path = LOCAL_CANDIDATE_OUTPUT,
    access_password: str | None = None,
) -> LoopbackThreadingHTTPServer:
    if port != 0 and not 1_024 <= port <= 65_535:
        raise LocalPrivateServerError("ポート番号は1024から65535の範囲で指定してください。")
    bind_address = validate_bind_host(host)
    is_lan = not bind_address.is_loopback
    if is_lan and (access_password is None or len(access_password) < 8):
        raise LocalPrivateServerError(
            "LAN配信には8文字以上のアクセスパスワードが必要です。"
        )
    load_local_analysis(analysis_path)
    load_local_candidates(candidate_path)
    auth_state = AuthenticationState(access_password)
    handler = partial(
        LocalPrivateHandler,
        directory=str(outputs.resolve()),
        analysis_path=analysis_path.resolve(),
        auth_state=auth_state,
        candidate_path=candidate_path.resolve(),
    )
    return LoopbackThreadingHTTPServer((str(bind_address), port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capital Gain Radar ローカル個人利用サーバー")
    parser.add_argument("--host", default=LOOPBACK_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    try:
        server = create_server(
            host=args.host,
            port=args.port,
            access_password=os.getenv(PASSWORD_ENV),
        )
    except (LocalPrivateServerError, OSError) as error:
        print(f"ERROR: {error}")
        return 1
    url = f"http://{args.host}:{args.port}{APP_ROUTE}"
    print("Capital Gain Radar ローカル個人利用モード", flush=True)
    print(f"URL: {url}")
    if args.host != LOOPBACK_HOST:
        print("同じWi-Fiに接続した端末だけで利用してください。")
        print("ログインには起動スクリプトが表示したパスワードが必要です。")
    print("この画面と .local-data の内容は外部公開・再配布しないでください。")
    print("終了するときは Ctrl+C を押してください。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nローカルサーバーを終了します。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
