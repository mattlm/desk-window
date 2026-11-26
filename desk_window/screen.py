"""The KDE desktop's layout (kscreen-doctor) and screenshots (spectacle).

The absolute pointer spans KWin's whole workspace, the bounding box of the
enabled outputs, so the page's frame is that box with each output outlined
inside it. Coordinates are logical (after scale and rotation), relative to
the box's top-left, which is how KWin positions the cursor.

Screenshots go through spectacle rather than KWin's ScreenShot2 D-Bus API:
KWin only lets executables listed in a .desktop file with
X-KDE-DBUS-Restricted-Interfaces call it, which a Python script isn't.
spectacle is already authorised, and a
background capture of the full workspace takes ~0.25s.
"""

import asyncio
import io
import json
import os
import tempfile
import time

from PIL import Image

# kscreen rotation flags: 2 = left, 8 = right (90° turns swap width/height)
_ROTATED = {2, 8}


async def _run(*cmd, timeout=10):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"{cmd[0]} timed out")
    if proc.returncode:
        raise RuntimeError(f"{cmd[0]} exited {proc.returncode}: {err.decode(errors='replace').strip()}")
    return out


async def layout():
    data = json.loads(await _run("kscreen-doctor", "-j"))
    outputs = []
    for o in data.get("outputs", []):
        if not (o.get("enabled") and o.get("connected")):
            continue
        w, h = o["size"]["width"], o["size"]["height"]
        if o.get("rotation") in _ROTATED:
            w, h = h, w
        scale = o.get("scale") or 1
        mode = next((m for m in o.get("modes", []) if m.get("id") == o.get("currentModeId")), {})
        outputs.append({"name": o["name"], "x": o["pos"]["x"], "y": o["pos"]["y"],
                        "w": round(w / scale), "h": round(h / scale),
                        # Hz, rounded: the most frames per second its capture can give
                        "hz": round(mode["refreshRate"]) if mode.get("refreshRate") else None})
    if not outputs:
        raise RuntimeError("kscreen-doctor reports no enabled outputs")
    left = min(o["x"] for o in outputs)
    top = min(o["y"] for o in outputs)
    for o in outputs:
        o["x"] -= left
        o["y"] -= top
    return {
        # Where the box's top-left is in KWin's coordinates, for placing a
        # screen the portal reports by its absolute position.
        "left": left, "top": top,
        "width": max(o["x"] + o["w"] for o in outputs),
        "height": max(o["y"] + o["h"] for o in outputs),
        "outputs": sorted(outputs, key=lambda o: (o["x"], o["y"])),
    }


class Screenshots:
    """One spectacle capture shared by every viewer within min_age seconds."""

    def __init__(self, min_age):
        self.min_age = min_age
        self._lock = asyncio.Lock()
        self._taken = 0.0
        self._image = None

    async def jpeg(self, width):
        async with self._lock:
            if self._image is None or time.monotonic() - self._taken >= self.min_age:
                self._image = await self._capture()
                self._taken = time.monotonic()
            image = self._image
        return await asyncio.to_thread(_encode, image, width)

    async def _capture(self):
        runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        fd, path = tempfile.mkstemp(prefix="desk-window-", suffix=".png", dir=runtime)
        os.close(fd)
        try:
            await _run("spectacle", "--background", "--nonotify", "--fullscreen", "--output", path)
            with Image.open(path) as im:
                return im.convert("RGB")
        finally:
            os.unlink(path)


def _encode(image, width):
    if width and width < image.width:
        image = image.resize((width, round(image.height * width / image.width)), Image.BILINEAR)
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=75)
    return buf.getvalue()
