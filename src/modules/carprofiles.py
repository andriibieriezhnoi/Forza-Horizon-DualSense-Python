"""Built-in car-class profiles: transient driving-feel overlays applied live.

These are distinct from the user's named profiles in `profiles.py`. A profile
here is a *partial* override of the driving-feel Settings fields, shipped as a
JSON file in `src/profiles/`. The active overlay is chosen automatically from the
car's performance class as the car changes mid-session (detected via
`car_ordinal`), or locked for the whole session with `--profile <name>`.

Overlays layer on top of whatever named profile is active and are never written
back to user_preferences.json — they only mutate the live Settings the loop reads
each frame. (Caveat: saving from the Profiles tab while an overlay is active bakes
the overlaid values into that named profile.)

Telemetry exposes only the performance class (car_class) and drivetrain, not a
car *type*, so the auto-heuristic can only separate "sports" from "default";
"drift" and "offroad" are reachable via --profile.
"""
import json
import logging
from pathlib import Path

from modules import preferences, profiles
from modules.settings import Settings

log = logging.getLogger("fhds")

PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"

# car_class is a performance tier: D=0, C=1, B=2, A=3, S1=4, S2=5, X=6.
SPORTS_CLASS_THRESHOLD = 4  # S1 and up feel best with the firmer "sports" overlay.


def available_names() -> list[str]:
    """Sorted profile names (JSON stems) shipped in PROFILES_DIR."""
    try:
        return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))
    except OSError:
        return []


def load_overlays() -> dict[str, dict]:
    """Read every overlay file into {name: {field: value}}.

    Only valid driving-feel (profile) fields are kept; unknown keys and any
    global/system fields are dropped with a warning so an overlay can never touch
    udp_port, language, etc.
    """
    allowed = set(preferences._profile_fields(Settings()))
    overlays: dict[str, dict] = {}
    for path in sorted(PROFILES_DIR.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Skipping car profile %s: %s", path.name, e)
            continue
        if not isinstance(raw, dict):
            log.warning("Skipping car profile %s: not a JSON object", path.name)
            continue
        clean = {}
        for k, v in raw.items():
            if k in allowed:
                clean[k] = v
            else:
                log.warning("Car profile %s: ignoring field '%s'", path.name, k)
        overlays[path.stem] = clean
    return overlays


def select(car_class: int) -> str:
    """Best-effort auto pick from the performance class."""
    return "sports" if car_class >= SPORTS_CLASS_THRESHOLD else "default"


def _rebuild_settings(s, overlay: dict) -> None:
    """Reset driving-feel fields to (defaults -> active named profile -> overlay).

    Globals are left untouched. Reuses preferences helpers exactly as profiles.py
    does, so type coercion and field selection stay consistent.
    """
    defaults = type(s)()
    for k in preferences._profile_fields(s):
        setattr(s, k, getattr(defaults, k))
    store = profiles.load_store()
    snap = store["profiles"].get(store["active"], {})
    preferences._apply_snap(s, snap, preferences._profile_fields(s))
    preferences._apply_snap(s, overlay, preferences._profile_fields(s))


class CarProfileManager:
    """Watches car_ordinal and swaps the driving-feel overlay on the live Settings.

    forced=None -> auto: pick by car_class, re-apply only when the choice changes.
    forced="x"  -> lock overlay "x" for the session, applied once on the first packet.
    on_switch   -> optional callback(name) fired after an overlay is applied.
    """

    def __init__(self, settings, forced=None, on_switch=None):
        self.s = settings
        self.forced = forced
        self.on_switch = on_switch
        self.overlays = load_overlays()
        self.applied = None
        self._last_ordinal = None

    def on_packet(self, t) -> None:
        ordinal = t["car_ordinal"]
        if ordinal == self._last_ordinal:
            return
        first = self._last_ordinal is None
        self._last_ordinal = ordinal
        if self.forced is not None:
            if first:
                self._switch(self.forced)
            return
        target = select(t["car_class"])
        if target != self.applied:
            self._switch(target)

    def _switch(self, name: str) -> None:
        _rebuild_settings(self.s, self.overlays.get(name, {}))
        self.applied = name
        log.info("Car profile -> %s", name)
        if self.on_switch is not None:
            try:
                self.on_switch(name)
            except Exception:
                log.exception("Car profile on_switch callback failed")
