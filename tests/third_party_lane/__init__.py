"""A lane that is NOT ours, implementing the contract and nothing else.

This package is the proof of the seat. It is a lane built by somebody who read
`docs/CONTRACT.md` and has none of our machinery: no `LaneController`, no
`LaneService`, no loops, no `Fallback`, no decision logic, no config file. It
answers four routes out of hand-built dictionaries.

It is deliberately NOT a good lane:

  * `confirms_entry: false` -- it has no loops after the barrier and cannot say
    whether a vehicle went through;
  * `geometry: {}` -- it has no loop geometry to publish, and none of our
    vocabulary for one;
  * its `reason` is outside our closed subset, because its vendor named its
    own states;
  * it has no identity service and no platform.

`tests/test_third_party_seat.py` reads it and reads ours with the SAME consumer
code. If that test ever needs a special case for either lane, the contract is
wrong -- which is the whole reason this exists.

**What it does import from us**, and why that is not a loss of independence:
`MalfunctionCode` and `NEVER_ALARM` are the contract's PUBLISHED closed sets.
An implementer reads those the same way they read the document -- shipping a
health payload complete with respect to a contract version means knowing what
that version's codes are. `tests/test_third_party_seat.py` asserts that this
package imports nothing else from `lane_controller`, so the boundary is a check
rather than a promise.
"""

from .lane import ThirdPartyLane

__all__ = ["ThirdPartyLane"]
