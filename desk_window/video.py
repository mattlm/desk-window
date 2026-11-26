"""Live video of the desktop (H.264, H.265 or AV1), fanned out to websocket
viewers.

Capture comes from the ScreenCast portal (portal.py) as a PipeWire stream;
GStreamer encodes it, on the GPU through VA-API where it can, and in
software otherwise (x264 / x265; there's
no software AV1, since SVT-AV1's defaults buffer frames, which is wrong for
a live view). Each encoded access unit goes to every viewer as one binary
websocket message:

    byte 0      flags (1 = keyframe)
    bytes 1-8   timestamp, microseconds, big-endian
    rest        the access unit: Annex B for H.264/H.265 (start codes,
                parameter sets repeated on keyframes), an OBU temporal unit
                for AV1

and the browser decodes it with WebCodecs. Before a viewer's first frame,
and whenever the codec or size changes, it gets a JSON text message
{"t": "config", "codec": "avc1.xxxxxx" | "hev1..." | "av01...", "name", "width", "height",
"area"}, area being where the shared screen sits on the desktop (logical
{"x", "y", "w", "h"}, or null if unknown), so the page can place the
picture over that screen.

{"t": "repick"} forgets the portal's saved choice and starts sharing
again, so KDE asks which screen to share.

Settings - codec, bitrate, frame-rate cap, resolution scale - belong to the
stream, so every viewer sees the same ones. A viewer changes them with
{"t": "settings", ...}; they're saved to the state dir (so they outlast a
restart; config.toml only gives the first defaults) and sent to every
viewer as {"t": "settings", ..., "codecs": {codec: "gpu" | "cpu"}}.
Changing them rebuilds the pipeline but keeps the portal session, so it
takes about half a second and never shows KDE's dialog.

Tuned for latency: no B-frames, no buffering in appsink, constant bitrate.
A viewer that falls behind has its queue dropped and waits for the next
keyframe, which is requested from the encoder straight away; so is one for
every new viewer. pipewiresrc resends the last frame every second when the
screen is still, so a keyframe request always gets answered.

Benchmark: {"t": "bench", "pattern": "moving" | "noise", "bitrate", "fps"}
swaps the capture for a generated test picture at the same size (so it
measures the encoder, the network and the browser, not KDE's capture,
which only sends frames when the screen changes), with that bitrate and
frame rate overriding the settings - not saved. {"t": "bench"} with no
pattern goes back to the screen, as does the viewer that started it
leaving, or BENCH_MAX seconds passing. {"t": "stats"} gets back
{"t": "stats", "frames", "bytes", "resyncs", "time"}: running totals of
access units produced, their bytes, and this viewer's resyncs, so the page
measures any span by subtracting two of them. The reply queues behind any
frames still waiting for that viewer, so its round trip includes that
backlog.

The pipeline (and the portal session) runs only while someone is watching,
and stops STOP_GRACE seconds after the last viewer leaves, so a page reload
doesn't go back through the portal.

If the pipeline fails while someone is watching, it's rebuilt on the same
portal session, up to RECOVER_MAX times in RECOVER_WINDOW seconds; past
that the error goes to the viewers as before. The usual failure is a GPU
encoder giving up on a frame at a high bitrate, which surfaces as
pipewiresrc's "Internal data stream error". The VA encoders get a
half-second cpb-size (cpb_kbits) to keep frames in check; recovery covers
what's left.
Errors are counted and sent to every viewer as {"t": "health", "errors",
"last_error", "last_error_ago", "gave_up"} (the age in seconds at sending,
so the page needn't share the server's clock).

Bitrate "auto" finds the highest bitrate that works (see AutoBitrate): it
steps down on an encoder error or on viewers falling behind (resyncs; the
page disconnects while its tab is hidden, so those are visible pages), and
creeps back up after a while without trouble. The current value goes out
as "auto_kbps" in the settings message, and is changed on the running
encoder, no rebuild.
"""

import asyncio
import json
import os
import struct
import threading
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
gi.require_version("GstPbutils", "1.0")
from gi.repository import Gst, GstPbutils, GstVideo  # noqa: E402

from .config import CODECS, clamp_bitrate, clamp_fps, clamp_scale  # noqa: E402
from .portal import STATE_DIR, ScreenCast, forget_token  # noqa: E402

Gst.init(None)

STOP_GRACE = 15  # s
VIEWER_QUEUE_MAX = 30  # access units (~0.5s at 60fps) before a viewer is resynced
KEYFRAME_SECONDS = 2  # fallback keyframe interval, for lost keyframe requests
HEADER = struct.Struct(">BQ")
SETTINGS_FILE = STATE_DIR / "video-settings.json"
BENCH_MAX = 120  # s, in case the page that started a benchmark never ends it
RECOVER_MAX = 3  # pipeline rebuilds after an error...
RECOVER_WINDOW = 60  # ...within this many seconds, before giving up
# Auto bitrate (kbps / seconds); see AutoBitrate.
AUTO_MIN, AUTO_MAX, AUTO_START = 8000, 50000, 20000
AUTO_DOWN, AUTO_UP = 5000, 2000  # step after trouble / after a quiet spell
AUTO_QUIET = 180  # s without trouble, at a bitrate actually used, before stepping up
AUTO_BLOCK = 600  # s to stay below a bitrate that failed
AUTO_BUSY = 0.7  # output this share of the target counts as using it
AUTO_TICK = 10  # s between checks
AUTO_RESYNCS = 2  # viewers' resyncs in one tick that count as trouble
# Generated test pictures. The scrolling bars move every pixel predictably,
# like scrolling a page; noise is incompressible, the worst case, and
# videotestsrc only makes ~80 frames/s of it at 1440x900.
BENCH_PATTERNS = {"moving": "pattern=smpte horizontal-speed=12", "noise": "pattern=snow"}

# codec -> (GPU encoder, software encoder or None)
ENCODERS = {"h264": ("vah264enc", "x264enc"), "h265": ("vah265enc", "x265enc"), "av1": ("vaav1enc", None)}
MEDIA_TYPES = {"video/x-h264": "h264", "video/x-h265": "h265", "video/x-av1": "av1"}


def _has(element):
    return element is not None and Gst.ElementFactory.find(element) is not None


def available_codecs(encoder):
    """{codec: "gpu" | "cpu"} for what this machine can encode, given
    config's encoder preference (auto | va | software)."""
    out = {}
    for codec, (va, sw) in ENCODERS.items():
        if encoder != "software" and _has(va):
            out[codec] = "gpu"
        elif encoder != "va" and _has(sw):
            out[codec] = "cpu"
    return out


def cpb_kbits(bitrate):
    """The VA encoders' rate-control buffer (cpb-size, kbit) for a bitrate
    (kbps): half a second, the least they accept. Unset, it's 2 s, which
    lets one busy frame outgrow the coded buffer GStreamer sizes from the
    resolution, and the frame fails. Smaller also means no multi-megabyte frames stalling the stream."""
    return bitrate // 2


def encoder_chain(codec, kind, bitrate, gop):
    """Raw video in, one access unit per buffer out."""
    va = f"name=enc rate-control=cbr bitrate={bitrate} cpb-size={cpb_kbits(bitrate)} key-int-max={gop} ref-frames=1"
    if codec == "h264":
        enc = (f"vah264enc {va} b-frames=0"
               if kind == "gpu" else
               f"x264enc name=enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate} key-int-max={gop} bframes=0")
        return (f"{enc} ! h264parse config-interval=-1 ! "
                f"video/x-h264,stream-format=byte-stream,alignment=au")
    if codec == "h265":
        enc = (f"vah265enc {va} b-frames=0"
               if kind == "gpu" else
               f"x265enc name=enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate} key-int-max={gop}")
        return (f"{enc} ! h265parse config-interval=-1 ! "
                f"video/x-h265,stream-format=byte-stream,alignment=au")
    return (f"vaav1enc {va} ! "
            f"av1parse ! video/x-av1,stream-format=obu-stream,alignment=tu")


def pipeline_description(source, settings, kind, size):
    """size is the captured (w, h), for scaling; without it, no scaling."""
    dims = ""
    if size and settings["scale"] < 1:
        w, h = (max(2, round(n * settings["scale"] / 2) * 2) for n in size)
        dims = f",width={w},height={h}"
    convert = (f"vapostproc ! video/x-raw(memory:VAMemory),format=NV12{dims}" if kind == "gpu" else
               f"videoconvertscale ! video/x-raw,format=I420{dims}")
    fps = settings["fps"]
    return (f"{source} ! queue max-size-buffers=2 leaky=downstream ! "
            f"videorate drop-only=true max-rate={fps} ! {convert} ! "
            f"{encoder_chain(settings['codec'], kind, settings['bitrate'], min(fps, 75) * KEYFRAME_SECONDS)} ! "
            f"appsink name=sink emit-signals=true sync=false max-buffers=8 drop=false")


def codec_string(name, caps, au):
    """The WebCodecs codec string for a keyframe's access unit."""
    if name == "h264":
        sps = _nal(au, lambda b: b & 0x1F == 7, 1)
        return "avc1." + sps[:3].hex() if sps and len(sps) >= 3 else "avc1.42e01f"
    if name == "h265":
        sps = _nal(au, lambda b: (b >> 1) & 0x3F == 33, 2)
        return _hevc_codec(sps) if sps and len(sps) >= 13 else "hev1.1.6.L93.B0"
    return GstPbutils.codec_utils_caps_get_mime_codec(caps) or "av01.0.08M.08"


def _nal(au, is_type, header_len):
    """The payload (emulation prevention removed) of the first NAL unit in
    an Annex B access unit that matches is_type(first header byte)."""
    i = 0
    while True:
        i = au.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= len(au):
            return None
        if is_type(au[i + 3]):
            end = au.find(b"\x00\x00\x01", i + 3)
            raw = au[i + 3 + header_len:end if end >= 0 else len(au)]
            return raw.replace(b"\x00\x00\x03", b"\x00\x00")
        i += 3


def _hevc_codec(sps):
    """hev1.<space><profile>.<compat>.<tier><level>.<constraints> from an
    H.265 SPS (ISO/IEC 14496-15 annex E)."""
    ptl = sps[1:]  # after vps_id / max_sub_layers / temporal_id_nesting
    space = "" if ptl[0] >> 6 == 0 else "ABC"[(ptl[0] >> 6) - 1]
    tier = "H" if ptl[0] & 0x20 else "L"
    profile = ptl[0] & 0x1F
    compat = int(f"{int.from_bytes(ptl[1:5], 'big'):032b}"[::-1], 2)  # bit-reversed
    constraints = ptl[5:11].rstrip(b"\x00")
    level = ptl[11]
    out = f"hev1.{space}{profile}.{compat:x}.{tier}{level}"
    return out + "".join(f".{b:02x}" for b in constraints)


class Viewer:
    def __init__(self, ws):
        self.ws = ws
        self.queue = asyncio.Queue()
        self.need_key = True
        self.config = None
        self.resyncs = 0  # times its queue was dropped for falling behind
        self.resyncs_seen = 0  # resyncs already counted by auto bitrate


class AutoBitrate:
    """The bitrate for "auto": the highest the encoder (and the viewers'
    connections) cope with. Trouble steps it down AUTO_DOWN at once and
    blocks the bitrate that failed for AUTO_BLOCK seconds; AUTO_QUIET
    seconds of trouble-free streaming that actually used the bitrate step it
    up AUTO_UP. Only busy time counts towards that, since an idle desktop
    sends next to nothing and would otherwise creep up to the maximum
    untested, then fail at the first busy scene."""

    def __init__(self, kbps):
        try:
            kbps = int(kbps)
        except (TypeError, ValueError):
            kbps = AUTO_START
        self.kbps = max(AUTO_MIN, min(AUTO_MAX, kbps))
        self.block = None  # (failed kbps, loop time it's blocked until)
        self.busy = 0.0  # s of streaming at AUTO_BUSY of the target since the last change

    def trouble(self, now):
        """Step down; returns the new kbps, or None if already at the minimum."""
        self.block = (self.kbps, now + AUTO_BLOCK)
        self.busy = 0.0
        return self._set(max(AUTO_MIN, self.kbps - AUTO_DOWN))

    def tick(self, now, out_kbps, dt):
        """A quiet AUTO_TICK; returns the new kbps when it's time to step up."""
        if out_kbps >= AUTO_BUSY * self.kbps:
            self.busy += dt
        if self.busy < AUTO_QUIET:
            return None
        self.busy = 0.0
        ceiling = AUTO_MAX
        if self.block and now < self.block[1]:
            ceiling = min(ceiling, self.block[0] - 1000)  # whole Mb/s below the failure
        return self._set(min(ceiling, self.kbps + AUTO_UP))

    def _set(self, kbps):
        if kbps == self.kbps:
            return None
        self.kbps = kbps
        return kbps


class VideoStream:
    def __init__(self, encoder, defaults):
        """defaults: {codec, bitrate, fps, scale} from config.toml, used
        until settings have been saved from the page."""
        self.codecs = available_codecs(encoder)
        if not self.codecs:
            raise RuntimeError(f"no video encoder available (encoder = {encoder})")
        self.settings = self._validate(defaults, {"codec": "h264", "bitrate": 8000, "fps": 60, "scale": 1.0})
        saved = {}
        try:
            saved = json.loads(SETTINGS_FILE.read_text())
            self.settings = self._validate(saved, self.settings)
        except (OSError, ValueError, TypeError, AttributeError):
            pass  # none saved yet, or unreadable: config.toml's defaults
        self.auto = AutoBitrate(saved.get("auto_kbps") if isinstance(saved, dict) else None)
        self._auto_task = None
        # Health: encoder/pipeline errors since the server started.
        self.errors = 0
        self.last_error = None
        self._last_error_at = None
        self.gave_up = False
        self.viewers = set()
        self.state = "stopped"  # stopped | starting | streaming | error
        self.error = None
        self._lock = asyncio.Lock()
        self._pipeline = None
        self._sink = None
        self._cast = None
        self._node = None  # (node id, size) of the portal stream
        self._area = None  # where that stream's screen is on the desktop
        self._stop_timer = None
        self._loop = None
        self._bench = None  # {"pattern", "bitrate", "fps", "owner"} while benchmarking
        self._bench_timer = None
        self._recoveries = []  # loop times of recent rebuilds after an error
        self.frames = 0  # access units produced, ever (see stats())
        self.bytes = 0

    # --- settings ---

    def _validate(self, raw, base):
        """raw's valid fields over base; unknown or bad values keep base's."""
        out = dict(base)
        if raw.get("codec") in CODECS and raw["codec"] in self.codecs:
            out["codec"] = raw["codec"]
        for key, clamp in (("bitrate", clamp_bitrate), ("fps", clamp_fps), ("scale", clamp_scale)):
            if key == "bitrate" and raw.get(key) == "auto":
                out[key] = "auto"
                continue
            try:
                if key in raw and not isinstance(raw[key], bool):
                    out[key] = clamp(raw[key])
            except (ValueError, TypeError):
                pass
        if out["codec"] not in self.codecs:
            out["codec"] = "h264" if "h264" in self.codecs else next(iter(self.codecs))
        return out

    def _settings_message(self):
        return json.dumps({"t": "settings", **self.settings, "auto_kbps": self.auto.kbps,
                           "codecs": self.codecs})

    def _bitrate(self):
        """The bitrate to encode at now, in kbps."""
        return self.auto.kbps if self.settings["bitrate"] == "auto" else self.settings["bitrate"]

    def _save_settings(self):
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = SETTINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({**self.settings, "auto_kbps": self.auto.kbps}))
            os.replace(tmp, SETTINGS_FILE)
        except OSError as err:
            print(f"video: couldn't save settings: {err}", flush=True)

    def _broadcast(self, msg):
        for v in self.viewers:
            v.queue.put_nowait(msg)

    async def apply(self, raw):
        new = self._validate(raw, self.settings)
        if new == self.settings:
            return
        self.settings = new
        self._save_settings()
        self._broadcast(self._settings_message())
        print(f"video: settings {new}", flush=True)
        await self._restart()

    # --- benchmark ---

    async def bench(self, viewer, raw):
        pattern = raw.get("pattern")
        if self._bench_timer:
            self._bench_timer.cancel()
            self._bench_timer = None
        if pattern not in BENCH_PATTERNS:
            if self._bench is not None:
                self._bench = None
                print("video: benchmark over, back to the screen", flush=True)
                await self._restart()
            return
        new = self._validate(raw, self.settings)
        bitrate = self.auto.kbps if new["bitrate"] == "auto" else new["bitrate"]
        self._bench = {"pattern": pattern, "bitrate": bitrate, "fps": new["fps"], "owner": viewer}
        self._bench_timer = self._loop.call_later(
            BENCH_MAX, lambda: asyncio.create_task(self.bench(None, {})))
        await self._restart()

    def stats(self, viewer):
        return json.dumps({"t": "stats", "frames": self.frames, "bytes": self.bytes,
                           "resyncs": viewer.resyncs, "time": self._loop.time()})

    # --- viewers ---

    async def add(self, ws):
        self._loop = asyncio.get_running_loop()
        viewer = Viewer(ws)
        self.viewers.add(viewer)
        if self._stop_timer:
            self._stop_timer.cancel()
            self._stop_timer = None
        viewer.queue.put_nowait(self._settings_message())
        viewer.queue.put_nowait(self._health_message())
        await self._send_state(viewer)
        if self._auto_task is None:
            self._auto_task = asyncio.create_task(self._auto_loop())
        if self.state in ("stopped", "error"):
            asyncio.create_task(self._start())
        else:
            self.request_keyframe()
        return viewer

    def remove(self, viewer):
        self.viewers.discard(viewer)
        if self._bench and self._bench["owner"] is viewer:
            asyncio.create_task(self.bench(None, {}))
        if not self.viewers and self._stop_timer is None:
            self._stop_timer = asyncio.get_running_loop().call_later(
                STOP_GRACE, lambda: asyncio.create_task(self._stop()))

    async def pump(self, viewer):
        """Send queued frames to one viewer until its socket closes."""
        while True:
            item = await viewer.queue.get()
            if isinstance(item, str):
                await viewer.ws.send_str(item)
            else:
                await viewer.ws.send_bytes(item)

    def request_keyframe(self):
        if self._sink is None:
            return
        event = GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
        # Element.send_event routes upstream events out of its sink pads
        # (Pad.send_event would push it the wrong way, into the pad).
        self._sink.send_event(event)

    def _health_message(self):
        now = time.monotonic()
        return json.dumps({
            "t": "health", "errors": self.errors,
            "last_error": self.last_error, "gave_up": self.gave_up,
            "last_error_ago": None if self._last_error_at is None else round(now - self._last_error_at),
        })

    # --- auto bitrate ---

    async def _auto_loop(self):
        """Every AUTO_TICK: steps auto bitrate down for viewers falling
        behind, or up after a busy, trouble-free spell."""
        last_t, last_bytes = self._loop.time(), self.bytes
        while True:
            await asyncio.sleep(AUTO_TICK)
            now = self._loop.time()
            dt, out_kbps = now - last_t, (self.bytes - last_bytes) * 8 / max(now - last_t, 1e-3) / 1000
            last_t, last_bytes = now, self.bytes
            resyncs = 0
            for v in self.viewers:
                resyncs += v.resyncs - v.resyncs_seen
                v.resyncs_seen = v.resyncs
            if self.settings["bitrate"] != "auto" or self.state != "streaming" or self._bench:
                continue
            if resyncs >= AUTO_RESYNCS:
                new, why = self.auto.trouble(now), f"{resyncs} resyncs (a viewer falling behind)"
            else:
                new, why = self.auto.tick(now, out_kbps, dt), "quiet and busy"
            if new:
                await self._auto_changed(why, live=True)

    async def _auto_changed(self, why, live):
        """auto.kbps changed: apply it (live, on the running encoder, when
        it allows that), save it and tell the viewers."""
        print(f"video: auto bitrate {self.auto.kbps}kbps ({why})", flush=True)
        self._save_settings()
        self._broadcast(self._settings_message())
        if not live:
            return
        enc = self._pipeline.get_by_name("enc") if self._pipeline else None
        spec = enc.find_property("bitrate") if enc else None
        if spec and spec.flags & Gst.PARAM_MUTABLE_PLAYING:
            # The encoder ignores a CPB under half a second of the bitrate
            # it has when it reconfigures (next frame), so going up the
            # CPB goes first, going down the bitrate does.
            props = [("bitrate", self.auto.kbps)]
            if enc.find_property("cpb-size"):
                props.append(("cpb-size", cpb_kbits(self.auto.kbps)))
                if self.auto.kbps > enc.get_property("bitrate"):
                    props.reverse()
            for name, value in props:
                enc.set_property(name, value)
        else:
            await self._restart()

    async def _send_state(self, viewer=None):
        msg = json.dumps({"t": "state", "state": self.state, "error": self.error})
        for v in [viewer] if viewer else list(self.viewers):
            v.queue.put_nowait(msg)

    # --- pipeline ---

    async def _start(self):
        async with self._lock:
            if self.state in ("starting", "streaming"):
                return
            self.state, self.error = "starting", None
            await self._send_state()
            cast = ScreenCast()
            try:
                fd, node_id, area = await cast.start()
            except Exception as err:
                await cast.close()
                return await self._fail(f"screen capture: {err}")
            size = (area["w"], area["h"]) if area else None
            self._cast, self._node, self._area = cast, (node_id, size), area
            try:
                self._launch(fd)
            except Exception as err:
                await self._teardown()
                return await self._fail(f"video pipeline: {err}")
            self.state = "streaming"
            await self._send_state()
            if self.gave_up:
                self.gave_up = False
                self._broadcast(self._health_message())
            asyncio.create_task(self._watch_portal(cast))

    async def repick(self):
        """Forget the saved screen choice and share again: KDE asks which
        screen, on the desktop."""
        forget_token()
        async with self._lock:
            await self._teardown()
            self.state, self.error = "stopped", None
        if self.viewers:
            await self._start()

    async def _restart(self):
        """Rebuild the pipeline with the current settings, keeping the
        portal session."""
        async with self._lock:
            if self.state != "streaming" or self._cast is None:
                return  # not running: the next start picks the settings up
            await self._stop_pipeline()
            try:
                self._launch(None if self._bench else await self._cast.open_remote())
            except Exception as err:
                await self._teardown()
                return await self._fail(f"video pipeline: {err}")

    def _launch(self, fd):
        """Start the pipeline on the portal's PipeWire fd, or on a test
        picture while benchmarking (fd None)."""
        node_id, size = self._node
        settings = {**self.settings, "bitrate": self._bitrate()}
        if self._bench:
            w, h = size or (1920, 1080)
            settings.update(bitrate=self._bench["bitrate"], fps=self._bench["fps"])
            source = (f"videotestsrc is-live=true {BENCH_PATTERNS[self._bench['pattern']]} ! "
                      f"video/x-raw,width={w},height={h},framerate={settings['fps']}/1")
            what = f"benchmark ({self._bench['pattern']})"
        else:
            # No always-copy: KWin hands over DMA-BUFs, which that mode can't
            # copy (every buffer failed with "gst_buffer_insert_memory:
            # assertion 'mem != NULL'"). vapostproc imports them directly.
            source = (f"pipewiresrc fd={fd} path={node_id} do-timestamp=true "
                      f"keepalive-time=1000")
            what = f"PipeWire node {node_id}"
        kind = self.codecs[settings["codec"]]
        try:
            self._run_pipeline(pipeline_description(source, settings, kind, size))
        except Exception:
            if fd is not None:
                os.close(fd)
            raise
        auto = " (auto)" if self.settings["bitrate"] == "auto" and not self._bench else ""
        print(f"video: streaming {what}: {settings['codec']} ({kind}) "
              f"{settings['bitrate']}kbps{auto}, up to {settings['fps']}fps, scale {settings['scale']}", flush=True)

    def _run_pipeline(self, description):
        pipeline = Gst.parse_launch(description)
        sink = pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("couldn't start the GStreamer pipeline")
        self._pipeline, self._sink = pipeline, sink
        threading.Thread(target=self._watch_bus, args=(pipeline,), daemon=True).start()

    def _watch_bus(self, pipeline):
        bus = pipeline.get_bus()
        while self._pipeline is pipeline:
            msg = bus.timed_pop_filtered(200 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if msg is None:
                continue
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                print(f"video: pipeline error: {err.message} ({debug})", flush=True)
                text = err.message
            else:
                text = "the screen capture ended"
            asyncio.run_coroutine_threadsafe(self._pipeline_ended(pipeline, text), self._loop)
            return

    async def _pipeline_ended(self, pipeline, text):
        if self._pipeline is not pipeline:
            return
        self.errors += 1
        self.last_error, self._last_error_at = text, time.monotonic()
        if self.settings["bitrate"] == "auto" and not self._bench and self.auto.trouble(self._loop.time()):
            # Not live: the rebuild below picks the new bitrate up.
            await self._auto_changed("pipeline error", live=False)
        if not await self._recover(pipeline, text):
            if self._pipeline is pipeline or self._pipeline is None:
                await self._teardown()
                await self._fail(text)
                self.gave_up = True
        self._broadcast(self._health_message())

    async def _recover(self, pipeline, text):
        """Rebuild a failed pipeline on the same portal session, if anyone
        is watching and it hasn't failed too often lately. True if a new
        pipeline is running (or another one replaced it meanwhile)."""
        now = self._loop.time()
        self._recoveries = [t for t in self._recoveries if now - t < RECOVER_WINDOW]
        if not self.viewers or self._bench or len(self._recoveries) >= RECOVER_MAX:
            return False
        async with self._lock:
            if self._pipeline is not pipeline:
                return True  # restarted or stopped while waiting for the lock
            if self._cast is None:
                return False
            self._recoveries.append(now)
            print(f"video: {text}; rebuilding the pipeline "
                  f"({len(self._recoveries)}/{RECOVER_MAX} in {RECOVER_WINDOW}s)", flush=True)
            await self._stop_pipeline()
            try:
                self._launch(await self._cast.open_remote())
            except Exception as err:
                print(f"video: rebuild failed: {err}", flush=True)
                return False
        return True

    async def _watch_portal(self, cast):
        await cast.closed.wait()
        if self._cast is cast:
            await self._teardown()
            await self._fail("screen sharing was stopped on the desktop")

    def _on_sample(self, sink):
        # GStreamer streaming thread: copy out and hand over to the event loop.
        sample = sink.emit("pull-sample")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        data = bytes(info.data)
        buf.unmap(info)
        key = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
        pts = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else 0
        caps = sample.get_caps()
        try:
            self._loop.call_soon_threadsafe(self._dispatch, sink, data, key, pts // 1000, caps)
        except RuntimeError:
            return Gst.FlowReturn.FLUSHING  # the event loop has closed: shutting down
        return Gst.FlowReturn.OK

    def _dispatch(self, sink, data, key, pts_us, caps):
        if sink is not self._sink:
            return  # a last frame from a pipeline that's just been replaced
        frame = HEADER.pack(1 if key else 0, pts_us) + data
        self.frames += 1
        self.bytes += len(frame)
        config = None
        if key:
            s = caps.get_structure(0)
            name = MEDIA_TYPES.get(s.get_name(), "h264")
            config = json.dumps({"t": "config", "codec": codec_string(name, caps, data), "name": name,
                                 "width": s.get_value("width"), "height": s.get_value("height"),
                                 "area": self._area})
        want_key = False
        for v in self.viewers:
            if v.queue.qsize() > VIEWER_QUEUE_MAX:
                while not v.queue.empty():
                    v.queue.get_nowait()
                v.need_key = True
                v.resyncs += 1
                want_key = True
            if v.need_key and not key:
                continue
            if key and v.config != config:
                v.queue.put_nowait(config)
                v.config = config
            v.need_key = False
            v.queue.put_nowait(frame)
        if want_key:
            self.request_keyframe()

    async def _fail(self, text):
        print(f"video: {text}", flush=True)
        self.state, self.error = "error", text
        await self._send_state()

    async def _stop_pipeline(self):
        pipeline, self._pipeline, self._sink = self._pipeline, None, None
        if pipeline is not None:
            await asyncio.to_thread(pipeline.set_state, Gst.State.NULL)
        for v in self.viewers:
            v.need_key = True
            v.config = None

    async def _teardown(self):
        # A benchmark ends with the stream (an error, the last viewer
        # leaving), so the next start captures the screen again.
        self._bench = None
        if self._bench_timer:
            self._bench_timer.cancel()
            self._bench_timer = None
        await self._stop_pipeline()
        cast, self._cast = self._cast, None
        if cast is not None:
            await cast.close()

    def stop_now(self):
        """Stop the pipeline synchronously, at exit (registered with atexit
        in __main__). A pipeline left running calls _on_sample from its
        streaming thread while Python is finalizing, which aborts the
        process with a core dump; aiohttp's own cleanup doesn't get that
        far on SIGTERM, since it cancels every task at once."""
        pipeline, self._pipeline, self._sink = self._pipeline, None, None
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)

    async def _stop(self):
        self._stop_timer = None
        if self.viewers:
            return
        async with self._lock:
            await self._teardown()
            self.state, self.error = "stopped", None
            print("video: stopped (no viewers)", flush=True)
