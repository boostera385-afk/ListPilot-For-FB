"""Thread-safe stop and pause/resume control for automation workers."""

from __future__ import annotations

import threading
import time


class AutomationControl:
    """Event-compatible control that can pause at safe checkpoints."""

    def __init__(self):
        self._condition = threading.Condition()
        self._stopped = False
        self._paused = False

    def stop(self):
        with self._condition:
            self._stopped = True
            self._paused = False
            self._condition.notify_all()

    def set(self):
        """Compatibility alias for ``threading.Event.set``."""
        self.stop()

    def pause(self):
        with self._condition:
            if self._stopped or self._paused:
                return False
            self._paused = True
            self._condition.notify_all()
            return True

    def resume(self):
        with self._condition:
            if self._stopped or not self._paused:
                return False
            self._paused = False
            self._condition.notify_all()
            return True

    @property
    def is_paused(self):
        with self._condition:
            return self._paused and not self._stopped

    def is_set(self):
        """Wait through a pause, then report whether a hard stop was set."""
        with self._condition:
            while self._paused and not self._stopped:
                self._condition.wait()
            return self._stopped

    def wait(self, timeout=None):
        """Event-compatible wait whose timeout excludes paused time."""
        with self._condition:
            if self._stopped:
                return True

            deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
            while not self._stopped:
                if self._paused:
                    paused_at = time.monotonic()
                    while self._paused and not self._stopped:
                        self._condition.wait()
                    if deadline is not None:
                        deadline += time.monotonic() - paused_at
                    continue

                if deadline is None:
                    self._condition.wait()
                    continue

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)

            return True
