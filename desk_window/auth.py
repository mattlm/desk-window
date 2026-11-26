"""Password sign-in: a login page, then a session cookie.

The cookie is "<expiry>.<signature>", an HMAC over the expiry with a key
made from a random secret (kept in the state dir, so sign-ins outlast a
restart) and the password (so changing the password signs everyone out).
Every route but the login page needs a valid one; without it the page
itself redirects to /login and everything else gets a 401.

Failed attempts are answered one at a time, a second apart, which keeps
guessing slow without needing per-address bookkeeping.
"""

import asyncio
import hashlib
import hmac
import html
import secrets
import time

from aiohttp import web

from .portal import STATE_DIR

COOKIE = "desk_window_session"
SESSION_DAYS = 30
FAIL_DELAY = 1.0  # s
KEY_FILE = STATE_DIR / "session-secret"


def _load_secret():
    try:
        secret = KEY_FILE.read_bytes()
        if len(secret) >= 32:
            return secret
    except FileNotFoundError:
        pass
    secret = secrets.token_bytes(32)
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    KEY_FILE.touch(mode=0o600)
    KEY_FILE.write_bytes(secret)
    return secret


class Auth:
    def __init__(self, password):
        self.password = password.encode()
        self.key = hashlib.sha256(_load_secret() + b"\0" + self.password).digest()
        self.fail_lock = asyncio.Lock()

    def _sign(self, expiry):
        return hmac.new(self.key, str(expiry).encode(), hashlib.sha256).hexdigest()

    def _valid(self, cookie):
        expiry, _, sig = (cookie or "").partition(".")
        if not expiry.isdigit() or int(expiry) < time.time():
            return False
        return hmac.compare_digest(sig, self._sign(int(expiry)))

    @web.middleware
    async def middleware(self, request, handler):
        if request.path == "/login" or self._valid(request.cookies.get(COOKIE)):
            return await handler(request)
        if request.method == "GET" and request.path == "/":
            raise web.HTTPFound("/login")
        raise web.HTTPUnauthorized(text="sign in at /login")

    def add_routes(self, app):
        app.router.add_get("/login", self.login_page)
        app.router.add_post("/login", self.login)
        app.router.add_post("/logout", self.logout)

    async def login_page(self, request, error=""):
        return web.Response(text=LOGIN_PAGE.replace("{error}", html.escape(error)),
                            content_type="text/html", headers={"Cache-Control": "no-store"})

    async def login(self, request):
        form = await request.post()
        given = str(form.get("password", "")).encode()
        if not hmac.compare_digest(hashlib.sha256(given).digest(), hashlib.sha256(self.password).digest()):
            async with self.fail_lock:
                await asyncio.sleep(FAIL_DELAY)
            return await self.login_page(request, "Wrong password.")
        expiry = int(time.time()) + SESSION_DAYS * 86400
        res = web.Response(status=303, headers={"Location": "/"})
        res.set_cookie(COOKIE, f"{expiry}.{self._sign(expiry)}", max_age=SESSION_DAYS * 86400,
                       httponly=True, samesite="Strict", secure=_is_https(request), path="/")
        return res

    async def logout(self, request):
        res = web.Response(status=303, headers={"Location": "/login"})
        res.del_cookie(COOKIE, path="/")
        return res


def _is_https(request):
    return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Desk Window</title>
<style>
  :root { color-scheme: light dark; --bg: #f4f4f5; --panel: #fff; --fg: #18181b; --muted: #71717a;
          --border: #d4d4d8; --accent: #2563eb; --bad: #dc2626; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #18181b; --panel: #27272a; --fg: #f4f4f5; --muted: #a1a1aa;
            --border: #3f3f46; --accent: #60a5fa; --bad: #f87171; }
  }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 16px;
         box-sizing: border-box; background: var(--bg); color: var(--fg);
         font: 15px/1.4 system-ui, sans-serif; }
  form { width: 100%; max-width: 320px; background: var(--panel); border: 1px solid var(--border);
         border-radius: 10px; padding: 24px; display: grid; gap: 12px; }
  h1 { margin: 0; font-size: 20px; }
  p { margin: 0; color: var(--muted); }
  input, button { font: inherit; padding: 8px 10px; border-radius: 6px; border: 1px solid var(--border); }
  input { background: var(--bg); color: var(--fg); }
  button { background: var(--accent); color: #fff; border: 0; cursor: pointer; }
  .error { color: var(--bad); }
  .error:empty { display: none; }
</style>
</head>
<body>
<form method="post" action="/login">
  <h1>🪟 Desk Window</h1>
  <p>Enter the password to control this desktop.</p>
  <input type="password" name="password" autocomplete="current-password" autofocus required aria-label="Password">
  <p class="error">{error}</p>
  <button type="submit">Sign in</button>
</form>
</body>
</html>
"""
