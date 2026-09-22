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
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
from pathlib import Path

#: The file's mode, applied on every write and not only at creation: SQLite
#: may recreate the file, and a mode set once is a mode that can be undone.
CACHE_FILE_MODE = 0o600
#: The directory's mode: the half that gates the forgery path.
CACHE_DIR_MODE = 0o700

#: The one key under which the whole cache is stored. One row, one transaction,
#: one read: a cache is a single value with several parts, not a table of
#: parts that can be half-written.
_KEY = "decision_cache"
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
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                result = action(connection)
        finally:
            connection.close()
        # Narrowed AFTER every touch, not only at creation.
        os.chmod(self.path, CACHE_FILE_MODE)
        return result

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
