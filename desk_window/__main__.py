"""Desk Window server.

Sign-in is a password (auth.py) when one is given; without one it only
listens on loopback, unless --no-auth says something in front of it (a
reverse proxy with its own sign-in) handles that. Either way the
websockets only accept the page's own origin, so another site open in the
same browser can't drive the desktop through them.

Websocket messages (JSON, client -> server):
  {"t": "move", "x": 0-1, "y": 0-1}
  {"t": "button", "b": "left"|"right"|"middle"|"back"|"forward", "down": bool}
  {"t": "rel", "dx": px, "dy": px}       relative motion, sent while dragging
  {"t": "wheel", "dx": notches, "dy": notches}      browser sign
  {"t": "key", "code": KeyboardEvent.code, "down": bool}
  {"t": "key", "key": KeyboardEvent.key, "down": bool}   named keys, no code
  {"t": "text", "text": "..."}                       soft keyboards
  {"t": "release"}                                   let go of everything
Whatever a connection still holds is released when it closes, so a dropped
connection can't leave a key or button stuck down. An up from any connection
lets go of a key or button, whichever connection pressed it.

/video is a second websocket, server -> client, carrying the live video
stream (format in video.py). The client sends {"t": "keyframe"} on it when
its decoder needs to start over, {"t": "settings", "codec", "bitrate",
"fps", "scale"} to change the stream's settings (shared by all viewers),
{"t": "repick"} to choose the shared screen again,
and {"t": "bench"} / {"t": "stats"} for the benchmark (see video.py).
"""

import argparse
import asyncio
import atexit
import ipaddress
import json
import os
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from . import config as config_mod
from . import keymap, screen
from .auth import Auth
from .devices import BUTTONS, Devices

PUBLIC = Path(__file__).resolve().parent / "public"
TEXT_MAX = 2000
KEY_GAP = 0.004  # s between typed characters
DEFAULT_PORT = 5110

config = None  # config.Config, loaded in main()
devices = None  # Devices, created on startup
# Each page picks its own refresh rate; this only lets viewers polling at the
# same moment share one capture (spectacle takes ~0.25s, the real floor).
screenshots = screen.Screenshots(min_age=0.2)
video_stream = None  # video.VideoStream, if video is enabled


async def index(request):
    return web.FileResponse(PUBLIC / "index.html", headers={"Cache-Control": "no-cache"})


async def api_layout(request):
    try:
        lay = await screen.layout()
    except Exception as err:
        return web.json_response({"error": str(err)}, status=503)
    return web.json_response({
        **lay,
        "screenshots": config.screenshots_enabled,
        "screenshotIntervalMs": config.screenshot_interval_ms,
        "video": video_stream is not None,
    })


async def api_screenshot(request):
    if not config.screenshots_enabled:
        raise web.HTTPNotFound()
    try:
        width = max(0, min(8192, int(request.query.get("w", "0"))))
    except ValueError:
        width = 0
    try:
        body = await screenshots.jpeg(width)
    except Exception as err:
        return web.json_response({"error": str(err)}, status=503)
    return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


def _same_origin(request):
    origin = request.headers.get("Origin")
    host = request.headers.get("X-Forwarded-Host") or request.host
    return bool(origin) and urlsplit(origin).netloc.lower() == host.lower()


# What's held down on the desktop, and which connection pressed it. Shared
# across connections, so an up from any of them lets go: when the page
# reconnects with a key held, its keyup arrives on the new connection while
# the old one (if the server hasn't noticed it's dead yet, up to a heartbeat
# or two) still holds the key. Per connection, that keyup would be ignored.
held_keys = {}  # evdev code -> Connection
held_buttons = {}  # button name -> Connection


class Connection:
    def release(self):
        for code in [c for c, owner in held_keys.items() if owner is self]:
            del held_keys[code]
            devices.key(code, False)
        for b in [b for b, owner in held_buttons.items() if owner is self]:
            del held_buttons[b]
            devices.button(b, False)

    async def handle(self, msg):
        t = msg.get("t")
        if t == "move":
            devices.move(_num(msg["x"]), _num(msg["y"]))
        elif t == "button":
            b, down = msg["b"], bool(msg["down"])
            if b not in BUTTONS or down == (b in held_buttons):
                return
            if down:
                held_buttons[b] = self
            else:
                del held_buttons[b]
            devices.button(b, down)
        elif t == "rel":
            devices.rel(_num(msg["dx"]), _num(msg["dy"]))
        elif t == "wheel":
            devices.wheel(max(-50, min(50, _num(msg.get("dx", 0)))),
                          max(-50, min(50, _num(msg.get("dy", 0)))))
        elif t == "key":
            code = keymap.CODES.get(msg.get("code")) or keymap.NAMED.get(msg.get("key"))
            if code is None:
                return
            down = bool(msg["down"])
            if down == (code in held_keys):
                return  # browser auto-repeat; KDE repeats held keys itself
            if down:
                held_keys[code] = self
            else:
                del held_keys[code]
            devices.key(code, down)
        elif t == "text":
            await self.type_text(str(msg["text"])[:TEXT_MAX])
        elif t == "release":
            self.release()

    async def type_text(self, text):
        shift = keymap.CODES["ShiftLeft"]
        for ch in text:
            entry = keymap.CHARS.get(ch)
            if entry is None:
                continue
            code, shifted = entry
            if shifted:
                devices.key(shift, True)
            devices.key(code, True)
            devices.key(code, False)
            if shifted:
                devices.key(shift, False)
            await asyncio.sleep(KEY_GAP)


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("expected a number")
    return float(v)


async def ws_handler(request):
    if not _same_origin(request):
        raise web.HTTPForbidden(text="cross-origin websocket refused")
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    conn = Connection()
    try:
        async for frame in ws:
            if frame.type != WSMsgType.TEXT:
                continue
            try:
                await conn.handle(json.loads(frame.data))
            except (ValueError, KeyError, TypeError):
                pass  # malformed message: ignore it, keep the connection
    finally:
        conn.release()
    return ws


async def video_handler(request):
    if video_stream is None:
        raise web.HTTPNotFound()
    if not _same_origin(request):
        raise web.HTTPForbidden(text="cross-origin websocket refused")
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    viewer = await video_stream.add(ws)
    pump = asyncio.create_task(video_stream.pump(viewer))
    try:
        async for frame in ws:
            if frame.type != WSMsgType.TEXT:
                continue
            try:
                msg = json.loads(frame.data)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("t") == "keyframe":
                viewer.need_key = True
                video_stream.request_keyframe()
            elif msg.get("t") == "settings":
                await video_stream.apply(msg)
            elif msg.get("t") == "bench":
                await video_stream.bench(viewer, msg)
            elif msg.get("t") == "repick":
                await video_stream.repick()
            elif msg.get("t") == "stats":
                viewer.queue.put_nowait(video_stream.stats(viewer))
    finally:
        pump.cancel()
        video_stream.remove(viewer)
    return ws


async def on_startup(app):
    global devices, video_stream
    devices = Devices()
    if config.video_enabled:
        from .video import VideoStream  # GStreamer import is slow; skip it when off
        try:
            video_stream = VideoStream(config.video_encoder, {
                "codec": config.video_codec, "bitrate": config.video_bitrate_kbps,
                "fps": config.video_fps, "scale": config.video_scale,
            })
        except RuntimeError as err:
            print(f"video off: {err}", flush=True)
        else:
            atexit.register(video_stream.stop_now)


async def on_cleanup(app):
    if devices:
        devices.close()


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="desk-window",
        description="Control this KDE Plasma desktop's mouse and keyboard from a browser, with live video.")
    p.add_argument("--host", default=os.environ.get("DESK_WINDOW_HOST", "127.0.0.1"),
                   help="address to listen on (default 127.0.0.1; 0.0.0.0 for every interface)")
    p.add_argument("--port", type=int, default=int(os.environ.get("DESK_WINDOW_PORT", DEFAULT_PORT)),
                   help=f"port to listen on (default {DEFAULT_PORT})")
    p.add_argument("--password-file", metavar="PATH", default=os.environ.get("DESK_WINDOW_PASSWORD_FILE"),
                   help="file holding the sign-in password (or set $DESK_WINDOW_PASSWORD)")
    p.add_argument("--password", help="the sign-in password; visible to other users in ps, so prefer "
                   "--password-file or $DESK_WINDOW_PASSWORD")
    p.add_argument("--no-auth", action="store_true",
                   help="allow listening beyond loopback without a password, for when a reverse proxy "
                   "in front handles sign-in")
    p.add_argument("--tls-cert", metavar="PATH", help="serve HTTPS with this certificate (PEM)")
    p.add_argument("--tls-key", metavar="PATH", help="...and this private key (PEM)")
    p.add_argument("--config", metavar="PATH",
                   help=f"config file (default {config_mod.default_path()})")
    return p.parse_args(argv)


def _password(args):
    if args.password_file:
        try:
            return Path(args.password_file).read_text().strip()
        except OSError as err:
            sys.exit(f"desk-window: can't read the password file: {err}")
    if args.password:
        print("warning: --password shows in the process list; prefer --password-file "
              "or $DESK_WINDOW_PASSWORD", file=sys.stderr, flush=True)
        return args.password
    return os.environ.get("DESK_WINDOW_PASSWORD") or None


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv=None):
    global config
    args = parse_args(argv)
    try:
        config = config_mod.load(args.config)
    except (ValueError, OSError) as err:
        sys.exit(f"desk-window: bad config: {err}")
    password = _password(args)
    if password is not None and not password:
        sys.exit("desk-window: the password is empty")
    if not password and not args.no_auth and not _loopback(args.host):
        sys.exit(f"desk-window: refusing to listen on {args.host} without a password. Give one with "
                 "--password-file or $DESK_WINDOW_PASSWORD, or pass --no-auth if a reverse proxy "
                 "in front of it handles sign-in.")
    if bool(args.tls_cert) != bool(args.tls_key):
        sys.exit("desk-window: --tls-cert and --tls-key go together")
    ssl_ctx = None
    if args.tls_cert:
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(args.tls_cert, args.tls_key)

    middlewares = []
    auth = Auth(password) if password else None
    if auth:
        middlewares.append(auth.middleware)
    app = web.Application(middlewares=middlewares)
    if auth:
        auth.add_routes(app)
    app.router.add_get("/", index)
    app.router.add_get("/api/layout", api_layout)
    app.router.add_get("/api/screenshot", api_screenshot)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/video", video_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    scheme = "https" if ssl_ctx else "http"
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"Desk Window on {scheme}://{shown}:{args.port} "
          f"(listening on {args.host}; sign-in {'password' if auth else 'off'}; "
          f"screenshots {'on' if config.screenshots_enabled else 'off'}; "
          f"video {'encoder ' + config.video_encoder if config.video_enabled else 'off'})",
          flush=True)
    if not auth and not _loopback(args.host):
        print("warning: no password; anyone who can reach this port controls the desktop",
              file=sys.stderr, flush=True)
    web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_ctx, print=None)


if __name__ == "__main__":
    main()
