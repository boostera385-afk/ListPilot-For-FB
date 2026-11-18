"""Small, dependency-free animations for CustomTkinter, driven by ``after()``.

All functions must be called on the Tk main thread (use ``run_on_ui`` from
worker threads). Each animation is keyed per widget, so starting a new one
cancels the previous one instead of fighting it.
"""
import time

from gui import theme

FRAME_MS = 16  # ~60 fps

_running = {}  # (widget id, key) -> after id


def ease_out_cubic(t):
    return 1 - (1 - t) ** 3


def _hex_to_rgb(color):
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    """Blend two '#RRGGBB' colors; t=0 -> a, t=1 -> b."""
    ra, ga, ba = _hex_to_rgb(a)
    rb, gb, bb = _hex_to_rgb(b)
    return "#{:02X}{:02X}{:02X}".format(
        round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t)
    )


def cancel(widget, key):
    after_id = _running.pop((str(widget), key), None)
    if after_id:
        try:
            widget.after_cancel(after_id)
        except Exception:
            pass


def animate(widget, key, duration_ms, step, done=None, easing=ease_out_cubic):
    """Call ``step(eased_t)`` from 0 to 1 over ``duration_ms``."""
    cancel(widget, key)
    start = time.perf_counter()

    def tick():
        try:
            if not widget.winfo_exists():
                return
        except Exception:
            return
        t = min(1.0, (time.perf_counter() - start) * 1000 / max(1, duration_ms))
        try:
            step(easing(t))
        except Exception:
            _running.pop((str(widget), key), None)
            return
        if t < 1.0:
            _running[(str(widget), key)] = widget.after(FRAME_MS, tick)
        else:
            _running.pop((str(widget), key), None)
            if done:
                done()

    tick()


def count_up(label, target, duration_ms=650, fmt="{:,}"):
    """Animate a number label from its current value to ``target``."""
    try:
        current = int(str(label.cget("text")).replace(",", "") or 0)
    except ValueError:
        current = 0
    target = int(target or 0)
    if current == target:
        label.configure(text=fmt.format(target))
        return
    animate(label, "count", duration_ms,
            lambda t: label.configure(text=fmt.format(round(current + (target - current) * t))))


def progress_to(bar, value, duration_ms=450):
    """Smoothly move a CTkProgressBar to ``value`` (0..1)."""
    start = bar.get()
    value = max(0.0, min(1.0, float(value)))
    animate(bar, "progress", duration_ms, lambda t: bar.set(start + (value - start) * t))


def fade_color(widget, option, start, end, duration_ms=180):
    """Fade one color option (e.g. fg_color) between two (light, dark) colors."""
    a, b = theme.pick(start), theme.pick(end)
    if not (str(a).startswith("#") and str(b).startswith("#")):
        widget.configure(**{option: end})
        return
    animate(widget, f"fade:{option}", duration_ms,
            lambda t: widget.configure(**{option: mix(a, b, t)}),
            done=lambda: widget.configure(**{option: end}))


def slide_in(widget, duration_ms=260, offset=0.04):
    """Page transition: place ``widget`` slightly right and glide it into place."""
    widget.place(relx=offset, rely=0, relwidth=1, relheight=1)
    widget.lift()
    animate(widget, "slide", duration_ms,
            lambda t: widget.place_configure(relx=offset * (1 - t)))


class Pulse:
    """Breathing color on a widget option while something is running."""

    def __init__(self, widget, option, low, high, period_ms=1400):
        self.widget, self.option = widget, option
        self.low, self.high, self.period = low, high, period_ms
        self._after = None
        self._start = 0.0

    @property
    def active(self):
        return self._after is not None

    def start(self):
        if self._after:
            return
        self._start = time.perf_counter()
        self._tick()

    def stop(self, color=None):
        if self._after:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
        self._after = None
        try:
            self.widget.configure(**{self.option: color if color is not None else self.low})
        except Exception:
            pass

    def _tick(self):
        import math
        try:
            if not self.widget.winfo_exists():
                return
            phase = ((time.perf_counter() - self._start) * 1000 % self.period) / self.period
            t = (1 - math.cos(phase * 2 * math.pi)) / 2
            self.widget.configure(**{self.option: mix(theme.pick(self.low), theme.pick(self.high), t)})
            self._after = self.widget.after(40, self._tick)
        except Exception:
            self._after = None
