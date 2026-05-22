"""DualSense adaptive trigger effects.

  TriggerAnimations — every effect (ABS, gear shift, rev limiter, resistance...).
                      Owns timing state for effects that span frames.
  Trigger           — one trigger's priority chain (config + wall hysteresis).
  Controller        — builds L2 / R2 and produces a frame for each per tick.
"""

import time

# --- Raw mode/effect bytes ---

M_OFF            = 0x05  # reset to neutral
M_VIBRATE        = 0x06  # Simple_Vibration (single zone buzz)
M_RIGID          = 0x01  # Static_Resistance
M_RIGID_ZONES    = 0x21  # Feedback: per-zone resistance (10 slots)
M_VIBRATE_ZONES  = 0x26  # Vibration: per-zone amplitude + frequency

# Hidden firmware effects - unique behavior, may be removed someday
M_BOW            = 0x22  # resist start..end then snap back
M_GALLOP         = 0x23  # rhythmic two-foot pulse
M_MACHINE        = 0x27  # oscillate between two amplitudes
M_WEAPON_SIMPLE  = 0x02  # Simple_Weapon
M_WEAPON         = 0x25  # resist start..end with snap release

# Limited leftovers - stricter param ranges, no clear use case
M_RIGID_LIMITED  = 0x11
M_WEAPON_LIMITED = 0x12

RAW_MAX = 255

# Below this car speed (km/h) we trust raw wheel rotation instead of slip_ratio
# (slip_ratio degenerates near zero speed). Above it, slip_ratio is canonical.
LOW_SPEED_KMH = 5.0
# Wheel angular speed (rad/s) above which we count as spinning at standstill.
# ~30 rad/s = ~3 wheel revs/sec, clearly spun-up regardless of tire size.
BURNOUT_ROT_THRESHOLD = 30.0


def _clamp(v, hi=RAW_MAX):
    return max(0, min(hi, round(v)))


def _pack_zones(strengths):
    """Build the 6-byte (active-mask, 3-bit-per-zone strengths) payload shared
    by rigid_zones and vibrate_zones. Strengths are 0..8; 0 = inactive."""
    active = packed = 0
    for i, s in enumerate(strengths[:10]):
        if s > 0:
            active |= 1 << i
            packed |= (s - 1) << (3 * i)
    return (
        active & 0xFF, (active >> 8) & 0xFF,
        packed & 0xFF, (packed >> 8) & 0xFF,
        (packed >> 16) & 0xFF, (packed >> 24) & 0xFF,
    )


# --- Effect primitives (raw HID frames) -----------------------------------

def off():
    return (M_OFF, ())

def rigid(force):
    return (M_RIGID, (0, _clamp(force)))

def vibrate(freq, amp):
    return (M_VIBRATE, (_clamp(freq), _clamp(amp)))

def vibrate_zones(amp, freq, wall_zones):
    """Per-zone vibrate: lower zones buzz at `amp` (1-8), top `wall_zones` stay maxed."""
    a = max(1, min(8, int(amp)))
    w = max(1, min(9, int(wall_zones)))
    strengths = [a] * (10 - w) + [8] * w
    return (M_VIBRATE_ZONES, _pack_zones(strengths) + (0, 0, _clamp(freq), 0))

def rigid_zones(zones):
    """Per-zone resistance: 10 per-zone strengths (0-8). Zero = inactive."""
    strengths = [max(0, min(8, int(s))) for s in zones[:10]]
    return (M_RIGID_ZONES, _pack_zones(strengths) + (0, 0, 0, 0))

def weapon(start, end, strength):
    """Weapon: resist between start..end zones, snap on release. start 2-7, end start+1..8, strength 1-8."""
    s = max(2, min(7, int(start)))
    e = max(s + 1, min(8, int(end)))
    f = max(1, min(8, int(strength)))
    zones = (1 << s) | (1 << e)
    return (M_WEAPON, (zones & 0xFF, (zones >> 8) & 0xFF, f - 1))

def bow(start, end, strength, snap_force):
    """Bow: resist start..end then snap. start 0-8, end start+1..8, both forces 1-8."""
    s = max(0, min(8, int(start)))
    e = max(s + 1, min(8, int(end)))
    f = max(1, min(8, int(strength)))
    sf = max(1, min(8, int(snap_force)))
    zones = (1 << s) | (1 << e)
    pair = ((f - 1) & 0x07) | (((sf - 1) & 0x07) << 3)
    return (M_BOW, (zones & 0xFF, (zones >> 8) & 0xFF, pair & 0xFF, (pair >> 8) & 0xFF))

def gallop(start, end, first_foot, second_foot, freq):
    """Galloping: two-foot pulse. start 0-8, end start+1..9, firstFoot 0-6, secondFoot ff+1..7, freq Hz."""
    s = max(0, min(8, int(start)))
    e = max(s + 1, min(9, int(end)))
    ff = max(0, min(6, int(first_foot)))
    sf = max(ff + 1, min(7, int(second_foot)))
    zones = (1 << s) | (1 << e)
    pair = (sf & 0x07) | ((ff & 0x07) << 3)
    return (M_GALLOP, (zones & 0xFF, (zones >> 8) & 0xFF, pair & 0xFF, _clamp(freq)))

def machine(start, end, amp_a, amp_b, freq, period):
    """Machine: oscillate between two amplitudes. start 0-8, end start+1..9, amps 0-7, freq Hz, period (0.1s units)."""
    s = max(0, min(8, int(start)))
    e = max(s + 1, min(9, int(end)))
    a = max(0, min(7, int(amp_a)))
    b = max(0, min(7, int(amp_b)))
    zones = (1 << s) | (1 << e)
    pair = (a & 0x07) | ((b & 0x07) << 3)
    return (M_MACHINE, (zones & 0xFF, (zones >> 8) & 0xFF, pair & 0xFF, _clamp(freq), _clamp(period)))


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
    return rigid_zones([0] * (10 - n) + [8] * n)

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
    return rigid_zones(zones)


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
        # Wall 0hz for kickback, else normal vibrate.
        if pedal >= (wall_engage_at + RAW_MAX) // 2:
            return vibrate_zones(_amp_to_strength(s.gear_shift_amp), 0, s.wall_zones)
        return vibrate(s.gear_shift_freq, s.gear_shift_amp)

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
        return vibrate(s.rev_limit_freq, amp)

    def wheelspin_buzz(self, t, s, now):
        # R2 buzz when tires lose grip (wheelspin or drift).
        # At speed: tire_combined_slip catches both longitudinal + lateral slip.
        # At standstill: slip values degenerate, so trust raw wheel rotation.
        if not s.enable_wheelspin_buzz:
            return None
        if t["accel"] < s.accel_deadzone:
            return None
        wheels = DRIVEN_WHEELS.get(t["drive_train"], ("fl", "fr", "rl", "rr"))
        # Detection: at standstill slip ratios degenerate, so trust raw wheel
        # rotation (launch burnouts); at speed use combined slip (spin + drift).
        if t["speed"] < LOW_SPEED_KMH:
            if max(abs(t[f"wheel_rotation_speed_{w}"]) for w in wheels) < BURNOUT_ROT_THRESHOLD:
                return None
            frac = 1.0
        else:
            slip = max(abs(t[f"tire_combined_slip_{w}"]) for w in wheels)
            if slip < 1.0:
                return None
            # Amp scales with how far past the break point the wheels spin.
            frac = _scale(slip, 1.0, s.wheelspin_slip_full_at, 1.0)
        # Surface profile: tarmac amp is the reference, others scale off it.
        amp = s.wheelspin_amp
        if any(t[f"wheel_in_puddle_{w}"] > 0 for w in wheels):
            freq, amp = 100, max(1, amp // 2)                # water 0.5x
        else:
            rumble = max(abs(t[f"surface_rumble_{w}"]) for w in wheels)
            if rumble > 0.30:                                # gravel / rocks 2x
                freq, amp = 20, min(255, amp * 2)
            elif rumble > 0.10:                              # dirt / loose tarmac 1.5x
                freq, amp = 60, min(255, int(amp * 1.5))
            else:                                            # tarmac 1x
                freq, amp = 100, amp
        return vibrate(freq, amp * frac)

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
        return vibrate(s.abs_freq, amp)

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
        return vibrate(s.surface_rumble_freq, rumble * s.surface_rumble_gain)


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
        if self._mode == M_VIBRATE and mode != M_VIBRATE:
            self._amp = max(0.0, self._amp - s.amp_release_per_s * dt)
            if self._amp >= 1.0:
                return vibrate(self._freq, self._amp)
            self._mode = None

        if mode == M_RIGID:
            target = frame[1][1]
            if self._mode == M_RIGID:
                self._force = _slew(self._force, target, s.force_slew_per_s * dt)
            else:
                self._force = float(target)
            self._mode = M_RIGID
            return rigid(self._force)

        if mode == M_VIBRATE:
            freq, target = frame[1]
            if self._mode == M_VIBRATE and freq == self._freq:
                rate = s.amp_attack_per_s if target >= self._amp else s.amp_release_per_s
                self._amp = _slew(self._amp, target, rate * dt)
            else:
                if self._mode != M_VIBRATE:
                    self._amp = 0.0          # fade in from silence on entry
                self._freq = freq
                self._amp = _slew(self._amp, target, s.amp_attack_per_s * dt)
            self._mode = M_VIBRATE
            return vibrate(self._freq, self._amp)

        # M_OFF, firmware wall (M_RIGID_ZONES), deep gear kick (M_VIBRATE_ZONES): snap.
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


# --- Standalone preview ---------------------------------------------------
# Run: `python -m modules.dualsense.triggers` from src/, or `python triggers.py`.
# Pick an effect by number; it plays on BOTH triggers for ~3s then resets.

EFFECT_MENU = [
    ("off                          - neutral",                                       lambda: off()),
    ("rigid(180)                   - uniform resistance whole travel",               lambda: rigid(180)),
    ("vibrate(20, 180)           - uniform buzz 20 Hz whole travel",               lambda: vibrate(20, 180)),
    ("vibrate_zones(6,20,3)       - light buzz lower zones + harder buzz top 3",   lambda: vibrate_zones(6, 20, 3)),
    ("rigid_zones middle zones        - resist zones 5-7 only (feel a bump mid-pull)", lambda: rigid_zones([0,0,0,0,0,8,8,8,0,0])),
    ("weapon(4, 7, 8)              - strong resist 4..7 then SUDDENLY free",         lambda: weapon(4, 7, 8)),
    ("bow(1, 5, 3, 8) SLOW pull    - light pull, then trigger PUSHES BACK hard",     lambda: bow(1, 5, 3, 8)),
    ("bow(2, 7, 2, 8) FULL pull    - very light pull, max snap-back over wide zone", lambda: bow(2, 7, 2, 8)),
    ("gallop(2, 8, 1, 4, 2)        - slow horse gallop (2 Hz)",                      lambda: gallop(2, 8, 1, 4, 2)),
    ("gallop(2, 8, 1, 4, 5)        - faster gallop (5 Hz)",                          lambda: gallop(2, 8, 1, 4, 5)),
    ("machine(2, 8, 2, 7, 8, 5)    - oscillate weak<->strong every 0.5s",            lambda: machine(2, 8, 2, 7, 8, 5)),
    ("machine(2, 8, 0, 7, 4, 8)    - oscillate OFF<->strong every 0.8s",             lambda: machine(2, 8, 0, 7, 4, 8)),
]

def _preview():
    try:
        from .main import DualSense
    except ImportError:
        import os, sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        from modules.dualsense.main import DualSense
    ds = DualSense(enable_startup_pulse=False)
    ds.open()
    print("Waiting for DualSense...")
    for _ in range(50):
        if ds.connected:
            break
        time.sleep(0.1)
    if not ds.connected:
        print("No controller found. Plug in a DualSense and retry.")
        ds.close()
        return
    print("Connected. Effects:")
    for i, (label, _) in enumerate(EFFECT_MENU):
        print(f"  {i}: {label}")
    print("  q: quit")
    try:
        while True:
            choice = input("\nPick effect # (or q): ").strip().lower()
            if choice in ("q", "quit", "exit"):
                break
            if not choice.isdigit() or not (0 <= int(choice) < len(EFFECT_MENU)):
                print("Invalid.")
                continue
            label, factory = EFFECT_MENU[int(choice)]
            print(f"Playing: {label}  (3s, squeeze either trigger)")
            frame = factory()
            ds.set(frame, frame)
            time.sleep(3.0)
            ds.set(off(), off())
    except KeyboardInterrupt:
        pass
    finally:
        ds.set(off(), off())
        time.sleep(0.1)
        ds.close()
        print("Done.")


if __name__ == "__main__":
    _preview()
