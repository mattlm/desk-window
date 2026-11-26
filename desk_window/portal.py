"""Screen capture permission through xdg-desktop-portal's ScreenCast API.

KWin doesn't let another process read the screen directly, so we ask the
portal for a PipeWire stream of a monitor: CreateSession -> SelectSources -> Start (KDE shows its
screen-share dialog here) -> OpenPipeWireRemote, which hands back a PipeWire
fd + node id for GStreamer's pipewiresrc.

persist_mode = 2 asks KDE to remember the choice; Start returns a
restore token, kept in the state dir, that skips the dialog next time.
Tokens are single-use, so every Start saves the new one it gets back.
"""

import asyncio
import os
import secrets
from pathlib import Path

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"

SOURCE_MONITOR = 1
CURSOR_EMBEDDED = 2  # drawn into the video, so you can see where it is
PERSIST_UNTIL_REVOKED = 2

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "desk-window"
TOKEN_FILE = STATE_DIR / "portal-restore-token"


class PortalError(RuntimeError):
    pass


class ScreenCast:
    """One portal session. start() -> (pipewire fd, node id, area), where
    area is where the shared screen sits on the desktop, {"x", "y", "w",
    "h"} in logical pixels, or None if the portal didn't say."""

    def __init__(self):
        self.bus = None
        self.session = None
        self.closed = asyncio.Event()  # set if the user stops sharing from KDE

    async def start(self):
        self.bus = await MessageBus(negotiate_unix_fd=True).connect()
        self.bus.add_message_handler(self._on_session_closed)

        res = await self._request("CreateSession", "a{sv}", [], {
            "session_handle_token": Variant("s", _token()),
        })
        self.session = res["session_handle"].value
        await self._add_match(f"type='signal',interface='org.freedesktop.portal.Session',"
                              f"member='Closed',path='{self.session}'")

        cursor_modes = await self._property("AvailableCursorModes")
        opts = {
            "types": Variant("u", SOURCE_MONITOR),
            "multiple": Variant("b", False),
            "persist_mode": Variant("u", PERSIST_UNTIL_REVOKED),
        }
        if cursor_modes & CURSOR_EMBEDDED:
            opts["cursor_mode"] = Variant("u", CURSOR_EMBEDDED)
        token = _load_token()
        if token:
            opts["restore_token"] = Variant("s", token)
        await self._request("SelectSources", "oa{sv}", [self.session], opts)

        res = await self._request("Start", "osa{sv}", [self.session, ""], {})
        if "restore_token" in res:
            _save_token(res["restore_token"].value)
        streams = res["streams"].value
        if not streams:
            raise PortalError("the portal returned no streams")
        node_id, props = streams[0]
        area = None
        if "size" in props:
            w, h = props["size"].value
            x, y = props["position"].value if "position" in props else (0, 0)
            area = {"x": x, "y": y, "w": w, "h": h}
        return await self.open_remote(), node_id, area

    async def open_remote(self):
        """A new PipeWire connection fd for this session's stream. Each
        pipeline takes one (pipewiresrc closes it), so restarting the
        pipeline with new settings doesn't need a new session."""
        reply = await self._call("OpenPipeWireRemote", "oa{sv}", [self.session, {}])
        return reply.unix_fds[reply.body[0]]

    async def close(self):
        if self.bus is None:
            return
        if self.session:
            try:
                await self.bus.call(Message(destination=PORTAL, path=self.session,
                                            interface="org.freedesktop.portal.Session", member="Close"))
            except Exception:
                pass
        self.bus.disconnect()
        self.bus = None

    # --- D-Bus plumbing ---

    async def _call(self, method, signature, body):
        reply = await self.bus.call(Message(destination=PORTAL, path=PORTAL_PATH, interface=SCREENCAST,
                                            member=method, signature=signature, body=body))
        if reply.message_type == MessageType.ERROR:
            raise PortalError(f"{method}: {reply.error_name}: {reply.body[0] if reply.body else ''}")
        return reply

    async def _property(self, name):
        reply = await self.bus.call(Message(destination=PORTAL, path=PORTAL_PATH,
                                            interface="org.freedesktop.DBus.Properties", member="Get",
                                            signature="ss", body=[SCREENCAST, name]))
        if reply.message_type == MessageType.ERROR:
            return 0
        return reply.body[0].value

    async def _add_match(self, rule):
        await self.bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                    interface="org.freedesktop.DBus", member="AddMatch",
                                    signature="s", body=[rule]))

    async def _request(self, method, signature, args, options):
        """Call a portal method that answers through a Request object's
        Response signal. The request path is predictable from our bus name
        and handle_token, so we subscribe before calling and can't miss it."""
        token = _token()
        sender = self.bus.unique_name.lstrip(":").replace(".", "_")
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        done = asyncio.get_running_loop().create_future()

        def on_response(msg):
            if (msg.message_type == MessageType.SIGNAL and msg.path == path
                    and msg.member == "Response" and not done.done()):
                done.set_result(msg.body)

        self.bus.add_message_handler(on_response)
        try:
            await self._add_match(f"type='signal',interface='org.freedesktop.portal.Request',"
                                  f"member='Response',path='{path}'")
            await self._call(method, signature, [*args, {**options, "handle_token": Variant("s", token)}])
            # No timeout: Start waits for someone to answer KDE's dialog.
            code, results = await done
        finally:
            self.bus.remove_message_handler(on_response)
        if code == 1:
            raise PortalError("screen sharing was cancelled on the desktop")
        if code != 0:
            raise PortalError(f"{method} failed (portal response {code})")
        return results

    def _on_session_closed(self, msg):
        if (msg.message_type == MessageType.SIGNAL and msg.member == "Closed"
                and msg.path == self.session):
            self.closed.set()


def _token():
    return "rwi_" + secrets.token_hex(6)


def forget_token():
    """Make the next Start ask which screen to share again."""
    TOKEN_FILE.unlink(missing_ok=True)


def _load_token():
    try:
        return TOKEN_FILE.read_text().strip() or None
    except OSError:
        return None


def _save_token(token):
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    os.replace(tmp, TOKEN_FILE)
