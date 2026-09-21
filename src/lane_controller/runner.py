"""The production loop: the lane keeps serving cars, and the cache keeps fresh.

Until this existed the package had every part of a running lane and nothing
that ran it. `LaneController.run_once` served ONE car; `sync_rules` refreshed
the cache ONCE; `serve` published the read contract over a simulated lane
that nothing drove, and `run_forever` had no caller. `sync_rules` was called
from the demo and from nowhere else -- so "the lane refreshes its rules off
the barrier's path" was a sentence about a function, not about a process.

Two threads, and the barrier's path is on neither of them's clock.

  THE LANE THREAD calls `run_once` for ever: wait for a vehicle, serve it,
  flush the outbox, sample the loops. It is the only thread that touches the
  hardware seams and the only one that vends. Nothing it does waits on the
  network: `run_once` reads the cache and writes the queue, and the queue's
  flush is best effort by design (`sync.PlatformTransport`).

  THE REFRESH THREAD keeps the cache fresh on TWO CADENCES. The slow one
  (`rules_refresh_seconds`) re-reads `GET /lane/rules` -- plans, space class,
  the modules' registers, the full set of open stays with its cursor. The
  fast one (`stays_refresh_seconds`) reads `GET /lane/stays?since=<cursor>`
  -- every stay opened or closed since, so the exit lane learns an entry
  time not long after the entry lane sent it. The fast interval IS the size
  of the class of cars that entered too recently to be priced at the
  barrier; `config.py` says so where the number is set.

A REFRESH THAT COULD NOT RUN IS SAID, NEVER HIDDEN. Each cadence keeps its
last outcome on `RunnerState`, readable by whoever serves this lane's
surfaces, and logs it; the cache is left exactly as it was ("stale rules are
still rules", and `is_stale` is what eventually stops the lane trusting
them). It is not appended to the event outbox, because the platform accepts
a closed set of event kinds and `refresh_failed` is not among them -- a lane
ahead of its platform is refused 400 -- and it is not a health code, because
`MalfunctionCode` is a closed set pinned by the gate agent. Both are named
here as the places it would go; neither is changed by this round.

A THREAD THAT DIES SILENTLY IS A LANE THAT STOPS SERVING CARS WITH ITS
HEALTH ROUTE STILL ANSWERING. So neither thread is allowed to die on an
exception: each turn is guarded, the exception is logged with its traceback
and counted on the state, and the loop goes round again. What a raise on the
lane thread MEANS -- a car at the barrier and the software that was serving
it gone -- is exactly why the guard is there and why the count is published.

No durable cache and no exit decision: the first is its own round (the cache
is a dict, replaced on every refresh and gone on restart, and this loop does
not change that), and the second reads what this loop keeps fresh and is not
written yet.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .controller import LaneController
from .platform_client import PlatformClient
from .sync import sync_rules, sync_stays

log = logging.getLogger(__name__)

#: How long the lane thread waits for a vehicle before sampling the loops and
#: going round again. Short, so a `stop()` is honoured promptly and the idle
#: turn's loop observations are frequent; not a site setting, because nothing
#: about a site changes what one turn of the loop is for.
LANE_POLL_S = 1.0


@dataclass
class CadenceState:
    """What the last read on one cadence did."""

    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    last_attempt_at: float | None = None
    last_success_at: float | None = None
    last_error: str | None = None


@dataclass
class RunnerState:
    """The loop's own account of itself, for a surface that wants to publish it."""

    rules: CadenceState = field(default_factory=CadenceState)
    stays: CadenceState = field(default_factory=CadenceState)
    #: Turns of the lane thread that raised. Each is logged with its
    #: traceback; this is the count a surface can show.
    lane_turn_errors: int = 0
    lane_turns: int = 0


class LaneRunner:
    """Runs a `LaneController` and keeps its cache fresh, until told to stop.

    `client` is None for a standalone lane: the lane thread runs and nothing
    refreshes, because there is nothing to refresh from -- which is the
    supported standalone product, said plainly rather than a loop that logs a
    failure every five seconds against a platform that was never configured.
    """

    def __init__(
        self,
        controller: LaneController,
        *,
        client: PlatformClient | None,
        rules_refresh_s: float,
        stays_refresh_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if stays_refresh_s > rules_refresh_s:
            raise ValueError("stays_refresh_s must not exceed rules_refresh_s")
        self.controller = controller
        self.client = client
        self.rules_refresh_s = rules_refresh_s
        self.stays_refresh_s = stays_refresh_s
        self.state = RunnerState()
        self._clock = clock
        self._stop = threading.Event()
        # Injectable so a test can run the refresh cadence against a fake
        # clock without waiting; the default waits on the stop event so a
        # stop() during a sleep is honoured at once.
        self._sleep = sleep or (lambda seconds: self._stop.wait(seconds))
        self._threads: list[threading.Thread] = []
        self._next_rules = 0.0
        self._next_stays = 0.0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._threads:
            raise RuntimeError("already started")
        self._stop.clear()
        lane = threading.Thread(target=self._lane_loop, name="lane", daemon=True)
        self._threads.append(lane)
        if self.client is not None:
            refresh = threading.Thread(target=self._refresh_loop, name="refresh", daemon=True)
            self._threads.append(refresh)
        for thread in self._threads:
            thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout)
        self._threads = []

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    # -- the lane thread ------------------------------------------------------

    def _lane_loop(self) -> None:
        while not self._stop.is_set():
            self.lane_turn()

    def lane_turn(self) -> None:
        """One turn: serve a vehicle if one arrives, else sample the loops."""
        self.state.lane_turns += 1
        try:
            self.controller.run_once(timeout=LANE_POLL_S)
        except Exception:  # noqa: BLE001 -- the guard IS the point; see the module docstring
            self.state.lane_turn_errors += 1
            log.exception("a turn of the lane loop raised; the loop goes round again")

    # -- the refresh thread -------------------------------------------------------

    def _refresh_loop(self) -> None:
        self.prime_schedule()
        while not self._stop.is_set():
            self.refresh_tick(self._clock())
            wait = max(0.0, min(self._next_rules, self._next_stays) - self._clock())
            self._sleep(wait)

    def prime_schedule(self) -> None:
        """The first read of the slow cadence happens NOW, not one interval from
        now: a lane that has just started holds nothing, and a barrier that
        opens on a cache it has not filled yet is a barrier deciding from
        nothing. The rules read carries the full stays set, so the fast
        cadence starts one interval later."""
        now = self._clock()
        self._next_rules = now
        self._next_stays = now + self.stays_refresh_s

    def refresh_tick(self, now: float) -> str | None:
        """One turn of the refresh thread at `now`: the slow read if it is due
        (and the fast one is then re-based on it, since the full set just
        landed), else the fast read if it is due, else nothing. Returns which
        ran, for a caller that wants to know."""
        if now >= self._next_rules:
            self.refresh_rules()
            self._next_rules = now + self.rules_refresh_s
            self._next_stays = now + self.stays_refresh_s
            return "rules"
        if now >= self._next_stays:
            self.refresh_stays()
            self._next_stays = now + self.stays_refresh_s
            return "stays"
        return None

    def refresh_rules(self) -> bool:
        """The slow cadence, once. True if the cache was refreshed."""
        return self._refresh(
            self.state.rules, "rules", lambda: sync_rules(self.client, self.controller.cache)
        )

    def refresh_stays(self) -> bool:
        """The fast cadence, once. True if the delta was applied."""
        return self._refresh(
            self.state.stays, "stays", lambda: sync_stays(self.client, self.controller.cache)
        )

    def _refresh(self, cadence: CadenceState, name: str, read: Callable[[], dict | None]) -> bool:
        cadence.attempted += 1
        cadence.last_attempt_at = self._clock()
        try:
            answer = read()
        except Exception as err:  # noqa: BLE001 -- see the module docstring
            cadence.failed += 1
            cadence.last_error = f"{type(err).__name__}: {err}"
            log.exception("the %s refresh raised; the cache is as it was", name)
            return False
        if answer is None:
            # `sync_rules` / `sync_stays` already logged why, and left the
            # cache alone. Counted here so a surface can say how long ago the
            # last GOOD read was.
            cadence.failed += 1
            cadence.last_error = "platform unreachable or refused; the cache is as it was"
            return False
        cadence.succeeded += 1
        cadence.last_success_at = self._clock()
        cadence.last_error = None
        return True
