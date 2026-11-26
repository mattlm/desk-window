"""The uinput devices: an absolute pointer, a relative mouse and a keyboard.

Creating them needs write access to /dev/uinput (see the README). KWin
picks them up like any newly plugged-in mouse and keyboard.

The pointer is absolute, like a VM's USB tablet: libinput sees ABS_X/ABS_Y
plus mouse buttons and no pen/touch tools, so it's a pointer whose position
KWin maps across the whole desktop. The page sends 0-1 coordinates within
its frame, so they map straight onto the axis range.

Absolute motion carries no relative delta, though, and games that turn the
camera while a button is held (anything using SDL's relative mode, many
Wine games) lock the pointer and read only relative motion - so a drag moved the
camera a little until the lock kicked in, then stopped. While a button is
held the page sends movement deltas instead, through a second, ordinary
relative mouse. Its acceleration is switched to flat in KWin so a drag moves
the cursor as far as the browser's pointer moved.
"""

import glob
import subprocess
import threading
import time

from evdev import AbsInfo, UInput
from evdev import ecodes as e

from . import keymap

VENDOR = 0xF00D
POINTER = ("Desk Window pointer", 0x0001)
KEYBOARD = ("Desk Window keyboard", 0x0002)
MOUSE = ("Desk Window mouse", 0x0003)  # relative, for drags
REL_MAX = 2000  # px per message, each axis

AXIS_MAX = 65535
WHEEL_HI_RES = 120  # REL_WHEEL_HI_RES units per notch
# back/forward are the thumb buttons; BTN_SIDE/BTN_EXTRA are what a real
# mouse sends for them, so apps and games see mouse 4/5.
BUTTONS = {
    "left": e.BTN_LEFT, "right": e.BTN_RIGHT, "middle": e.BTN_MIDDLE,
    "back": e.BTN_SIDE, "forward": e.BTN_EXTRA,
}


class Devices:
    def __init__(self):
        name, product = POINTER
        self.pointer = UInput(
            {
                e.EV_KEY: sorted(BUTTONS.values()),
                e.EV_ABS: [
                    (e.ABS_X, AbsInfo(0, 0, AXIS_MAX, 0, 0, 0)),
                    (e.ABS_Y, AbsInfo(0, 0, AXIS_MAX, 0, 0, 0)),
                ],
                e.EV_REL: [e.REL_WHEEL, e.REL_HWHEEL, e.REL_WHEEL_HI_RES, e.REL_HWHEEL_HI_RES],
            },
            name=name, vendor=VENDOR, product=product,
        )
        # libinput only treats a relative device as a mouse if it has
        # buttons, so it gets them too, though they're never pressed - the
        # pointer's buttons and this mouse's motion share the seat's cursor.
        name, product = MOUSE
        self.mouse = UInput({e.EV_KEY: sorted(BUTTONS.values()), e.EV_REL: [e.REL_X, e.REL_Y]},
                            name=name, vendor=VENDOR, product=product)
        threading.Thread(target=_flat_accel_in_kwin, args=(name,), daemon=True).start()
        name, product = KEYBOARD
        self.keyboard = UInput({e.EV_KEY: keymap.ALL_KEYS}, name=name, vendor=VENDOR, product=product)
        # Leftover sub-notch wheel movement, so a trackpad's small deltas
        # still add up to whole-notch REL_WHEEL events for apps that only
        # read those.
        self._wheel_rest = [0, 0]

    def close(self):
        self.pointer.close()
        self.mouse.close()
        self.keyboard.close()

    def rel(self, dx, dy):
        self.mouse.write(e.EV_REL, e.REL_X, max(-REL_MAX, min(REL_MAX, int(dx))))
        self.mouse.write(e.EV_REL, e.REL_Y, max(-REL_MAX, min(REL_MAX, int(dy))))
        self.mouse.syn()

    def move(self, x, y):
        self.pointer.write(e.EV_ABS, e.ABS_X, round(_clamp01(x) * AXIS_MAX))
        self.pointer.write(e.EV_ABS, e.ABS_Y, round(_clamp01(y) * AXIS_MAX))
        self.pointer.syn()

    def button(self, button, down):
        self.pointer.write(e.EV_KEY, BUTTONS[button], 1 if down else 0)
        self.pointer.syn()

    def wheel(self, dx, dy):
        """dx/dy in notches, browser sign (+dy = scroll down, +dx = right)."""
        for i, (amount, hi, lo, sign) in enumerate([
            (dy, e.REL_WHEEL_HI_RES, e.REL_WHEEL, -1),
            (dx, e.REL_HWHEEL_HI_RES, e.REL_HWHEEL, 1),
        ]):
            units = round(amount * WHEEL_HI_RES)
            if not units:
                continue
            self.pointer.write(e.EV_REL, hi, sign * units)
            self._wheel_rest[i] += units
            notches = int(self._wheel_rest[i] / WHEEL_HI_RES)
            if notches:
                self._wheel_rest[i] -= notches * WHEEL_HI_RES
                self.pointer.write(e.EV_REL, lo, sign * notches)
        self.pointer.syn()

    def key(self, code, down):
        self.keyboard.write(e.EV_KEY, code, 1 if down else 0)
        self.keyboard.syn()


def _clamp01(v):
    return 0.0 if v < 0 else 1.0 if v > 1 else float(v)


def _flat_accel_in_kwin(name):
    """Turn off pointer acceleration for one device, through KWin's D-Bus
    API (the same per-device switch as System Settings > Mouse). Runs on a
    thread: the device's event node takes a moment to appear, and KWin a
    moment more to pick it up. Best effort: without it, drags still work,
    just with the desktop's acceleration applied."""
    for _ in range(50):
        for f in glob.glob("/sys/class/input/event*/device/name"):
            with open(f) as fh:
                if fh.read().strip() != name:
                    continue
            ev = f.split("/")[4]
            for _ in range(20):
                try:
                    done = subprocess.run(
                        ["busctl", "--user", "set-property", "org.kde.KWin",
                         f"/org/kde/KWin/InputDevice/{ev}", "org.kde.KWin.InputDevice",
                         "pointerAccelerationProfileFlat", "b", "true"],
                        capture_output=True)
                except FileNotFoundError:
                    print("warning: busctl not found, so drags keep pointer acceleration", flush=True)
                    return
                if done.returncode == 0:
                    return
                time.sleep(0.1)
            print(f"warning: couldn't set flat acceleration for {name} in KWin", flush=True)
            return
        time.sleep(0.1)
    print(f"warning: couldn't find {name}'s event node", flush=True)
