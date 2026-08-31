"""Can a lane that is not ours take the seat?

This file is the answer. One consumer (`tests/lane_consumer.py`) reads all four
routes from OUR lane and from a third party's (`tests/third_party_lane/`), and
every assertion below runs against both through the same code.

**If any test here needs a special case for either lane, the contract is
wrong.** That is the property being measured, and it is why the two lanes are
parametrised rather than tested separately: a test written twice would let the
two drift and would prove nothing about the seat.

The third-party lane is deliberately unlike ours -- no loops, no confirmation,
no identity service, no platform, and a reason from its own vocabulary. Those
are exactly the differences that would force a special case if the contract had
baked our lane's shape into it.
"""

from __future__ import annotations

import ast
import urllib.error
from pathlib import Path

import pytest

from lane_consumer import Escalate, LaneConsumer
from lane_controller import EventQueue, VehicleIdClient
from lane_controller.contract import (
    CONTRACT_VERSION,
    OUTCOMES,
    REQUIRED_REASONS,
    HealthState,
    MalfunctionCode,
    Source,
    TransitState,
)
from lane_controller.service import LaneService
from lane_controller.service import make_server as our_server
from serving import serving
from test_lane_contract import full_lane
from third_party_lane import ThirdPartyLane
from third_party_lane.lane import VENDOR_REASON
from third_party_lane.lane import make_server as their_server

THIRD_PARTY_DIR = Path(__file__).resolve().parent / "third_party_lane"


class _ARecordingTransport:
    """An outbox transport that delivers nothing. Enough to BE one.

    `has_platform` is the presence of a transport, not a successful delivery --
    a lane whose platform is unreachable still has one, and reporting it as
    standalone would be a different lane.
    """

    def send(self, events):
        return False

#: What the contract PUBLISHES as closed, and therefore the only thing an
#: implementer may take from our package. Everything else about a foreign lane
#: is its own.
ALLOWED_IMPORTS = {"MalfunctionCode", "NEVER_ALARM"}


@pytest.fixture(params=["ours", "theirs"])
def lane_url(request):
    """The same two-line setup for both lanes, and nothing else differs.

    Our lane is served by `LaneService`; theirs by its own handler. Both are
    reached over a socket by URL -- no in-process shortcut for ours, because a
    test that called our service directly and theirs over HTTP would be
    comparing two different things.
    """
    if request.param == "ours":
        controller = full_lane()
        controller.run_once()
        server = our_server(LaneService(controller), port=0)
    else:
        server = their_server(ThirdPartyLane(), port=0)
    with serving(server) as base:
        yield base


# ---------------------------------------------------------------------------
# All four routes, from both lanes, through one consumer
# ---------------------------------------------------------------------------


def test_a_consumer_reads_all_four_routes_from_either_lane(lane_url):
    consumer = LaneConsumer(lane_url)

    lane = consumer.lane()
    assert lane["contract_version"] == CONTRACT_VERSION
    assert lane["direction"] in ("entry", "exit")
    assert set(lane["capabilities"]) == {
        "confirms_entry",
        "has_identity_service",
        "has_platform",
        "has_display",
        "can_vend",
    }
    # The geometry is a lane's OWN. Ours has five keys; a lane with no loops
    # publishes none, and a consumer must not require any of them.
    assert isinstance(lane["geometry"], dict)

    state = consumer.state()
    assert state["transit"]["state"] in {s.value for s in TransitState}
    if state["decision"] is not None:
        assert state["decision"]["outcome"] in OUTCOMES

    health = consumer.health()
    assert {entry["code"] for entry in health["codes"]} == {c.value for c in MalfunctionCode}
    for entry in health["codes"]:
        assert entry["source"] in {s.value for s in Source}
        assert entry["state"] in {s.value for s in HealthState}

    events = consumer.events(0)
    assert events["reset"] is False
    assert isinstance(events["dropped"], int)
    assert [item["cursor"] for item in events["events"]] == sorted(
        item["cursor"] for item in events["events"]
    )


def test_a_consumer_asks_whether_a_lane_will_act_and_gets_the_truth(lane_url):
    """`can_vend` and the route AGREE, on whichever lane this is.

    The question a consumer would actually ask, asked identically of both --
    and this is the assertion that changed shape in version 2. It used to be
    "neither lane will do anything". Now ours will and theirs will not, and
    what the contract has to guarantee is that the capability tells you which
    WITHOUT trying it.
    """
    consumer = LaneConsumer(lane_url)
    can_vend = consumer.lane()["capabilities"]["can_vend"]
    answered = consumer.post("/v1/lane/vend")
    if can_vend:
        assert answered not in (404, 405), (
            "a lane that says it can vend must serve the route it names"
        )
    else:
        assert answered in (404, 405), (
            "a lane that says it cannot vend must not serve a route that opens a barrier"
        )


def test_a_cursor_ahead_of_either_lane_says_reset(lane_url):
    """One cursor policy, both lanes."""
    consumer = LaneConsumer(lane_url)
    current = consumer.events(0)["cursor"]
    assert consumer.events(current + 1)["reset"] is True
    assert consumer.events(current)["reset"] is False


def test_a_consumer_never_acts_on_an_unknown_or_never_alarm_code(lane_url):
    """`unknown` is not `ok`, on either lane.

    The third-party lane answers `unknown` for every code, which is the truth
    for a lane with none of this instrumentation. A consumer that read those as
    healthy would report a clean lane it has measured nothing about.
    """
    health = LaneConsumer(lane_url).health()
    for entry in health["codes"]:
        if entry["state"] == "unknown":
            assert LaneConsumer.actionable(entry) is False
        if entry["never_alarm"]:
            assert entry["caveat"]
            assert LaneConsumer.actionable({**entry, "state": "active"}) is False


# ---------------------------------------------------------------------------
# The two lanes' differences, read by the same code
# ---------------------------------------------------------------------------


def test_a_reason_outside_our_set_escalates_rather_than_being_guessed_at():
    """The case the L1 named, exercised on a real foreign payload.

    Our lane's reason is interpreted. Theirs is not in the subset this consumer
    was built against, and the consumer hands the vehicle to a human -- it does
    not map `barrier_operator_intervened` onto the nearest code it knows.
    """
    controller = full_lane()
    controller.run_once()
    with serving(our_server(LaneService(controller), port=0)) as base:
        ours = LaneConsumer(base).state()["decision"]
        assert LaneConsumer(base).interpret(ours, REQUIRED_REASONS) in REQUIRED_REASONS

    with serving(their_server(ThirdPartyLane(), port=0)) as base:
        theirs = LaneConsumer(base).state()["decision"]
        assert theirs["fallback"] is None
        with pytest.raises(Escalate) as escalated:
            LaneConsumer(base).interpret(theirs, REQUIRED_REASONS)
        assert str(escalated.value) == VENDOR_REASON


def test_a_lane_that_cannot_confirm_anything_says_so_and_is_still_readable():
    """`confirms_entry: false` and a transit that is always `none`.

    The other half of the L1's finding: our loops vocabulary is ours, and a
    lane without it must be a first-class citizen of this contract rather than
    a degraded one.
    """
    with serving(their_server(ThirdPartyLane(), port=0)) as base:
        lane = LaneConsumer(base).lane()
        state = LaneConsumer(base).state()

    assert lane["capabilities"]["confirms_entry"] is False
    assert lane["capabilities"]["has_identity_service"] is False
    assert lane["capabilities"]["has_platform"] is False
    assert lane["geometry"] == {}
    assert state["transit"] == {"state": "none", "since": None}


def test_our_lane_and_theirs_disagree_on_every_capability_that_can_differ():
    """The control for the parametrised tests above.

    If the two lanes answered the same everywhere, "the same consumer reads
    both" would be a claim about one lane written twice. So ours is wired the
    other way on all three capabilities that can differ -- closing loops, a
    Vehicle ID client, an outbox transport -- and the consumer needed no branch
    for any of it.

    It is also the control for the capabilities themselves: each of the three
    is DERIVED from the wiring, and a derivation that could not answer both
    ways would be a constant.
    """
    controller = full_lane(events=EventQueue(transport=_ARecordingTransport()))
    controller.identifier = VehicleIdClient("http://127.0.0.1:1")
    with serving(our_server(LaneService(controller), port=0)) as base:
        ours = LaneConsumer(base).lane()["capabilities"]
    with serving(their_server(ThirdPartyLane(), port=0)) as base:
        theirs = LaneConsumer(base).lane()["capabilities"]

    differing = {key for key in ours if ours[key] != theirs[key]}
    assert differing == {
        "confirms_entry",
        "has_identity_service",
        "has_platform",
        # The one this round adds, and it is now a fourth axis the two lanes
        # disagree on: implementing the act side is OPTIONAL, and a consumer
        # must read the capability rather than assume either answer.
        "can_vend",
    }
    # Neither has a display.
    assert ours["has_display"] is False and theirs["has_display"] is False
    assert ours["can_vend"] is True and theirs["can_vend"] is False


# ---------------------------------------------------------------------------
# The stub is independent, and that is a check
# ---------------------------------------------------------------------------


def test_the_third_party_lane_imports_nothing_of_ours_but_the_published_sets():
    """A stub built on our machinery would prove nothing about a foreign lane.

    Read out of the source rather than promised in a docstring: every `from
    lane_controller...` import in the package is enumerated and must be one of
    the contract's published closed sets.
    """
    imported: set[str] = set()
    for path in sorted(THIRD_PARTY_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "lane_controller"
            ):
                imported.update(alias.name for alias in node.names)
            if isinstance(node, ast.Import):
                assert not any(
                    alias.name.startswith("lane_controller") for alias in node.names
                ), f"{path.name} imports lane_controller wholesale"

    assert imported == ALLOWED_IMPORTS, (
        f"the third-party lane imports {sorted(imported)} from our package. Only the "
        f"contract's published closed sets ({sorted(ALLOWED_IMPORTS)}) are what an "
        "implementer reads; anything else makes this a copy of our lane rather than "
        "a foreign one."
    )
    # The control: the sweep can see an import at all.
    assert imported, "the sweep found no imports; it is not looking at the right files"


def test_the_third_party_lane_can_be_broken(monkeypatch):
    """Prove the seat test can fail. A stub that satisfies everything is a
    fixture that measures nothing.

    `scripts/contract_fail_control.py` runs the whole suite under each break;
    this is the in-suite proof that the breaks reach the payload at all.
    """
    lane = ThirdPartyLane()
    assert "direction" in lane.describe()
    monkeypatch.setenv("BREAK_THIRD_PARTY_LANE", "no_direction")
    assert "direction" not in lane.describe()

    monkeypatch.setenv("BREAK_THIRD_PARTY_LANE", "short_health")
    assert len(lane.health()["codes"]) == len(MalfunctionCode) - 1

    monkeypatch.setenv("BREAK_THIRD_PARTY_LANE", "our_reason")
    assert lane.state()["decision"]["reason"] in REQUIRED_REASONS


def test_a_broken_stub_is_caught_by_the_route_that_reads_it(monkeypatch):
    """And the break is caught where it matters: through the consumer.

    The control above proves the break reaches the payload. This proves the
    payload reaches the assertion -- without it, a stub could be breakable and
    the seat test still blind to it.
    """
    monkeypatch.setenv("BREAK_THIRD_PARTY_LANE", "short_health")
    with serving(their_server(ThirdPartyLane(), port=0)) as base:
        health = LaneConsumer(base).health()
    assert {entry["code"] for entry in health["codes"]} != {c.value for c in MalfunctionCode}


def test_a_lane_that_is_not_running_is_not_a_lane_that_is_fine():
    """A consumer reading nothing gets an error, not an empty answer.

    Stated because the alternative -- a client that swallows a connection
    failure and returns `{}` -- would make every assertion in this file pass
    against a lane that is switched off.
    """
    consumer = LaneConsumer("http://127.0.0.1:1")
    with pytest.raises(urllib.error.URLError):
        consumer.lane()
