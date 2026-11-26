"""config.toml loader. Every key is optional; see config.example.toml.

The file is looked for at --config, then $DESK_WINDOW_CONFIG, then
$XDG_CONFIG_HOME/desk-window/config.toml (~/.config/desk-window/config.toml).
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


CODECS = ("h264", "h265", "av1")


@dataclass(frozen=True)
class Config:
    screenshots_enabled: bool = True
    screenshot_interval_ms: int = 1000
    video_enabled: bool = True
    video_encoder: str = "auto"  # auto | va | software
    # Defaults for the stream's settings, until someone changes them from
    # the page (which then sticks - see video.py).
    video_codec: str = "h264"
    video_bitrate_kbps: int = 8000
    video_fps: int = 60
    video_scale: float = 1.0


def default_path():
    if os.environ.get("DESK_WINDOW_CONFIG"):
        return Path(os.environ["DESK_WINDOW_CONFIG"])
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "desk-window" / "config.toml"


def load(path=None):
    path = Path(path) if path else default_path()
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        return Config()
    shots = raw.get("screenshots", {})
    video = raw.get("video", {})
    encoder = str(video.get("encoder", "auto"))
    if encoder not in ("auto", "va", "software"):
        raise ValueError(f"video.encoder must be auto, va or software, not {encoder!r}")
    codec = str(video.get("codec", "h264"))
    if codec not in CODECS:
        raise ValueError(f"video.codec must be one of {', '.join(CODECS)}, not {codec!r}")
    return Config(
        screenshots_enabled=bool(shots.get("enabled", True)),
        screenshot_interval_ms=max(250, int(shots.get("interval_ms", 1000))),
        video_enabled=bool(video.get("enabled", True)),
        video_encoder=encoder,
        video_codec=codec,
        video_bitrate_kbps=clamp_bitrate(video.get("bitrate_kbps", 8000)),
        video_fps=clamp_fps(video.get("fps", 60)),
        video_scale=clamp_scale(video.get("scale", 1.0)),
    )


def clamp_bitrate(v):
    return max(500, min(50000, int(v)))


def clamp_fps(v):
    # 240 = no real cap (the page's "Max"): capture never outruns the display.
    return max(1, min(240, int(v)))


def clamp_scale(v):
    return max(0.25, min(1.0, float(v)))
