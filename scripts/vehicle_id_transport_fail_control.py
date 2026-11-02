#!/usr/bin/env python3
"""The control for where the lane's reads go, and with what (OP-5-9 / OP-9-8).

Runs tests/test_vehicle_id_client.py once intact, where it must pass, and once
per break, where it must fail. Every read carries pictures of a car, and a
token when one is configured.

  redirect_followed  a 302/303 from the service is followed, and the request
                     and its token go wherever it names.
  proxy_used         `http_proxy` is obeyed: the frames go through a proxy.
  no_bearer          the configured token is never sent.
  any_address        any [lane] vehicle_id_url is accepted, plain http to
                     another machine included.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_vehicle_id_client.py"]
BREAKAGES = [
    ("redirect_followed", "a redirect is followed"),
    ("proxy_used", "the proxy settings are obeyed"),
    ("no_bearer", "the token is never sent"),
    ("any_address", "any vehicle_id_url is accepted"),
]


failures = 0

print("== control A: the vehicle-id client suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_VEHICLE_ID": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_VEHICLE_ID": mode})):
        failures += 1

if failures:
    print(
        f"\n{failures} control(s) failed. Do not trust the vehicle-id transport tests.",
        file=sys.stderr,
    )
    sys.exit(1)
print("\nall controls OK — the vehicle-id client suite fails when its transport is broken.")
