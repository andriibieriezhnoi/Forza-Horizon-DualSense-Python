"""All tunables in one place. Forces 0-255, frequencies in Hz."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Settings:
    # MARK: UDP
    udp_host: str = "127.0.0.1"               # bind address for Forza Data Out
    udp_port: int = 5300                      # match Forza HUD setting
    udp_timeout: float = 0.5                  # socket recv timeout (s)

    # MARK: Pedal shared
    pedal_value_max: int = 255                # raw pedal byte range. DO NOT CHANGE
    wall_zones: int = 2                       # firmware end-wall depth; 1=top zone only, 9=full travel

    # MARK: Output smoothing (both triggers)
    # Eases trigger output over time so effects ramp instead of snapping: rigid
    # force is slew-rate limited and vibration amplitude fades in/out. This is
    # what keeps full throttle/brake, ABS, rev limiter and gear thumps from
    # feeling jarring. Off restores the old instant behavior.
    enable_smoothing: bool = True
    force_slew_per_s: int = 800               # max rigid force change per second (0-255 scale)
    amp_attack_per_s: int = 1200              # vibration amplitude rise per second (fade-in)
    amp_release_per_s: int = 700              # vibration amplitude fall per second (fade-out tail)

    # MARK: Surface rumble (both triggers)
    # Ambient texture buzz from FH surface_rumble_* telemetry (0..1 per wheel):
    # gravel/dirt/grass push it up, tarmac is ~0. Sits below the walls but above
    # resistance in both chains, so it replaces flat resistance while off-road.
    enable_surface_rumble: bool = True
    surface_rumble_gain: float = 40.0         # 0..1 rumble * gain -> 0-255 vibration amplitude
    surface_rumble_freq: int = 25             # Hz; low = earthy
    surface_rumble_min: float = 0.1           # deadzone: ignore rumble below this

    # MARK: L2 brake resistance
    # Rigid curve: 0..wall_engage_at maps baseline..max_force, then firmware wall at 100%.
    enable_brake_resistance: bool = True
    brake_deadzone: int = 50                  # ignore pedal below this byte
    brake_baseline_force: int = 20            # force at deadzone exit
    brake_max_force: int = 55                 # peak force just before the wall
    brake_curve: float = 5.0                  # parabolic exponent; higher = softer mid, harder near wall
    brake_wall_engage_at: int = 250           # byte that triggers firmware wall. DO NOT CHANGE
    brake_wall_release_at: int = 200          # hysteresis exit byte. DO NOT CHANGE
    brake_wall_force: int = 140               # rigid force the curve climbs to over [release_at, engage_at] so wall entry isn't a slam
    enable_brake_static_wall: bool = False    # optional fixed wall mid-travel
    brake_static_wall_at: int = 128           # pedal byte where the static wall sits
    brake_static_wall_force: int = 255        # static wall strength

    # MARK: L2 brake G-force feedback
    # Firm the brake trigger by how hard the car actually decelerates (longitudinal
    # accel_z), on top of the pedal curve. Conveys weight transfer / brake bite. Only
    # active while braking; ABS and the wall take priority, so this lives in the
    # mid-to-hard braking range.
    enable_brake_gforce: bool = True
    brake_gforce_per_g: float = 45.0          # rigid force added per g of deceleration
    brake_gforce_max_force: int = 60          # cap on the G contribution (0-255)
    brake_gforce_deadzone_g: float = 0.15     # ignore decel below this (g)
    brake_gforce_smoothing: float = 0.25      # EMA factor on the decel signal (0..1; lower = smoother)

    # MARK: L2 handbrake bonus
    enable_handbrake_bonus: bool = True
    handbrake_bonus: int = 60                 # flat extra force while handbrake is engaged

    # MARK: L2 ABS pulse
    # Vibrates when tire slip crosses thresholds under hard braking.
    enable_abs: bool = True
    abs_brake_threshold: int = 80             # min brake byte to arm
    abs_min_speed_kmh: float = 15.0           # min speed to arm
    abs_slip_ratio_threshold: float = 1.0     # per-wheel slip trigger
    abs_combined_slip_threshold: float = 1.0  # combined slip trigger
    abs_slip_full_at: float = 1.6             # slip at which the ABS pulse reaches abs_amp (ramps up from the threshold)
    abs_freq: int = 10                        # pulse frequency
    abs_amp: int = 20                         # pulse amplitude

    # MARK: R2 throttle resistance
    # Light rigid curve: 0..wall_engage_at maps baseline..max_force, then firmware wall at 100%.
    enable_throttle_resistance: bool = True
    accel_deadzone: int = 50                  # ignore pedal below this byte
    throttle_baseline_force: int = 0          # force at deadzone exit
    throttle_max_force: int = 8               # peak force just before the wall (lighter than brake)
    throttle_curve: float = 5.0               # parabolic exponent; higher = softer early, firmer near wall
    throttle_wall_engage_at: int = 250        # byte that triggers firmware wall. DO NOT CHANGE
    throttle_wall_release_at: int = 200       # hysteresis exit byte. DO NOT CHANGE
    throttle_wall_force: int = 40             # rigid force the curve climbs to over [release_at, engage_at] so wall entry isn't a slam

    # MARK: R2 rev limiter
    # Vibrates when rpm/max_rpm exceeds the ratio; brief hold smooths rpm bounce.
    enable_rev_limiter: bool = True
    rev_limit_ratio: float = 0.93             # fraction of max_rpm to fire at
    rev_limit_full_ratio: float = 1.0         # rpm ratio at which the buzz reaches rev_limit_amp (soft ramp from rev_limit_ratio)
    rev_limit_freq: int = 20
    rev_limit_amp: int = 10
    rev_limit_hold_ms: float = 120.0          # min on-time per trigger

    # MARK: R2 wheelspin buzz
    # `wheelspin_amp` is the tarmac reference. Off-road / water amps scale off it
    # (water 0.5x, dirt 1.5x, gravel 2x). Surface freqs are fixed in code.
    enable_wheelspin_buzz: bool = True
    wheelspin_amp: int = 3
    wheelspin_slip_full_at: float = 2.5       # slip at which wheelspin buzz reaches full amp (ramps from the 1.2 break point)

    # MARK: Gear shift
    # One short burst on up/downshift while moving.
    enable_gear_shift: bool = True            # buzz on R2
    enable_gear_shift_brake: bool = True      # also buzz on L2 via the wall
    gear_shift_freq: int = 10
    gear_shift_amp: int = 255
    gear_shift_duration_ms: float = 100.0     # burst length

    # MARK: RGB lightbar speedline
    # Green throttle, red brake, blue handbrake, blinking yellow on ABS. Rides the
    # same HID frame as the triggers but only touches the lightbar bytes, never
    # rumble. Brightness scales with pedal travel; ABS blinks at full.
    # Priority ABS > handbrake > brake > throttle.
    enable_lightbar: bool = True
    lightbar_brightness: int = 255            # master scale 0-255
    lightbar_abs_blink_hz: float = 6.0        # ABS warning blink rate (full cycles/sec)

    # MARK: System - startup pulse
    enable_startup_pulse: bool = True
    startup_pulse_force: int = 150            # one-shot force test on connect

    # MARK: System - reconnect
    # Off by default for HidHide compatibility. On = USB unplug/replug recovers without restart.
    enable_reconnect: bool = False
    reconnect_interval_s: float = 5.0         # retry cadence when disconnected

    # MARK: System - controller selection
    # Lock to a specific DualSense by serial. Empty = auto (first found).
    # Soft lock: falls back to first-found if the locked one is missing.
    # USB and BT report different serials for the same controller.
    controller_lock_serial: str = ""

    # MARK: System - updates
    check_for_updates: bool = False           # ZUV loader checks GitHub for a new release at launch

    # MARK: System - language
    # Module name in `lang/` (en, tr, zh, ja). Unknown codes fall back to English.
    language: str = "en"

    # MARK: System - auto exit
    # Closes when the game process disappears; telemetry-lost is a fallback for Task Manager kills.
    exit_on_game_close: bool = True
    game_process_name_contains: tuple = ("forza",)   # substring match, case-insensitive
    game_poll_interval_s: float = 2.0                # psutil scan cadence
    telemetry_lost_exit_s: float = 60.0              # quit if no packets for this long after first packet
