"""Lightbar speedline: map telemetry to an RGB colour.

This is not a trigger effect (no force feedback), but it rides the same HID
output report the trigger writer builds, into bytes that are independent of the
rumble bits. main.DualSense._build() stamps the colour returned here.

Colour by driving state (priority high to low):
    ABS lockup  -> blinking yellow (full brightness; a warning)
    handbrake   -> blue
    brake       -> red
    throttle    -> green
    coasting    -> off (the zero of the proportional ramp)

Brightness scales with pedal travel, so lifting off a pedal fades its colour to
black naturally.
"""
import time

from .triggers import abs_active

# Fixed palette (RGB 0-255). Intensity is scaled at runtime by _scale().
_GREEN  = (0, 255, 0)
_RED    = (255, 0, 0)
_BLUE   = (0, 0, 255)
_YELLOW = (255, 200, 0)
OFF     = (0, 0, 0)


def _scale(rgb, brightness, pedal=255):
    """rgb scaled by the master brightness and the pedal position (both 0-255)."""
    f = (brightness / 255) * (pedal / 255)
    return tuple(int(c * f) for c in rgb)


def compute(t, s):
    """Return (r, g, b) for the current frame, or OFF when dark."""
    if not t["on"]:
        return OFF
    if abs_active(t, s):
        on_phase = int(time.monotonic() * s.lightbar_abs_blink_hz * 2) % 2 == 0
        return _scale(_YELLOW, s.lightbar_brightness) if on_phase else OFF
    if t["handbrake"] > 0:
        return _scale(_BLUE, s.lightbar_brightness, t["handbrake"])
    if t["brake"] > 0:
        return _scale(_RED, s.lightbar_brightness, t["brake"])
    if t["accel"] > 0:
        return _scale(_GREEN, s.lightbar_brightness, t["accel"])
    return OFF
