"""Custom Textual widgets used by the TUI."""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget


class RangeSlider(Widget, can_focus=True):
    """Mouse-draggable horizontal bar over a numeric range.

    - Click or drag anywhere on the bar to set the value.
    - Left/Right arrow keys nudge by one step; PageUp/PageDown by ten steps.
    - `value` is always a float; consumers cast to int where the field is int.
    """

    DEFAULT_CSS = """
    RangeSlider {
        height: 3;
        padding: 1 0;
        width: 1fr;
        min-width: 8;
        color: $accent;
    }
    """

    value: reactive[float] = reactive(0.0)

    class Changed(Message):
        def __init__(self, slider: "RangeSlider", value: float) -> None:
            super().__init__()
            self.slider = slider
            self.value = value

    def __init__(
        self,
        value: float,
        min_val: float = 0.0,
        max_val: float = 1.0,
        step: float | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.min_val = float(min_val)
        self.max_val = float(max_val)
        span = self.max_val - self.min_val
        # MARK: default step = 1 for whole-int ranges, else span/200
        if step is not None:
            self.step = float(step)
        elif span > 50 and float(span).is_integer():
            self.step = 1.0
        elif span:
            self.step = span / 200.0
        else:
            self.step = 1.0
        self.value = self._clamp(float(value))

    def _clamp(self, v: float) -> float:
        return max(self.min_val, min(self.max_val, v))

    def render(self) -> Text:
        w = max(self.size.width, 1)
        span = self.max_val - self.min_val
        ratio = (self.value - self.min_val) / span if span else 0.0
        filled = max(0, min(w, round(w * ratio)))
        return Text("\u2588" * filled + "\u2500" * (w - filled))

    def watch_value(self, _old: float, _new: float) -> None:
        self.refresh()

    def _from_x(self, x: int) -> float:
        w = max(self.size.width - 1, 1)
        x = max(0, min(w, x))
        ratio = x / w
        v = self.min_val + ratio * (self.max_val - self.min_val)
        if self.step > 0:
            v = round((v - self.min_val) / self.step) * self.step + self.min_val
        return self._clamp(v)

    def _set_and_emit(self, new: float) -> None:
        if abs(new - self.value) > 1e-9:
            self.value = new
            self.post_message(self.Changed(self, new))

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._set_and_emit(self._from_x(event.x))
        self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        # MARK: only drag while a mouse button is held
        if event.button:
            self._set_and_emit(self._from_x(event.x))

    def on_mouse_up(self, _event: events.MouseUp) -> None:
        self.release_mouse()

    def on_key(self, event: events.Key) -> None:
        if event.key == "left":
            self._set_and_emit(self._clamp(self.value - self.step))
            event.stop()
        elif event.key == "right":
            self._set_and_emit(self._clamp(self.value + self.step))
            event.stop()
        elif event.key == "pageup":
            self._set_and_emit(self._clamp(self.value + self.step * 10))
            event.stop()
        elif event.key == "pagedown":
            self._set_and_emit(self._clamp(self.value - self.step * 10))
            event.stop()
        elif event.key == "home":
            self._set_and_emit(self.min_val)
            event.stop()
        elif event.key == "end":
            self._set_and_emit(self.max_val)
            event.stop()


# MARK: back-compat alias for any old imports
AmpSlider = RangeSlider
