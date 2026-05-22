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

def _scale(value, lo, hi, out_max):
    """Map value in [lo, hi] to [0, out_max], clamped. Makes event effects
    (ABS, rev limiter, wheelspin) proportional to how far past their threshold
    the signal is, instead of a binary full-amplitude trigger."""
    if hi <= lo:
        return out_max if value >= lo else 0.0
    return out_max * min(1.0, max(0.0, (value - lo) / (hi - lo)))

def _wall_approach(value, force, wall_force, release_at, engage_at):
    """Lift `force` toward `wall_force` across (release_at, engage_at) so entry
    into the firmware wall is a small step, not a slam from the light curve."""
    if value <= release_at or wall_force <= force:
        return force
    f = min(1.0, (value - release_at) / max(engage_at - release_at, 1))
    return force + (wall_force - force) * f

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
        self._rev_amp = 0.0
        self._decel_ema = 0.0

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
        # Soft limiter: amp grows from rev_limit_ratio toward redline instead of a
        # binary trigger. Brief hold carries the last amp through rpm bounce.
        if not s.enable_rev_limiter or t["accel"] < s.accel_deadzone:
            return None
        max_rpm = t["max_rpm"]
        rpm_r = t["rpm"] / max_rpm if max_rpm > 0 else 0.0
        amp = _scale(rpm_r, s.rev_limit_ratio, s.rev_limit_full_ratio, s.rev_limit_amp)
        if amp > 0:
            self._rev_amp = amp
            self._rev_until = now + s.rev_limit_hold_ms / 1000.0
        elif now < self._rev_until:
            amp = self._rev_amp
        else:
            return None
        return vibration(s.rev_limit_freq, amp)

    def wheelspin_buzz(self, t, s, now):
        # Per-surface R2 buzz when driven wheels spin faster than the road.
        if not s.enable_wheelspin_buzz:
            return None
        # Need real throttle input above 10 km/h; below that launch-spin dominates.
        if t["speed"] < 10.0 or t["accel"] < s.accel_deadzone:
            return None
        # Positive slip only. Negative = locked wheels (handbrake/ABS), not wheelspin.
        wheels = DRIVEN_WHEELS.get(t["drive_train"], ("fl", "fr", "rl", "rr"))
        slip = max(t[f"tire_slip_ratio_{w}"] for w in wheels)
        if slip < 1.2:
            return None
        # Amp scales with how hard the driven wheels spin past the break point.
        frac = _scale(slip, 1.2, s.wheelspin_slip_full_at, 1.0)
        # Surface profile: water halves amp, off-road gets a thumpier buzz.
        if any(t[f"wheel_in_puddle_depth_{w}"] > 0.0 for w in wheels):
            freq, amp = 100, max(1, s.wheelspin_amp // 2)
        else:
            rumble = max(abs(t[f"surface_rumble_{w}"]) for w in wheels)
            if rumble > 0.30:        # gravel / rocks
                freq, amp = 20, 15
            elif rumble > 0.10:      # dirt / loose tarmac
                freq, amp = 60, 8
            else:                    # tarmac
                freq, amp = 100, s.wheelspin_amp
        return vibration(freq, amp * frac)

    def abs_pulse(self, t, s):
        if not s.enable_abs or not abs_active(t, s):
            return None
        # Amp grows with how hard the tyres are locking, not a flat full pulse.
        amp = max(
            _scale(_max_slip(t, "tire_slip_ratio"),
                   s.abs_slip_ratio_threshold, s.abs_slip_full_at, s.abs_amp),
            _scale(_max_slip(t, "tire_combined_slip"),
                   s.abs_combined_slip_threshold, s.abs_slip_full_at, s.abs_amp),
        )
        return vibration(s.abs_freq, amp)

    def _brake_gforce(self, t, s):
        """Extra L2 resistance from real longitudinal deceleration (weight
        transfer / brake bite). accel_z is forward-positive, so braking is
        negative; we use the decelerating magnitude in g. EMA-smoothed because
        the accelerometer is noisy."""
        if not s.enable_brake_gforce:
            return 0.0
        decel_g = max(0.0, -t["accel_z"]) / 9.81
        self._decel_ema += s.brake_gforce_smoothing * (decel_g - self._decel_ema)
        if t["brake"] < s.brake_deadzone or self._decel_ema < s.brake_gforce_deadzone_g:
            return 0.0
        over = self._decel_ema - s.brake_gforce_deadzone_g
        return min(float(s.brake_gforce_max_force), over * s.brake_gforce_per_g)

    def brake_resistance(self, t, s):
        handbrake = s.enable_handbrake_bonus and t["handbrake"]
        if not s.enable_brake_resistance:
            return rigid(s.handbrake_bonus) if handbrake else off()
        force = _ramp(t["brake"], s.brake_deadzone, s.brake_baseline_force,
                      s.brake_max_force, s.brake_curve, s.brake_wall_engage_at)
        force = _wall_approach(t["brake"], force, s.brake_wall_force,
                               s.brake_wall_release_at, s.brake_wall_engage_at)
        force += self._brake_gforce(t, s)
        if handbrake:
            force += s.handbrake_bonus
        return rigid(force)

    def throttle_ramp(self, t, s):
        if not s.enable_throttle_resistance:
            return off()
        force = _ramp(t["accel"], s.accel_deadzone, s.throttle_baseline_force,
                      s.throttle_max_force, s.throttle_curve, s.throttle_wall_engage_at)
        force = _wall_approach(t["accel"], force, s.throttle_wall_force,
                               s.throttle_wall_release_at, s.throttle_wall_engage_at)
        return rigid(force)

    def surface_rumble(self, t, s):
        # Ambient off-road texture on both triggers; replaces flat resistance.
        if not s.enable_surface_rumble:
            return None
        rumble = _max_slip(t, "surface_rumble")
        if rumble < s.surface_rumble_min:
            return None
        return vibration(s.surface_rumble_freq, rumble * s.surface_rumble_gain)


# --- Output smoothing -----------------------------------------------------

def _slew(current, target, max_delta):
    """Move `current` toward `target` by at most `max_delta`."""
    if max_delta <= 0:
        return current
    if target > current:
        return min(target, current + max_delta)
    return max(target, current - max_delta)


class _TriggerSmoother:
    """Eases one trigger's output over time. Holds state only; the rates live in
    Settings so they stay tunable per profile. Rigid force is slew-limited;
    vibration amplitude fades in/out (attack/release). Cross-mode jumps and
    firmware walls can't be interpolated, so they snap - except a dying pulse,
    which decays to silence first so effects don't end with a click."""

    def __init__(self):
        self.reset()

    def reset(self):
        self._mode = None
        self._force = 0.0
        self._amp = 0.0
        self._freq = 0

    def apply(self, frame, dt, s):
        if not s.enable_smoothing:
            self._mode = None
            return frame
        mode = frame[0]

        # A pulse that just ended: fade its amplitude out before the new frame.
        if self._mode == M_PULSE and mode != M_PULSE:
            self._amp = max(0.0, self._amp - s.amp_release_per_s * dt)
            if self._amp >= 1.0:
                return vibration(self._freq, self._amp)
            self._mode = None

        if mode == M_RIGID:
            target = frame[1][1]
            if self._mode == M_RIGID:
                self._force = _slew(self._force, target, s.force_slew_per_s * dt)
            else:
                self._force = float(target)
            self._mode = M_RIGID
            return rigid(self._force)

        if mode == M_PULSE:
            freq, target = frame[1]
            if self._mode == M_PULSE and freq == self._freq:
                rate = s.amp_attack_per_s if target >= self._amp else s.amp_release_per_s
                self._amp = _slew(self._amp, target, rate * dt)
            else:
                if self._mode != M_PULSE:
                    self._amp = 0.0          # fade in from silence on entry
                self._freq = freq
                self._amp = _slew(self._amp, target, s.amp_attack_per_s * dt)
            self._mode = M_PULSE
            return vibration(self._freq, self._amp)

        # M_OFF, firmware wall (M_FEEDBACK), deep gear kick (M_PULSE_AB): snap.
        self._mode = mode
        self._force = 0.0
        self._amp = 0.0
        return frame


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
        self._l2_smoother = _TriggerSmoother()
        self._r2_smoother = _TriggerSmoother()
        self._last_now = None

    def update(self, t, s):
        if not t["on"]:
            self._l2_smoother.reset()
            self._r2_smoother.reset()
            self._last_now = None
            return off(), off()
        now = time.monotonic()
        # Slew by elapsed time (telemetry rate varies); clamp so a gap after idle
        # doesn't produce one huge step.
        dt = 0.0 if self._last_now is None else min(0.05, max(0.0, now - self._last_now))
        self._last_now = now
        if s.enable_gear_shift or s.enable_gear_shift_brake:
            self.anim.arm_shift(t, s, now)
        left = self._l2_smoother.apply(self.L2(t, s, now), dt, s)
        right = self._r2_smoother.apply(self.R2(t, s, now), dt, s)
        return left, right

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
