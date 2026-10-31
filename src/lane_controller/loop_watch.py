"""The arming loop, watched while the lane waits on the network for the car on it.

A registration QR is checked through the platform (`observe_qr`), and the call
can take up to the client's timeout. A car can back off the loop in that time,
and the next car roll on: an answer that opened the barrier then would open it
for a car nobody checked, and record its stay under the first car's read. So
while the call runs a thread of its own reads the loop every
`SAMPLE_SECONDS`, and the barrier opens only for a car that is still the car
that was there: occupied when the call began, occupied when it came back, and
no clear in between longer than a flicker.

The same rule the settled exit applies (lane-controller's exit case, its
`LoopWatch`), with the same numbers: a clear of `CLEAR_SAMPLES` samples in a
row, or `FLICKER_SECONDS` on the lane's clock, is a car gone; one clear sample
is a flicker. A loop read that raises is a car gone -- a watch that cannot see
the loop vouches for nothing. A swap of cars inside one sample cannot be seen
by sampling; a car cannot physically roll off and another roll on that fast.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

#: The longest clear a loop flicker is taken to be.
FLICKER_SECONDS = 0.5
#: How often the loop is read while the platform is asked: short enough that a
#: flicker reads as one clear sample, under `FLICKER_SECONDS`.
SAMPLE_SECONDS = 0.25
#: How many clear samples in a row are a car gone rather than a flicker.
CLEAR_SAMPLES = 2


class LoopWatch:
    """`stayed()` after `stop()`: the loop was occupied at the start and at the
    end, and no clear in between was longer than a flicker."""

    def __init__(self, loop, clock, *, name: str = "qr-loop-watch") -> None:
        self._loop = loop
        self._clock = clock
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._clear_since: float | None = None
        self._clear_run = 0
        self._left = False
        self._sample()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> LoopWatch:
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop sampling, and read the loop as the call returns. A clear
        reading then is read once more after `SAMPLE_SECONDS`, so a flicker at
        that moment is not taken for the car gone."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()
        self._sample()
        if self._clear_since is not None and not self._left:
            time.sleep(SAMPLE_SECONDS)
            self._sample()

    def stayed(self) -> bool:
        with self._lock:
            return not self._left and self._clear_since is None

    def _run(self) -> None:
        while not self._stop.wait(SAMPLE_SECONDS):
            self._sample()

    def _sample(self) -> None:
        try:
            occupied = self._loop.is_occupied()
        except Exception as err:  # noqa: BLE001 -- an unread loop fails shut
            log.warning("the arming loop could not be read while the platform was asked; the "
                        "car is taken as gone: %s", err.__class__.__name__)
            with self._lock:
                self._left = True
            return
        now = self._clock()
        with self._lock:
            if occupied:
                since = self._clear_since
                if since is not None and now - since >= FLICKER_SECONDS:
                    self._left = True
                self._clear_since, self._clear_run = None, 0
                return
            if self._clear_since is None:
                self._clear_since = now
            self._clear_run += 1
            if self._clear_run >= CLEAR_SAMPLES:
                self._left = True
