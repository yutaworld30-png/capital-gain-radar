const SESSION_COOKIE = "cgr_session";
const SESSION_SECONDS = 7 * 24 * 60 * 60;
const encoder = new TextEncoder();

function securityHeaders(extra = {}) {
  return {
    "Cache-Control": "private, no-store, max-age=0",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    ...extra,
  };
}

function response(body, status = 200, extraHeaders = {}) {
  return new Response(body, {
    status,
    headers: securityHeaders(extraHeaders),
  });
}

function htmlEscape(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function safeNext(value) {
  const candidate = String(value || "");
  return candidate.startsWith("/") && !candidate.startsWith("//")
    ? candidate
    : "/investment-candidate-app.html";
}

function cookieValue(request, name) {
  const cookie = request.headers.get("Cookie") || "";
  for (const item of cookie.split(";")) {
    const [key, ...parts] = item.trim().split("=");
    if (key === name) return parts.join("=");
  }
  return "";
}

function toHex(buffer) {
  return Array.from(new Uint8Array(buffer), byte => byte.toString(16).padStart(2, "0")).join("");
}

async function digest(value) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(value)));
}

async function constantTimeEqual(left, right) {
  const [leftHash, rightHash] = await Promise.all([digest(left), digest(right)]);
  let difference = 0;
  for (let index = 0; index < leftHash.length; index += 1) {
    difference |= leftHash[index] ^ rightHash[index];
  }
  return difference === 0;
}

async function hmac(secret, message) {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return toHex(await crypto.subtle.sign("HMAC", key, encoder.encode(message)));
}

async function createSession(secret) {
  const expires = Math.floor(Date.now() / 1000) + SESSION_SECONDS;
  const nonce = crypto.randomUUID();
  const payload = `v1.${expires}.${nonce}`;
  return `${payload}.${await hmac(secret, payload)}`;
}

async function validSession(request, secret) {
  const token = cookieValue(request, SESSION_COOKIE);
  const parts = token.split(".");
  if (parts.length !== 4 || parts[0] !== "v1") return false;
  const expires = Number(parts[1]);
  if (!Number.isInteger(expires) || expires <= Math.floor(Date.now() / 1000)) return false;
  const payload = parts.slice(0, 3).join(".");
  const expected = await hmac(secret, payload);
  return constantTimeEqual(parts[3], expected);
}

async function validServiceRequest(request, serviceSecret) {
  const supplied = request.headers.get("X-Capital-Radar-Service") || "";
  return Boolean(supplied) && constantTimeEqual(supplied, serviceSecret);
}

function loginPage(next, error = false) {
  const safePath = htmlEscape(safeNext(next));
  const errorMessage = error
    ? '<p class="error" role="alert">合言葉が一致しません。少し待ってから、もう一度確認してください。</p>'
    : "";
  return `<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Capital Gain Radar - 本人確認</title>
  <style>
    *{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#eef3f7;color:#152238;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:20px}.panel{width:min(100%,420px);background:#fff;border:1px solid #d8e1ea;border-radius:8px;padding:28px;box-shadow:0 16px 42px rgba(21,34,56,.1)}.brand{font-size:14px;color:#52647a;margin:0 0 8px}h1{font-size:24px;margin:0 0 8px}p{line-height:1.6;color:#52647a;margin:0 0 22px}label{display:block;font-weight:700;margin-bottom:8px}input{width:100%;font:inherit;padding:12px;border:1px solid #aebdca;border-radius:6px;margin-bottom:14px}button{width:100%;border:0;border-radius:6px;background:#0c8c70;color:#fff;font:inherit;font-weight:700;padding:13px;cursor:pointer}.error{color:#a52b36;background:#fff1f2;border:1px solid #f0bec3;border-radius:6px;padding:10px;margin-bottom:14px}.note{font-size:12px;margin:16px 0 0}</style>
</head>
<body>
  <main class="panel">
    <p class="brand">Capital Gain Radar</p>
    <h1>本人確認</h1>
    <p>このアプリは個人利用専用です。設定した合言葉を入力してください。</p>
    ${errorMessage}
    <form method="post" action="/_auth/login">
      <input type="hidden" name="next" value="${safePath}">
      <label for="password">合言葉</label>
      <input id="password" name="password" type="password" minlength="16" maxlength="256" autocomplete="current-password" required autofocus>
      <button type="submit">ログイン</button>
    </form>
    <p class="note">認証状態はこの端末で7日間保持されます。共有端末では利用後にログアウトしてください。</p>
  </main>
</body>
</html>`;
}

function redirect(location, extraHeaders = {}) {
  return response(null, 303, { Location: location, ...extraHeaders });
}

function sessionCookie(value, maxAge) {
  return `${SESSION_COOKIE}=${value}; Path=/; Max-Age=${maxAge}; HttpOnly; Secure; SameSite=Strict`;
}

async function handleLogin(context) {
  const request = context.request;
  if (request.method === "GET") {
    const next = new URL(request.url).searchParams.get("next");
    if (await validSession(request, context.env.PRIVATE_SESSION_SECRET)) {
      return redirect(safeNext(next));
    }
    return response(loginPage(next), 200, { "Content-Type": "text/html; charset=utf-8" });
  }
  if (request.method !== "POST") return response("Method Not Allowed", 405, { Allow: "GET, POST" });
  const contentType = request.headers.get("Content-Type") || "";
  const contentLength = Number(request.headers.get("Content-Length") || 0);
  if (!contentType.startsWith("application/x-www-form-urlencoded") || contentLength > 4096) {
    return response("Bad Request", 400);
  }
  const body = await request.text();
  if (body.length > 4096) return response("Bad Request", 400);
  const form = new URLSearchParams(body);
  const password = String(form.get("password") || "");
  const next = safeNext(form.get("next"));
  const valid = password.length <= 256
    && await constantTimeEqual(password, context.env.PRIVATE_APP_PASSWORD);
  if (!valid) {
    await new Promise(resolve => setTimeout(resolve, 650));
    return response(loginPage(next, true), 401, { "Content-Type": "text/html; charset=utf-8" });
  }
  const session = await createSession(context.env.PRIVATE_SESSION_SECRET);
  return redirect(next, { "Set-Cookie": sessionCookie(session, SESSION_SECONDS) });
}

export async function onRequest(context) {
  const { PRIVATE_APP_PASSWORD, PRIVATE_SESSION_SECRET, PRIVATE_SERVICE_TOKEN } = context.env;
  if (!PRIVATE_APP_PASSWORD || !PRIVATE_SESSION_SECRET || !PRIVATE_SERVICE_TOKEN) {
    return response("Private application authentication is not configured.", 503);
  }

  const url = new URL(context.request.url);
  if (url.pathname === "/_auth/login") return handleLogin(context);
  if (url.pathname === "/_auth/logout") {
    if (context.request.method !== "POST") return response("Method Not Allowed", 405, { Allow: "POST" });
    return redirect("/_auth/login", { "Set-Cookie": sessionCookie("", 0) });
  }

  if (
    await validServiceRequest(context.request, PRIVATE_SERVICE_TOKEN)
    || await validSession(context.request, PRIVATE_SESSION_SECRET)
  ) {
    const upstream = await context.next();
    const headers = new Headers(upstream.headers);
    headers.set("Cache-Control", "private, no-store, max-age=0");
    headers.set("Referrer-Policy", "no-referrer");
    headers.set("X-Content-Type-Options", "nosniff");
    headers.set("X-Frame-Options", "DENY");
    return new Response(upstream.body, { status: upstream.status, headers });
  }

  const next = `${url.pathname}${url.search}`;
  return redirect(`/_auth/login?next=${encodeURIComponent(next)}`);
}
