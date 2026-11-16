"""
Deadline-driven MIDI playback scheduler
=======================================

Drives a :class:`~picomidi.playback.engine.PlaybackEngine` from a background
thread by waiting until the next event (or clock / loop boundary) is due,
rather than polling on a fixed interval.

The engine stays deterministic and Qt-free; this module owns the wait loop.
Call :meth:`PlaybackScheduler.wake` after seek, pause, resume, speed changes,
or any other transport mutation so the wait is interrupted promptly.
"""

from __future__ import annotations

import math
import threading
from typing import Any, Callable, Optional, Union

from decologr import Decologr as log

from picomidi.playback.engine import PlaybackEngine, TransportState

# Cap a single sleep so a missed wake cannot stall forever; seek/pause still
# call wake() for immediate response.
DEFAULT_MAX_SLEEP_S = 1.0


class PlaybackScheduler:
    """
    Wait for engine deadlines and call :meth:`PlaybackEngine.process_until_now`.

    :param engine: PlaybackEngine to drive
    :param lock: Optional lock serialising engine access with the owner
    :param on_ended: Called (on the scheduler thread) when playback reaches EOF
    :param max_sleep_s: Upper bound on one wait interval
    """

    def __init__(
        self,
        engine: PlaybackEngine,
        lock: Optional[Union[threading.Lock, threading.RLock]] = None,
        on_ended: Optional[Callable[[], Any]] = None,
        max_sleep_s: float = DEFAULT_MAX_SLEEP_S,
    ) -> None:
        self._engine = engine
        self._lock = lock if lock is not None else threading.RLock()
        self._on_ended = on_ended
        self._max_sleep_s = max(0.001, float(max_sleep_s))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        """Whether the scheduler thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the scheduler thread (or wake it if already running)."""
        if self.is_running:
            self.wake()
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="PlaybackScheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the scheduler thread and wait for it to exit."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                log.message(
                    "PlaybackScheduler thread did not exit in time",
                    scope=self.__class__.__name__,
                )
            self._thread = None

    def wake(self) -> None:
        """Interrupt the current wait (after seek, pause, resume, etc.)."""
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            sleep_s = math.inf
            ended = False
            try:
                with self._lock:
                    if self._engine.state == TransportState.PLAYING:
                        ended = self._engine.process_until_now()
                        if not ended and self._engine.state == TransportState.PLAYING:
                            sleep_s = self._engine.wall_seconds_until_next_due()
            except Exception as ex:
                log.error(
                    f"Error during scheduled MIDI playback: {ex}",
                    scope=self.__class__.__name__,
                )
                sleep_s = self._max_sleep_s

            if self._stop.is_set():
                break

            if ended:
                callback = self._on_ended
                if callback is not None:
                    try:
                        callback()
                    except Exception as ex:
                        log.error(
                            f"Error in playback ended callback: {ex}",
                            scope=self.__class__.__name__,
                        )
                self._wait_for_wake(None)
                continue

            if sleep_s == math.inf:
                self._wait_for_wake(None)
                continue

            timeout = max(0.0, min(sleep_s, self._max_sleep_s))
            self._wait_for_wake(timeout)

    def _wait_for_wake(self, timeout: Optional[float]) -> None:
        """Wait until wake, stop, or *timeout* seconds (None = until wake/stop)."""
        if timeout is None:
            while not self._stop.is_set():
                if self._wake.wait(timeout=0.25):
                    break
        else:
            self._wake.wait(timeout=timeout)
        self._wake.clear()
