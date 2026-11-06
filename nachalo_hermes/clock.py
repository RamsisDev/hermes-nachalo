"""The two clocks every Nachalo step reads."""

from __future__ import annotations

import sys
import time


def monotonic_counting_sleep() -> int:
    """Monotonic seconds that keep counting while the machine sleeps.

    ``time.monotonic()`` stops during sleep on Linux (CLOCK_MONOTONIC) and macOS
    (mach_absolute_time), which would stretch a 10-minute TTL across a suspend.
    """
    if sys.platform.startswith("linux"):
        return int(time.clock_gettime(time.CLOCK_BOOTTIME))
    if sys.platform == "darwin":
        return int(time.clock_gettime(time.CLOCK_MONOTONIC))
    return int(time.monotonic())  # Windows: QueryPerformanceCounter includes sleep.


def now_unix() -> int:
    return int(time.time())


def clock():
    """A ``nachalo.Clock`` for this instant."""
    import nachalo

    return nachalo.Clock(now=monotonic_counting_sleep(), now_unix=now_unix())
