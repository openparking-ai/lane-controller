"""The decision cache on disk: what a lane decides from survives a restart.

`DecisionCache` said of itself that its storage "will become something durable
on the Jetson (SQLite on the controller is the intended shape)". Until this
existed the offline guarantee was a warm-process illusion: a lane that lost
the platform kept deciding from what it held, and a lane that then restarted
-- a power cut, a watchdog, an update -- came back holding nothing, with the
platform still unreachable and a car at the barrier. This is that store.

WHAT IS PERSISTED IS THE WHOLE CACHE, AS ONE. Rules, default action, plans,
space class, entitlement registers, open stays with their cursor, and the two
timestamps that say when each half was last refreshed. Written as one
transaction after every refresh that changed it, so a restart finds either
the previous state or the new one and never a half of each; read back once,
at construction, and the in-memory dicts are the source for every decision
after that. The barrier's path reads memory; the disk is written on the
refresh thread.

THE TIMESTAMPS TRAVEL WITH THE DATA. A cache restored from disk is as old as
its last refresh, not as old as the restart: `is_stale` counts from the
refresh, so a lane that comes back after two days holds a cache it does not
trust, and says so on every vehicle (`stale_rules`), rather than a cache that
looks fresh because it was just loaded.

WHAT THE CACHE HOLDS AT REST IS PERSONAL DATA, AND ITS BOUND IS ONE NUMBER.
Pass-holder plates, monthly vehicles and every open stay's plate or ticket
are on the box. The platform's retention purge does not reach this file, so
the lane's own rule has to, and it is `rules_max_age_seconds` -- the same
number after which the lane stops trusting the cache. A cache older than that
is not held either: it is wiped from memory and from disk, on start and on
every refresh tick, and the file is replaced whole on every successful
refresh, so a plate that left the register or a stay that closed is gone
from the box at the next read. One bound, stated once: the cache is held
exactly as long as it is trusted, and not a second longer.

EXCEPT THE GARAGE'S ENTRY SETTINGS, which are not personal data: whether it
takes transients, its default for a car with no rule, and this lane's closing.
The entry is decided on them, so they outlive the wipe -- in memory and in the
file, written back alone -- and a lane two days into an outage still opens for
every car at a garage that takes transients, and still keeps a `full` lane
shut (`DecisionCache.clear`). AND A PASS-ONLY GARAGE'S REGISTERS, which are
personal data, outlive it too while the platform cannot be reached: there they
are the only way a car gets in. The first rules read that answers wipes them
and holds only what it carries; a platform that refuses the lane outright
(401, 403, 404) wipes them at once; and past `[lane]
pass_registers_offline_days` after the platform last confirmed them they go
anyway, on a restart too.

THE DIRECTORY IS THE PROCESS'S OWN, OR THE STORE REFUSES TO OPEN. A cache row
is `plate -> allow`: a line somebody wrote into this file by hand OPENS THE
BARRIER for that plate, with no platform and no register behind it. So the
same rule Vehicle ID applies to its queue directory applies here, copied
rather than re-derived: the leaf owned by this process and mode 0700, every
ancestor owned by this process or by root and not writable by others (a
sticky world-writable ancestor such as /tmp is not writable in the sense
that matters), a relative path refused outright, the file itself 0600 and
narrowed on every write. Refused at construction, before the port opens: a
lane that starts and then vends on a forged row is worse than one that does
not start.

THE OUTBOX'S SESSION ACTIONS ARE KEPT BESIDE IT (`OutboxStore`), in a file of
their own in the same directory, under the same rule. A stay's open and close
wait in the outbox until the platform takes them, and that outbox lived in
memory only: a restart during an outage lost every one it held. They are not
cache and not bounded by `rules_max_age_seconds`: each is held until the
platform has taken it or refused it, and not after. The pictures kept with an
entry are kept in the same file, on the same terms. Both are written by the
outbox's own disk thread (`events.DiskWriter`), never by the lane's.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
from pathlib import Path

from .events import LaneEvent, Picture

#: The file's mode, applied on every write and not only at creation: SQLite
#: may recreate the file, and a mode set once is a mode that can be undone.
CACHE_FILE_MODE = 0o600
#: The directory's mode: the half that gates the forgery path.
CACHE_DIR_MODE = 0o700

#: The one key under which the whole cache is stored. One row, one transaction,
#: one read: a cache is a single value with several parts, not a table of
#: parts that can be half-written.
_KEY = "decision_cache"
#: The default ceiling on the pictures' share of an outbox file, in bytes
#: (`config.DEFAULT_OUTBOX_PICTURES_MAX_BYTES`, which `serve` passes).
PICTURES_MAX_BYTES = 1 << 30
#: The on-disk shape's version. A store written by a build with a different
#: shape is not read as this one; it is discarded and refilled at the next
#: refresh, which is what a cache is for.
FORMAT = 1


class CacheDirectoryUnsafe(Exception):
    """The cache directory is not this process's own, or the path is not one
    the process can vouch for. Raised at construction, before the port opens.
    There is no flag that turns this off."""


def cache_directory_fault(
    mode: int, owner_uid: int, process_uid: int, *, leaf: bool = True
) -> str | None:
    """Why this directory is not safe to hold the cache, or None if it is.

    The decision, separated from the syscalls so both branches are tested
    without root. Ownership first: a directory somebody else owns is theirs
    to chmod back. Then the mode: the leaf may be no wider than 0700; an
    ancestor may be readable by others but not writable, except that a STICKY
    world-writable ancestor (/tmp) is not writable in the sense that matters.
    """
    trusted_owners = {process_uid} if leaf else {process_uid, 0}
    if owner_uid not in trusted_owners:
        return (
            f"it is owned by uid {owner_uid}, not by this process (uid {process_uid}). A cache "
            "directory somebody else owns is a cache somebody else can write a rule into, and "
            "a hand-written allow opens the barrier."
        )
    if leaf:
        forbidden = 0o077
    elif mode & stat.S_ISVTX:
        forbidden = 0
    else:
        forbidden = 0o022
    if mode & forbidden:
        wider_than = CACHE_DIR_MODE if leaf else 0o755
        return (
            f"it is mode {mode:04o}, which is wider than {wider_than:04o}. Anything that can "
            "write this directory can put an allow for any plate in front of the barrier."
        )
    return None


def ensure_cache_directory(directory: Path) -> Path:
    """Create the cache directory narrow, and refuse a PATH that is not the
    process's own -- every component of it, not just the last one."""
    if not directory.is_absolute():
        raise CacheDirectoryUnsafe(
            f"refusing a relative cache path: {directory} resolves against whatever directory "
            "this process was started in, which nothing here checks and nothing records. Give "
            "an absolute [lane] cache_path."
        )
    directory = directory.resolve()
    if not directory.exists():
        for component in reversed([d for d in (directory, *directory.parents) if not d.exists()]):
            component.mkdir()
            component.chmod(CACHE_DIR_MODE)
    elif not directory.is_dir():
        raise CacheDirectoryUnsafe(f"{directory} exists and is not a directory")
    for component in (directory, *directory.parents):
        info = component.stat()
        fault = cache_directory_fault(
            stat.S_IMODE(info.st_mode), info.st_uid, os.getuid(), leaf=(component == directory)
        )
        if fault:
            raise CacheDirectoryUnsafe(
                f"refusing to hold the decision cache under {component}: {fault} Narrow it, "
                "or point [lane] cache_path somewhere this process owns."
            )
    return directory


class DurableStore:
    """The SQLite file behind a `DecisionCache`.

    `write(state)` stores the whole cache as one row in one transaction and
    narrows the file; `read()` returns the last state written, or None for an
    empty, missing or differently-shaped store; `wipe()` removes the row. A
    connection is opened per call: calls are seconds apart and the lock keeps
    the refresh thread's writes and a wipe from interleaving.
    """

    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        self.directory = ensure_cache_directory(path.parent)
        self.path = self.directory / path.name
        self._lock = threading.Lock()
        with self._lock:
            self._with_connection(lambda c: c.execute(
                "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, format INTEGER NOT NULL, "
                "state TEXT NOT NULL)"
            ))

    def _with_connection(self, action):
        return _touch(self.path, action)

    def write(self, state: dict) -> None:
        text = json.dumps(state, sort_keys=True)
        with self._lock:
            self._with_connection(lambda c: c.execute(
                "INSERT OR REPLACE INTO cache (key, format, state) VALUES (?, ?, ?)",
                (_KEY, FORMAT, text),
            ))

    def read(self) -> dict | None:
        with self._lock:
            row = self._with_connection(lambda c: c.execute(
                "SELECT format, state FROM cache WHERE key = ?", (_KEY,)
            ).fetchone())
        if row is None or row[0] != FORMAT:
            return None
        try:
            state = json.loads(row[1])
        except ValueError:
            return None
        return state if isinstance(state, dict) else None

    def wipe(self) -> None:
        with self._lock:
            self._with_connection(lambda c: c.execute("DELETE FROM cache WHERE key = ?", (_KEY,)))


def _touch(path: Path, action):
    """One connection, one transaction, `action` run inside it -- and the file
    narrowed AFTER every touch, not only at creation. The one way both stores
    here open their file."""
    connection = sqlite3.connect(path)
    try:
        with connection:
            result = action(connection)
    finally:
        connection.close()
    os.chmod(path, CACHE_FILE_MODE)
    return result


def outbox_path(cache_path: str | Path) -> Path:
    """Where a lane whose cache is at `cache_path` keeps its outbox: the same
    directory, and the cache file's name with `.outbox` before its suffix --
    `decision-cache.sqlite` beside `decision-cache.outbox.sqlite` -- so two
    lanes sharing a directory keep two outboxes."""
    path = Path(cache_path)
    return path.with_name(f"{path.stem}.outbox{path.suffix}")


class OutboxStore:
    """The outbox's session actions on disk: every stay open and close the
    platform has not taken yet.

    `EventQueue` calls `add` as it records one, `remove` once the platform has
    taken it or refused it (counted and logged, as every refusal is), and
    `held` once, at start: what the file held when the process stopped is
    queued again, in the order it happened. A row is one action, keyed on its
    event id -- the id the platform deduplicates on -- so an action that was
    delivered and not yet removed when the process stopped is sent again and
    absorbed.

    LOG EVENTS ARE NOT KEPT HERE. They are bounded and dropped first by design
    (`EventQueue`); the money is in the session actions.

    THE PICTURES KEPT WITH AN ENTRY ARE KEPT HERE TOO, in a table of their
    own: the pictures of the stays this file holds, which a restart during an
    outage used to lose with the process (they lived in memory, 200 at most,
    sent all or nothing). Each is a row until the platform has taken it or
    refused it, read back a few at a time (`held_pictures`). Their share of
    the file has a CEILING in bytes (`pictures_max_bytes`, `[lane]
    outbox_pictures_max_bytes`): past it the oldest are dropped and counted,
    so a long outage can never leave the stays' opens and closes without
    room. The platform's retention starts when it has them.

    The directory rule and the file mode are the cache's, because it is the
    cache's directory: what a row holds is a plate, a ticket or a picture of a
    car, and a row written by hand would be a stay this lane never saw.

    EVERY WRITE COMES FROM `events.DiskWriter`'s thread, never the lane's.
    """

    def __init__(self, path: str | Path, *, pictures_max_bytes: int = PICTURES_MAX_BYTES) -> None:
        path = Path(path)
        self.directory = ensure_cache_directory(path.parent)
        self.path = self.directory / path.name
        #: THE CEILING on the pictures' share of this file, in bytes: past it
        #: the oldest are dropped (`add_picture`), so the stays always have room.
        self.pictures_max_bytes = pictures_max_bytes
        self._lock = threading.Lock()
        with self._lock:
            _touch(self.path, lambda c: c.execute(
                "CREATE TABLE IF NOT EXISTS outbox (event_id TEXT PRIMARY KEY, at REAL NOT NULL, "
                "kind TEXT NOT NULL, lane_id TEXT NOT NULL, detail TEXT NOT NULL)"
            ))
            _touch(self.path, lambda c: c.execute(
                "CREATE TABLE IF NOT EXISTS pictures (picture_ref TEXT PRIMARY KEY, "
                "event_id TEXT NOT NULL, taken_at REAL NOT NULL, size INTEGER NOT NULL, "
                "image BLOB NOT NULL)"
            ))
            _touch(self.path, lambda c: c.execute(
                "CREATE TABLE IF NOT EXISTS cancels (exit_id TEXT PRIMARY KEY, at REAL NOT NULL)"
            ))

    def add(self, event: LaneEvent) -> None:
        detail = json.dumps(event.detail, sort_keys=True)
        with self._lock:
            _touch(self.path, lambda c: c.execute(
                "INSERT OR REPLACE INTO outbox (event_id, at, kind, lane_id, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (event.event_id, event.at, event.kind, event.lane_id, detail),
            ))

    def remove(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        with self._lock:
            _touch(self.path, lambda c: c.executemany(
                "DELETE FROM outbox WHERE event_id = ?", [(event_id,) for event_id in event_ids]
            ))

    def held(self) -> list[LaneEvent]:
        with self._lock:
            rows = _touch(self.path, lambda c: c.execute(
                "SELECT kind, lane_id, at, detail, event_id FROM outbox ORDER BY at, rowid"
            ).fetchall())
        return [
            LaneEvent(kind=kind, lane_id=lane_id, at=at, detail=json.loads(detail),
                      event_id=event_id)
            for kind, lane_id, at, detail, event_id in rows
        ]

    # -- the cancels the platform has not confirmed -------------------------------

    def add_cancel(self, exit_id: str, at: float) -> None:
        """An exit case whose payment is still to come off the reader: kept
        until the platform confirms the cancel, so a restart asks again. An
        exit case's id, nothing about the car."""
        with self._lock:
            _touch(self.path, lambda c: c.execute(
                "INSERT OR IGNORE INTO cancels (exit_id, at) VALUES (?, ?)", (exit_id, at)
            ))

    def remove_cancel(self, exit_id: str) -> None:
        with self._lock:
            _touch(self.path, lambda c: c.execute(
                "DELETE FROM cancels WHERE exit_id = ?", (exit_id,)
            ))

    def held_cancels(self) -> list[str]:
        with self._lock:
            rows = _touch(self.path, lambda c: c.execute(
                "SELECT exit_id FROM cancels ORDER BY at, rowid"
            ).fetchall())
        return [exit_id for (exit_id,) in rows]

    # -- the pictures ---------------------------------------------------------

    def add_picture(self, picture: Picture) -> int:
        """Keep one picture, under the CEILING: past `pictures_max_bytes` the
        OLDEST pictures are deleted, in the same transaction, until the rest
        fit -- the newest is the one most likely still needed, and a picture
        is never worth a stay. A picture larger than the ceiling alone is not
        kept. Returns how many pictures this dropped, the new one included."""
        size = len(picture.image_bytes)
        if size > self.pictures_max_bytes:
            return 1

        def keep(connection) -> int:
            connection.execute(
                "INSERT OR REPLACE INTO pictures (picture_ref, event_id, taken_at, size, image) "
                "VALUES (?, ?, ?, ?, ?)",
                (picture.picture_ref, picture.event_id, picture.taken_at, size,
                 sqlite3.Binary(picture.image_bytes)),
            )
            total = connection.execute("SELECT COALESCE(SUM(size), 0) FROM pictures").fetchone()[0]
            dropped = 0
            while total > self.pictures_max_bytes:
                rowid, oldest = connection.execute(
                    "SELECT rowid, size FROM pictures ORDER BY rowid LIMIT 1"
                ).fetchone()
                connection.execute("DELETE FROM pictures WHERE rowid = ?", (rowid,))
                total -= oldest
                dropped += 1
            return dropped

        with self._lock:
            return _touch(self.path, keep)

    def held_pictures(self, limit: int, *, after: int = 0) -> list[tuple[int, Picture]]:
        """The oldest `limit` pictures not yet taken PAST position `after`, in
        the order they were kept, each with its position: a flush pages through
        by position, so a picture whose removal failed never holds the ones
        behind it."""
        with self._lock:
            rows = _touch(self.path, lambda c: c.execute(
                "SELECT rowid, picture_ref, event_id, taken_at, image FROM pictures "
                "WHERE rowid > ? ORDER BY rowid LIMIT ?", (after, limit)
            ).fetchall())
        return [
            (rowid, Picture(event_id=event_id, taken_at=taken_at, image_bytes=bytes(image),
                            picture_ref=picture_ref))
            for rowid, picture_ref, event_id, taken_at, image in rows
        ]

    def remove_pictures(self, picture_refs: list[str]) -> None:
        if not picture_refs:
            return
        with self._lock:
            _touch(self.path, lambda c: c.executemany(
                "DELETE FROM pictures WHERE picture_ref = ?", [(ref,) for ref in picture_refs]
            ))

    def picture_count(self) -> int:
        with self._lock:
            return _touch(self.path, lambda c: c.execute(
                "SELECT count(*) FROM pictures"
            ).fetchone()[0])
