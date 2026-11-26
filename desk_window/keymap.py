"""Browser key identifiers -> evdev keycodes.

Desktop browsers send KeyboardEvent.code, the physical key position, which
maps one-to-one onto evdev codes whatever the browser machine's layout is.
Phone soft keyboards mostly don't (code is "", key "Unidentified"), so the
page sends what they type as text instead, typed here on US key positions
(right for US-style layouts on the desktop). A few named keys like
Backspace do come through with a usable KeyboardEvent.key but no code.
"""

from evdev import ecodes as e

CODES = {}
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    CODES[f"Key{_c}"] = getattr(e, f"KEY_{_c}")
for _d in "0123456789":
    CODES[f"Digit{_d}"] = getattr(e, f"KEY_{_d}")
    CODES[f"Numpad{_d}"] = getattr(e, f"KEY_KP{_d}")
for _n in range(1, 25):
    CODES[f"F{_n}"] = getattr(e, f"KEY_F{_n}")
CODES.update({
    "Escape": e.KEY_ESC, "Minus": e.KEY_MINUS, "Equal": e.KEY_EQUAL,
    "Backspace": e.KEY_BACKSPACE, "Tab": e.KEY_TAB, "Enter": e.KEY_ENTER,
    "BracketLeft": e.KEY_LEFTBRACE, "BracketRight": e.KEY_RIGHTBRACE,
    "Semicolon": e.KEY_SEMICOLON, "Quote": e.KEY_APOSTROPHE, "Backquote": e.KEY_GRAVE,
    "Backslash": e.KEY_BACKSLASH, "IntlBackslash": e.KEY_102ND,
    "Comma": e.KEY_COMMA, "Period": e.KEY_DOT, "Slash": e.KEY_SLASH, "Space": e.KEY_SPACE,
    "IntlRo": e.KEY_RO, "IntlYen": e.KEY_YEN,
    "ShiftLeft": e.KEY_LEFTSHIFT, "ShiftRight": e.KEY_RIGHTSHIFT,
    "ControlLeft": e.KEY_LEFTCTRL, "ControlRight": e.KEY_RIGHTCTRL,
    "AltLeft": e.KEY_LEFTALT, "AltRight": e.KEY_RIGHTALT,
    "MetaLeft": e.KEY_LEFTMETA, "MetaRight": e.KEY_RIGHTMETA,
    "OSLeft": e.KEY_LEFTMETA, "OSRight": e.KEY_RIGHTMETA,  # older Firefox
    "ContextMenu": e.KEY_COMPOSE,
    "CapsLock": e.KEY_CAPSLOCK, "NumLock": e.KEY_NUMLOCK, "ScrollLock": e.KEY_SCROLLLOCK,
    "PrintScreen": e.KEY_SYSRQ, "Pause": e.KEY_PAUSE,
    "Insert": e.KEY_INSERT, "Delete": e.KEY_DELETE, "Home": e.KEY_HOME, "End": e.KEY_END,
    "PageUp": e.KEY_PAGEUP, "PageDown": e.KEY_PAGEDOWN,
    "ArrowUp": e.KEY_UP, "ArrowDown": e.KEY_DOWN, "ArrowLeft": e.KEY_LEFT, "ArrowRight": e.KEY_RIGHT,
    "NumpadAdd": e.KEY_KPPLUS, "NumpadSubtract": e.KEY_KPMINUS,
    "NumpadMultiply": e.KEY_KPASTERISK, "NumpadDivide": e.KEY_KPSLASH,
    "NumpadDecimal": e.KEY_KPDOT, "NumpadEnter": e.KEY_KPENTER, "NumpadEqual": e.KEY_KPEQUAL,
    "AudioVolumeMute": e.KEY_MUTE, "AudioVolumeDown": e.KEY_VOLUMEDOWN, "AudioVolumeUp": e.KEY_VOLUMEUP,
    "MediaPlayPause": e.KEY_PLAYPAUSE, "MediaStop": e.KEY_STOPCD,
    "MediaTrackNext": e.KEY_NEXTSONG, "MediaTrackPrevious": e.KEY_PREVIOUSSONG,
})

# KeyboardEvent.key for named keys that arrive without a code (soft keyboards).
NAMED = {
    "Backspace": e.KEY_BACKSPACE, "Enter": e.KEY_ENTER, "Tab": e.KEY_TAB, "Escape": e.KEY_ESC,
    "Delete": e.KEY_DELETE, "ArrowUp": e.KEY_UP, "ArrowDown": e.KEY_DOWN,
    "ArrowLeft": e.KEY_LEFT, "ArrowRight": e.KEY_RIGHT, "Home": e.KEY_HOME, "End": e.KEY_END,
}

# char -> (evdev code, needs shift), US key positions.
CHARS = {" ": (e.KEY_SPACE, False), "\n": (e.KEY_ENTER, False), "\t": (e.KEY_TAB, False)}
for _c in "abcdefghijklmnopqrstuvwxyz":
    CHARS[_c] = (getattr(e, f"KEY_{_c.upper()}"), False)
    CHARS[_c.upper()] = (getattr(e, f"KEY_{_c.upper()}"), True)
for _plain, _shifted, _code in [
    ("1", "!", e.KEY_1), ("2", "@", e.KEY_2), ("3", "#", e.KEY_3), ("4", "$", e.KEY_4),
    ("5", "%", e.KEY_5), ("6", "^", e.KEY_6), ("7", "&", e.KEY_7), ("8", "*", e.KEY_8),
    ("9", "(", e.KEY_9), ("0", ")", e.KEY_0), ("-", "_", e.KEY_MINUS), ("=", "+", e.KEY_EQUAL),
    ("[", "{", e.KEY_LEFTBRACE), ("]", "}", e.KEY_RIGHTBRACE), ("\\", "|", e.KEY_BACKSLASH),
    (";", ":", e.KEY_SEMICOLON), ("'", '"', e.KEY_APOSTROPHE), ("`", "~", e.KEY_GRAVE),
    (",", "<", e.KEY_COMMA), (".", ">", e.KEY_DOT), ("/", "?", e.KEY_SLASH),
]:
    CHARS[_plain] = (_code, False)
    CHARS[_shifted] = (_code, True)

ALL_KEYS = sorted(set(CODES.values()) | set(NAMED.values()) | {c for c, _ in CHARS.values()})
