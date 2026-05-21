"""DualSense adaptive trigger effects.

  TriggerAnimations — every effect (ABS, gear shift, rev limiter, resistance...).
                      Owns timing state for effects that span frames.
  Trigger           — one trigger's priority chain (config + wall hysteresis).
  Controller        — builds L2 / R2 and produces a frame for each per tick.
"""

import time

# --- Raw mode bytes ---
M_OFF      = 0x05
M_RIGID    = 0x01
M_PULSE    = 0x06
M_FEEDBACK = 0x21  # MultiplePositionFeedback — per-zone static strength
M_PULSE_AB = 0x26  # Pulse_AB — per-zone strength + rhythmic kickback
RAW_MAX = 255


def _clamp(v, hi=RAW_MAX):
    return max(0, min(hi, round(v)))


# --- Effect primitives (raw HID frames) -----------------------------------

def off():
    return (M_OFF, ())

def rigid(force):
    return (M_RIGID, (0, _clamp(force)))

def vibration(freq, amp):
    return (M_PULSE, (_clamp(freq), _clamp(amp)))

def vibration_wall(amp, freq, wall_zones):
    """Pulse_AB: lower zones buzz at `amp` (1-8), top `wall_zones` stay maxed."""
    a = max(1, min(8, int(amp)))
    w = max(1, min(9, int(wall_zones)))
    zones = [a] * (10 - w) + [8] * w
    active = strength = 0
    for i, s in enumerate(zones):
        active |= 1 << i
        strength |= (s - 1) << (3 * i)
    return (M_PULSE_AB, (
        active & 0xFF, (active >> 8) & 0xFF,
        strength & 0xFF, (strength >> 8) & 0xFF, (strength >> 16) & 0xFF, (strength >> 24) & 0xFF,
        _clamp(freq), 0, 0, 0,
    ))

def feedback(zones):
    """MultiplePositionFeedback: 10 per-zone strengths (0-8)."""
    active = force = 0
    for i, s in enumerate(zones[:10]):
        s = max(0, min(8, int(s)))
        if s:
            active |= 1 << i
            force |= (s - 1) << (3 * i)
    return (M_FEEDBACK, (
        active & 0xFF, (active >> 8) & 0xFF,
        force & 0xFF, (force >> 8) & 0xFF, (force >> 16) & 0xFF, (force >> 24) & 0xFF,
        0, 0, 0, 0,
    ))


# --- Helpers --------------------------------------------------------------
# Forza drive_train enum -> wheels that receive engine torque.
DRIVEN_WHEELS = {0: ("fl", "fr"), 1: ("rl", "rr"), 2: ("fl", "fr", "rl", "rr")}

def _amp_to_strength(amp_byte):
    return max(1, min(8, (max(0, int(amp_byte)) // 32) + 1))

def _max_slip(t, prefix, wheels=("fl", "fr", "rl", "rr")):
    return max(abs(t[f"{prefix}_{w}"]) for w in wheels)

def abs_active(t, s):
    """True when tyres are locking under braking (where ABS would intervene).
    No enable_abs gate, so the lightbar warning can be driven independently of
    the trigger rumble toggle that abs_pulse() checks."""
    if t["brake"] < s.abs_brake_threshold or t["speed"] < s.abs_min_speed_kmh:
        return False
    return (_max_slip(t, "tire_slip_ratio") >= s.abs_slip_ratio_threshold
            or _max_slip(t, "tire_combined_slip") >= s.abs_combined_slip_threshold)

def _ramp(value, deadzone, baseline, max_force, curve, ceiling):
    """deadzone..ceiling -> baseline..max_force, curve = exponent."""
    if value < deadzone:
        return baseline
    r = min(1.0, (value - deadzone) / max(ceiling - deadzone, 1))
    return baseline + (max_force - baseline) * (r ** curve)

def _wall_state(value, engaged, engage_at, release_at):
    """Hysteresis: enter wall at >= engage_at, leave at < release_at."""
    return value >= release_at if engaged else value >= engage_at

def build_wall(zones):
    """Static firmware wall — top `zones` (1-9) maxed. Built once at startup."""
    n = max(1, min(9, int(zones)))
    return feedback([0] * (10 - n) + [8] * n)

def build_brake_walls(static_at, force, wall_zones):
    """End wall (top `wall_zones`) plus a static wall from brake byte `static_at` down.

    From `static_at` to the bottom of travel every zone holds `force` (a 0-255 byte
    mapped to strength) so the resistance never lightens again past the threshold; the
    top `wall_zones` stay maxed as the end wall. Firmware-held, so a fast stab can't
    skip it."""
    n = max(1, min(9, int(wall_zones)))
    strength = _amp_to_strength(force)
    start = min(9, int(static_at) * 10 // 256)
    zones = [strength if i >= start else 0 for i in range(10)]
    for i in range(10 - n, 10):
        zones[i] = 8
    return feedback(zones)


# --- Animations -----------------------------------------------------------

class TriggerAnimations:
    """Every trigger effect lives here. Methods return an HID frame or None."""

    def __init__(self):
        self._prev_gear = None
        self._shift_until = 0.0
        self._rev_until = 0.0

    def arm_shift(self, t, s, now):
        gear = t["gear"]
        if self._prev_gear is not None and gear != self._prev_gear:
            self._shift_until = now + s.gear_shift_duration_ms / 1000.0
        self._prev_gear = gear
        

    def shift_burst(self, s, now, pedal, wall_engage_at):
        if now >= self._shift_until:
            return None
        # Wall kickback when pedal is deep past the wall, else plain buzz.
        if pedal >= (wall_engage_at + RAW_MAX) // 2:
            return vibration_wall(_amp_to_strength(s.gear_shift_amp), s.gear_shift_freq, s.wall_zones)
        return vibration(s.gear_shift_freq, s.gear_shift_amp)

    def rev_buzz(self, t, s, now):
        # Brief hold so rpm bouncing against the limit doesn't stutter.
        if not s.enable_rev_limiter:
            return None
        if t["accel"] >= s.accel_deadzone:
            max_rpm = t["max_rpm"]
            rpm_r = t["rpm"] / max_rpm if max_rpm > 0 else 0.0
            if rpm_r > s.rev_limit_ratio:
                self._rev_until = now + s.rev_limit_hold_ms / 1000.0
        if now < self._rev_until:
            return vibration(s.rev_limit_freq, s.rev_limit_amp)
        return None

    def wheelspin_buzz(self, t, s, now):
        # Per-surface R2 buzz when driven wheels spin faster than the road.
        if not s.enable_wheelspin_buzz:
            return None
        # Need real throttle input above 10 km/h; below that launch-spin dominates.
        if t["speed"] < 10.0 or t["accel"] < s.accel_deadzone:
            return None
        # Positive slip only. Negative = locked wheels (handbrake/ABS), not wheelspin.
        wheels = DRIVEN_WHEELS.get(t["drive_train"], ("fl", "fr", "rl", "rr"))
        if max(t[f"tire_slip_ratio_{w}"] for w in wheels) < 1.2:
            return None
        # Surface profile: water halves amp, off-road gets a thumpier buzz.
        if any(t[f"wheel_in_puddle_depth_{w}"] > 0.0 for w in wheels):
            return vibration(100, max(1, s.wheelspin_amp // 2))
        rumble = max(abs(t[f"surface_rumble_{w}"]) for w in wheels)
        if rumble > 0.30:        # gravel / rocks
            return vibration(20, 15)
        if rumble > 0.10:        # dirt / loose tarmac
            return vibration(60, 8)
        return vibration(100, s.wheelspin_amp)  # tarmac

    def abs_pulse(self, t, s):
        if not s.enable_abs or not abs_active(t, s):
            return None
        return vibration(s.abs_freq, s.abs_amp)

    def brake_resistance(self, t, s):
        handbrake = s.enable_handbrake_bonus and t["handbrake"]
        if not s.enable_brake_resistance:
            return rigid(s.handbrake_bonus) if handbrake else off()
        force = _ramp(t["brake"], s.brake_deadzone, s.brake_baseline_force,
                      s.brake_max_force, s.brake_curve, s.brake_wall_engage_at)
        if handbrake:
            force += s.handbrake_bonus
        return rigid(force)

    def throttle_ramp(self, t, s):
        if not s.enable_throttle_resistance:
            return off()
        return rigid(_ramp(t["accel"], s.accel_deadzone, s.throttle_baseline_force,
                           s.throttle_max_force, s.throttle_curve, s.throttle_wall_engage_at))

    def surface_rumble(self, t, s):
        # Ambient off-road texture on both triggers; replaces flat resistance.
        if not s.enable_surface_rumble:
            return None
        rumble = _max_slip(t, "surface_rumble")
        if rumble < s.surface_rumble_min:
            return None
        return vibration(s.surface_rumble_freq, rumble * s.surface_rumble_gain)


# --- Controller -----------------------------------------------------------

class Controller:
    """Produces (L2, R2) frames per tick.

    Each chain returns the FIRST non-empty effect; later items are masked.
    Order is hand-tuned so the "loudest" / most informative effect wins.

    L2 priority (top wins):
        1. Gear shift thump    - one-shot burst on every shift, brief
        2. ABS pulse           - tire lockup buzz under hard braking
        3. Firmware end wall   - hard wall near 100% travel (hysteresis)
        4. Static brake wall   - optional fixed wall at brake_static_wall_at
        5. Surface rumble      - off-road texture buzz; replaces resistance
        6. Brake resistance    - default rigid ramp 0..max_force

    R2 priority (top wins):
        1. Gear shift thump    - one-shot burst on every shift, brief
        2. Rev limiter buzz    - rpm/max_rpm >= rev_limit_ratio
        3. Wheelspin buzz      - driven wheels slipping (surface-aware)
        4. Firmware end wall   - hard wall near 100% travel (hysteresis)
        5. Surface rumble      - off-road texture buzz; replaces resistance
        6. Throttle resistance - default rigid ramp 0..max_force
    """

    def __init__(self, settings):
        self.anim = TriggerAnimations()
        self.wall = build_wall(settings.wall_zones)
        self._l2_in_wall = False
        self._r2_in_wall = False

    def update(self, t, s):
        if not t["on"]:
            return off(), off()
        now = time.monotonic()
        if s.enable_gear_shift or s.enable_gear_shift_brake:
            self.anim.arm_shift(t, s, now)
        return self.L2(t, s, now), self.R2(t, s, now)

    def L2(self, t, s, now):
        brake = t["brake"]

        # 1. Gear shift thump - brief burst on shift, masks everything below
        if s.enable_gear_shift_brake:
            shift = self.anim.shift_burst(s, now, brake, s.brake_wall_engage_at)
            if shift:
                return shift

        # 2. ABS pulse - tire lockup under hard braking
        pulse = self.anim.abs_pulse(t, s)
        if pulse:
            return pulse

        # 3. Firmware end wall - hard wall near 100% travel (latched via hysteresis)
        self._l2_in_wall = _wall_state(brake, self._l2_in_wall,
                                       s.brake_wall_engage_at, s.brake_wall_release_at)
        if self._l2_in_wall:
            return self.wall

        # 4. Static brake wall - optional fixed wall mid-travel; replaces ramp
        if s.enable_brake_static_wall:
            return build_brake_walls(s.brake_static_wall_at, s.brake_static_wall_force, s.wall_zones)

        # 5. Surface rumble - off-road texture; replaces resistance
        surf = self.anim.surface_rumble(t, s)
        if surf is not None:
            return surf

        # 6. Brake resistance - default rigid ramp
        return self.anim.brake_resistance(t, s)

    def R2(self, t, s, now):
        accel = t["accel"]

        # 1. Gear shift thump - brief burst on shift, masks everything below
        if s.enable_gear_shift:
            shift = self.anim.shift_burst(s, now, accel, s.throttle_wall_engage_at)
            if shift:
                return shift

        # 2. Rev limiter buzz - rpm at/over rev_limit_ratio
        rev = self.anim.rev_buzz(t, s, now)
        if rev:
            return rev

        # 3. Wheelspin buzz - driven wheels spinning, surface-aware amp/freq
        spin = self.anim.wheelspin_buzz(t, s, now)
        if spin is not None:
            return spin

        # 4. Firmware end wall - hard wall near 100% travel (latched via hysteresis)
        self._r2_in_wall = _wall_state(accel, self._r2_in_wall,
                                       s.throttle_wall_engage_at, s.throttle_wall_release_at)
        if self._r2_in_wall:
            return self.wall

        # 5. Surface rumble - off-road texture; replaces resistance
        surf = self.anim.surface_rumble(t, s)
        if surf is not None:
            return surf

        # 6. Throttle resistance - default rigid ramp
        return self.anim.throttle_ramp(t, s)
