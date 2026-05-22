"""All tunables in one place. Forces 0-255, frequencies in Hz."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Settings:
    # --- UDP ---
    udp_host: str = "127.0.0.1"
    udp_port: int = 5300
    udp_timeout: float = 0.5

    # --- Shared pedal config ---
    pedal_value_max: int = 255
    wall_zones: int = 2                       # firmware wall depth: 1 = only zone 9 (lightest), 9 = whole travel walled

    # --- Output smoothing (both triggers) ---
    # Eases trigger output over time so effects ramp instead of snapping: rigid
    # force is slew-rate limited and vibration amplitude fades in/out. This is
    # what keeps full throttle/brake, ABS, rev limiter and gear thumps from
    # feeling jarring. Off restores the old instant behavior.
    enable_smoothing: bool = True
    force_slew_per_s: int = 800               # max rigid force change per second (0-255 scale)
    amp_attack_per_s: int = 1200              # vibration amplitude rise per second (fade-in)
    amp_release_per_s: int = 700              # vibration amplitude fall per second (fade-out tail)

    # --- Surface rumble (both triggers) ---
    # Ambient texture buzz from FH surface_rumble_* telemetry (0..1 per wheel):
    # gravel/dirt/grass push it up, tarmac is ~0. Sits below the walls but above
    # resistance in both chains, so it replaces flat resistance while off-road.
    enable_surface_rumble: bool = True
    surface_rumble_gain: float = 40.0         # 0..1 rumble * gain -> 0-255 vibration amplitude
    surface_rumble_freq: int = 25             # Hz; low = earthy
    surface_rumble_min: float = 0.1           # deadzone: ignore rumble below this

    # =============================================================
    # L2 — Brake pedal
    # =============================================================

    # Resistance: rigid curve 0..wall_engage_at -> baseline..max_force, firmware wall at 100%.
    enable_brake_resistance: bool = True
    brake_deadzone: int = 50
    brake_baseline_force: int = 20
    brake_max_force: int = 55                 # rigid force at brake_wall_engage_at (peak of the curve before the wall)
    brake_curve: float = 5.0                  # parabolic: light through mid travel, sharply firm near the wall
    brake_wall_engage_at: int = 250           # accel byte to switch to firmware wall
    brake_wall_release_at: int = 200          # accel byte to release the wall back to rigid curve (hysteresis)
    brake_wall_force: int = 140               # rigid force the curve climbs to over [release_at, engage_at] so wall entry isn't a slam
    enable_brake_static_wall: bool = False    # Optional extra static wall
    brake_static_wall_at: int = 128           # brake byte where the static wall sits
    brake_static_wall_force: int = 255        # how hard the static wall resists (0-255)

    # Handbrake bonus: flat extra force when handbrake is engaged.
    enable_handbrake_bonus: bool = True
    handbrake_bonus: int = 60

    # ABS pulse: vibrate when tire slip telemetry crosses thresholds under hard braking.
    enable_abs: bool = True
    abs_brake_threshold: int = 80             # only pulse if we're definitely braking hard
    abs_min_speed_kmh: float = 15.0           # only pulse if we're definitely moving
    abs_slip_ratio_threshold: float = 1.0
    abs_combined_slip_threshold: float = 1.0
    abs_slip_full_at: float = 1.6             # slip at which the ABS pulse reaches abs_amp (ramps up from the threshold)
    abs_freq: int = 10                        # Hz for the ABS pulse
    abs_amp: int = 20                         # raw 0-255 byte for mode 0x06 vibration amplitude

    # =============================================================
    # R2 — Gas pedal
    # =============================================================

    # Resistance: light rigid curve 0..wall_engage_at -> baseline..max_force, firmware wall at 100%.
    enable_throttle_resistance: bool = True
    accel_deadzone: int = 50
    throttle_baseline_force: int = 0
    throttle_max_force: int = 8               # rigid force at the wall threshold — much lighter than the brake
    throttle_curve: float = 5.0               # parabolic: feather-light early, slightly firmer near the wall
    throttle_wall_engage_at: int = 250        # accel byte to switch to firmware wall
    throttle_wall_release_at: int = 200       # accel byte to release the wall back to rigid (hysteresis)
    throttle_wall_force: int = 40             # rigid force the curve climbs to over [release_at, engage_at] so wall entry isn't a slam

    # Rev limiter: vibrate when rpm/max_rpm exceeds the ratio.
    enable_rev_limiter: bool = True
    rev_limit_ratio: float = 0.93             # fire right at the cutoff, not across the whole upper rpm range
    rev_limit_full_ratio: float = 1.0         # rpm ratio at which the buzz reaches rev_limit_amp (soft ramp from rev_limit_ratio)
    rev_limit_freq: int = 20
    rev_limit_amp: int = 10                   # raw 0-255 byte for mode 0x06 vibration amplitude
    rev_limit_hold_ms: float = 120.0          # hold buzz this long after each trigger so the rpm bounce doesn't stutter it

    # Wheelspin buzz: when driven wheels spin faster than the car (longitudinal
    # slip), buzzes the R2 trigger. Surface-aware: water halves amp, off-road
    # gets a thumpier profile, tarmac uses the amp below. Frequency is fixed at
    # 100 Hz (only amp is user-tunable).
    enable_wheelspin_buzz: bool = True
    wheelspin_amp: int = 3                    # raw 0-255 byte for mode 0x06 vibration amplitude
    wheelspin_slip_full_at: float = 2.5       # slip at which wheelspin buzz reaches full amp (ramps from the 1.2 break point)

    # Gear shift: single short vibration burst on up/downshift while moving.
    enable_gear_shift: bool = True
    enable_gear_shift_brake: bool = True
    gear_shift_freq: int = 20
    gear_shift_amp: int = 255                 # raw 0-255 byte for mode 0x06 vibration amplitude
    gear_shift_duration_ms: float = 100.0     # one shot per shift

    # RGB lightbar "speedline": green throttle, red brake, blue handbrake,
    # blinking yellow on ABS. Rides the same HID frame as the triggers but only
    # touches the lightbar bytes, never rumble. Brightness scales with pedal
    # travel; ABS blinks at full. Priority ABS > handbrake > brake > throttle.
    enable_lightbar: bool = True
    lightbar_brightness: int = 255            # master scale 0-255
    lightbar_abs_blink_hz: float = 6.0        # ABS warning blink rate (full cycles/sec)

    # =============================================================
    # System
    # =============================================================

    # Startup pulse: brief trigger buzz to confirm HID connection on launch.
    enable_startup_pulse: bool = True
    startup_pulse_force: int = 150



    # Auto-reconnect to the controller when it's missing or drops. Disabled by
    # default for HidHide compatibility — re-enumerating HID devices while a
    # HidHide cloak toggles can leave the OS holding a dead handle. Enable from
    # the Settings tab if you want USB unplug/replug to recover without
    # restarting the app.
    enable_reconnect: bool = False
    reconnect_interval_s: float = 5.0

    # --- Controller selection ---
    # Lock to a specific DualSense by serial. Empty = auto (first device found).
    # Soft lock: if the locked controller is missing at connect time, fall back
    # to first-found rather than refusing to start. hidapi reports different
    # serials for the same controller on USB vs BT, so a lock is effectively to
    # a (controller, transport) pair.
    controller_lock_serial: str = ""

    # Whether ZUV should check for updates at launch. Default off so the user
    # isn't prompted every run; toggle on from the top of the System tab to
    # re-enable. The toggle writes a sentinel file the ZUV loader reads on next launch.
    check_for_updates: bool = False

    # UI language code (matches a module name in the `lang` package, e.g. "en",
    # "tr", "zh", "ja"). Applied at startup; change it from the LANG tab and
    # restart to re-render the UI. Unknown codes fall back to English.
    language: str = "en"

    # Auto-exit when game closes (Windows + Linux/Proton). Telemetry-lost is a fallback for Task Manager kills.
    exit_on_game_close: bool = True
    game_process_name_contains: tuple = ("forza",)
    game_poll_interval_s: float = 2.0
    telemetry_lost_exit_s: float = 60.0
