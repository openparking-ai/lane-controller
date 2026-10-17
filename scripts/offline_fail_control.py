#!/usr/bin/env python3
"""The control for the offline guarantees.

Runs the offline suite three times: once intact, where it must pass, and twice
with the queue deliberately broken, where it must fail. A guarantee that has
never been seen to fail is not known to be a guarantee.

  drop  -- the queue clears whether or not delivery succeeded. This is what
           "offline-tolerant" looks like when it is only claimed.
  noid  -- delivery happens twice with a regenerated event id, which is an
           acknowledgement lost on the way back plus no idempotency key.
  outbox_not_kept      -- the session actions are queued in memory only, so
                          a restart during an outage loses them.
  outbox_not_replayed  -- the outbox file is written and never read back.
  outbox_not_wired     -- `serve` keeps the outbox in memory whatever the
                          cache does.
  all_or_nothing       -- the whole queue in one send, and everything kept on
                          the first call that does not get through.
  one_log_post         -- every queued log event in one request, however large.
  no_split_on_413      -- a chunk the platform answers 413 is dropped.
  no_last_send         -- `serve` stops without one last send.
  terminate_kills      -- SIGTERM ends `serve` on the spot.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_offline.py"]
BREAKAGES = [
    ("drop", "the queue drops what it could not deliver"),
    ("noid", "delivery is retried without an idempotency key"),
    ("outbox_not_kept", "the queued stays are kept in memory only"),
    ("outbox_not_replayed", "the outbox file is never read back"),
    ("outbox_not_wired", "serve keeps the outbox in memory"),
    ("all_or_nothing", "a failed send keeps what was already taken"),
    ("one_log_post", "an outage's log events go in one request"),
    ("no_split_on_413", "a chunk answered 413 is dropped"),
    ("no_last_send", "serve stops without a last send"),
    ("terminate_kills", "SIGTERM ends serve on the spot"),
]


failures = 0

print("== control A: the offline suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_OFFLINE_QUEUE": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_OFFLINE_QUEUE": mode})):
        failures += 1

if failures:
    print(f"\n{failures} control(s) failed. Do not trust the offline tests.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the offline suite fails when the queue is broken, as it must.")
