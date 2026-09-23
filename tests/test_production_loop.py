"""The production loop: the lane keeps serving cars and the cache keeps fresh.

Until this round `sync_rules` had one caller -- the demo -- and `run_forever`
had none; `serve` published the contract over a lane nothing drove. Now
`serve` wires a real `PlatformClient` when the configuration names one, and
`LaneRunner` runs the lane on one thread and refreshes the cache on two
cadences on another. Each claim below is paired with the case that would
falsify it, and `scripts/refresh_fail_control.py` breaks the loop in four ways
and requires this file to go red.

Nothing here decides an exit or persists a cache: those are their own rounds.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from fake_platform import FakePlatform
from lane_controller import CameraConfig, DecisionCache, GateConfig, LaneConfig, cli
from lane_controller.config import DEFAULT_RULES_REFRESH_S, DEFAULT_STAYS_REFRESH_S
from lane_controller.platform_client import PlatformClient
from lane_controller.runner import LaneRunner
from lane_controller.simulated import StubVehicleIdentifier
from lane_controller.sync import PlatformTransport, sync_rules, sync_stays
from lane_controller.vehicle_id_client import VehicleIdClient

A_REGISTER = {"garage": "gp-1", "registrations": [{"vehicle_identity": "PASS-1", "pass": "p-1"}]}


def a_config(**overrides) -> LaneConfig:
    fields = dict(
        lane_id="lane-loop",
        site_id="site-loop",
        camera=CameraConfig(camera_id="sim-cam", rtsp_url="", frames_per_read=1),
        gate=GateConfig(),
    )
    fields.update(overrides)
    return LaneConfig(**fields)


# ---------------------------------------------------------------------------
# the configuration: two cadences, declared, ordered, inside the staleness bound
# ---------------------------------------------------------------------------


def test_the_two_cadences_have_stated_defaults_and_the_fast_one_is_the_shorter():
    config = a_config()
    assert config.rules_refresh_seconds == DEFAULT_RULES_REFRESH_S == 300.0
    assert config.stays_refresh_seconds == DEFAULT_STAYS_REFRESH_S == 5.0
    assert config.stays_refresh_seconds < config.rules_refresh_seconds
    assert config.rules_refresh_seconds < config.rules_max_age_seconds


@pytest.mark.parametrize(
    ("overrides", "names"),
    [
        ({"stays_refresh_seconds": 400.0}, "stays_refresh_seconds"),  # slower than the slow one
        ({"rules_refresh_seconds": 86_400.0}, "rules_refresh_seconds"),  # at the staleness bound
        ({"rules_refresh_seconds": 0.0}, "rules_refresh_seconds"),
        ({"stays_refresh_seconds": float("nan")}, "stays_refresh_seconds"),
        ({"stays_refresh_seconds": True}, "stays_refresh_seconds"),
    ],
)
def test_a_cadence_that_is_not_a_cadence_is_refused_by_name(overrides, names):
    with pytest.raises(ValueError, match=names):
        a_config(**overrides)


def test_the_cadences_and_the_identifier_address_are_read_from_the_file(tmp_path):
    example = Path(__file__).resolve().parent.parent / "config" / "lane.example.toml"
    text = (
        example.read_text()
        .replace("rules_refresh_seconds = 300", "rules_refresh_seconds = 60")
        .replace("stays_refresh_seconds = 5", "stays_refresh_seconds = 2")
        .replace('# vehicle_id_url = "http://127.0.0.1:8088"', 'vehicle_id_url = "http://127.0.0.1:8088"')
    )
    assert text != example.read_text(), "the premise: every replacement landed"
    path = tmp_path / "lane.toml"
    path.write_text(text)
    config = LaneConfig.from_file(path)
    assert (config.rules_refresh_seconds, config.stays_refresh_seconds) == (60.0, 2.0)
    assert config.vehicle_id_url == "http://127.0.0.1:8088"
    # the example itself carries the defaults and no identifier
    shipped = LaneConfig.from_file(example)
    assert (shipped.rules_refresh_seconds, shipped.stays_refresh_seconds) == (300.0, 5.0)
    assert shipped.vehicle_id_url is None


# ---------------------------------------------------------------------------
# the cache holds what the payload carries, by the payload's rules
# ---------------------------------------------------------------------------


def test_load_payload_holds_plans_class_registers_and_the_stays_with_their_cursor():
    platform = FakePlatform()
    platform.entitlements["garage_pass"] = {"consulted": True, "register": A_REGISTER}
    platform.stay_opened("s-1", plate="CAR-1")
    cache = DecisionCache()
    assert sync_rules(platform, cache) is not None
    assert cache.plans == [{"plan_version": "flat-250-USD", "currency": "USD"}]
    assert cache.space_class == "standard"
    assert cache.default_action == "allow"
    assert cache.entitlements["garage_pass"]["register"] == A_REGISTER
    assert "monthly_billing" not in cache.entitlements, "a module with no register is not held"
    assert cache.entitlements_complete is True
    assert list(cache.stays) == ["s-1"] and cache.stays["s-1"]["plate"] == "CAR-1"
    assert cache.stays_cursor == "1"
    assert not cache.is_stale()


def test_a_module_the_platform_could_not_read_keeps_what_the_cache_already_held():
    """An outage is not an empty register. The payload says `unavailable` and
    carries no `register`; the cache keeps the last register it had, and says
    the read was incomplete. CONTROL: a register that IS present replaces."""
    platform = FakePlatform()
    platform.entitlements["garage_pass"] = {"consulted": True, "register": A_REGISTER}
    cache = DecisionCache()
    sync_rules(platform, cache)
    held = cache.entitlements["garage_pass"]
    # the outage
    platform.entitlements["garage_pass"] = {
        "consulted": True, "unavailable": "exit 2: GARAGE_PASS_DSN is not set", "exit_code": 2,
    }
    platform.entitlements["complete"] = False
    sync_rules(platform, cache)
    assert cache.entitlements["garage_pass"] is held, "the outage replaced the register"
    assert cache.entitlements_complete is False
    # the control: a new register replaces the old one
    platform.entitlements["garage_pass"] = {"consulted": True, "register": {"registrations": []}}
    platform.entitlements["complete"] = True
    sync_rules(platform, cache)
    assert cache.entitlements["garage_pass"]["register"] == {"registrations": []}
    assert cache.entitlements_complete is True


def test_a_delta_adds_an_open_stay_drops_a_closed_one_and_moves_the_cursor():
    platform = FakePlatform()
    platform.stay_opened("s-1", plate="CAR-1")
    cache = DecisionCache()
    sync_rules(platform, cache)
    assert cache.stays_cursor == "1"
    # nothing changed: an empty delta, the cursor stays
    quiet = sync_stays(platform, cache)
    assert quiet == {"since": "1", "cursor": "1", "changes": [], "more": False}
    assert platform.stays_reads == ["1"]
    # CAR-1 leaves, CAR-2 arrives
    platform.stay_closed("s-1")
    platform.stay_opened("s-2", plate="CAR-2")
    answer = sync_stays(platform, cache)
    changes = [(c["session_id"], c["open"]) for c in answer["changes"]]
    assert changes == [("s-1", False), ("s-2", True)]
    assert list(cache.stays) == ["s-2"]
    assert cache.stays_cursor == "3"
    # and a re-open of a known session updates it rather than duplicating
    platform.stay_opened("s-2", plate="CAR-2X")
    sync_stays(platform, cache)
    assert cache.stays["s-2"]["plate"] == "CAR-2X" and len(cache.stays) == 1


def test_a_page_that_says_more_is_followed_at_once_and_every_row_lands_once():
    platform = FakePlatform()
    platform.stay_page = 3
    cache = DecisionCache()
    sync_rules(platform, cache)
    for n in range(7):
        platform.stay_opened(f"s-{n}", plate=f"CAR-{n}")
    answer = sync_stays(platform, cache)
    assert answer["more"] is False
    assert sorted(cache.stays) == sorted(f"s-{n}" for n in range(7))
    assert platform.stays_reads == ["0", "3", "6"], "three pages, each continuing from the last row"
    assert cache.stays_cursor == "7"


def test_a_cache_with_no_cursor_takes_the_full_set_and_a_refusal_drops_the_cursor():
    platform = FakePlatform()
    platform.stay_opened("s-1", plate="CAR-1")
    cache = DecisionCache()
    assert cache.stays_cursor is None
    assert sync_stays(platform, cache)["open"][0]["session_id"] == "s-1"
    assert platform.stays_reads == [None] and cache.stays_cursor == "1"
    # a cursor the platform refuses: logged, dropped, the next read is full
    cache.stays_cursor = "not-a-cursor"
    assert sync_stays(platform, cache) is None
    assert cache.stays_cursor is None
    assert sync_stays(platform, cache) is not None and cache.stays_cursor == "1"


def test_an_offline_stays_read_changes_nothing():
    platform = FakePlatform()
    platform.stay_opened("s-1", plate="CAR-1")
    cache = DecisionCache()
    sync_rules(platform, cache)
    platform.stay_closed("s-1")
    platform.online = False
    assert sync_stays(platform, cache) is None
    assert list(cache.stays) == ["s-1"] and cache.stays_cursor == "1", "an outage moved the cache"
    platform.online = True
    sync_stays(platform, cache)
    assert cache.stays == {}


# ---------------------------------------------------------------------------
# the client's sixth method: the stays route, off the barrier's path
# ---------------------------------------------------------------------------


def test_get_stays_asks_the_stays_route_with_and_without_a_cursor():
    seen: list[tuple[str, str]] = []

    class Response:
        def __init__(self, body: str):
            self._body = body.encode()

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def opener(request, timeout):
        seen.append((request.get_method(), request.full_url))
        return Response('{"cursor": "9", "open": []}')

    client = PlatformClient("http://platform.test/", "tok", opener=opener)
    client.get_stays()
    client.get_stays(since="9")
    client.get_stays(since="a b")
    assert seen == [
        ("GET", "http://platform.test/api/v1/lane/stays"),
        ("GET", "http://platform.test/api/v1/lane/stays?since=9"),
        ("GET", "http://platform.test/api/v1/lane/stays?since=a%20b"),
    ]
    # the whole client surface, so a new method is a deliberate addition. The
    # seventh, `claim_validation`, is the reader's: the validation claimed when
    # the phone is entered (platform 0019, amendment A1).
    assert sorted(m for m in vars(PlatformClient) if not m.startswith("_")) == [
        "claim_validation", "close_session", "find_open_session", "get_rules", "get_stays",
        "open_session", "post_events",
    ]


# ---------------------------------------------------------------------------
# the runner: two threads, two cadences, nothing dies silently
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def a_runner(platform, *, rules_s=300.0, stays_s=5.0):
    from conftest import build_lane

    cache = DecisionCache()
    config = a_config(rules_refresh_seconds=rules_s, stays_refresh_seconds=stays_s)
    controller, _vend = build_lane(config, cache, [], arrivals=0)
    clock = FakeClock()
    runner = LaneRunner(
        controller, client=platform, rules_refresh_s=rules_s, stays_refresh_s=stays_s,
        clock=clock, sleep=lambda s: None,
    )
    return runner, clock, cache


def test_the_rules_are_read_first_then_the_stays_on_the_fast_cadence_then_the_rules_again():
    platform = FakePlatform()
    platform.stay_opened("s-1", plate="CAR-1")
    runner, clock, cache = a_runner(platform, rules_s=300.0, stays_s=5.0)
    runner.prime_schedule()
    # the first turn: the rules, NOW, carrying the full stays set
    assert runner.refresh_tick(clock.now) == "rules"
    assert platform.rules_reads == 1 and platform.stays_reads == []
    assert cache.stays_cursor == "1"
    # inside the fast interval: nothing
    assert runner.refresh_tick(clock.now + 4.9) is None
    # five seconds later: a stays delta, not a rules read
    platform.stay_closed("s-1")
    assert runner.refresh_tick(clock.now + 5.0) == "stays"
    assert platform.rules_reads == 1 and platform.stays_reads == ["1"]
    assert cache.stays == {}
    # ...and again every five seconds, on the fast cadence alone
    assert runner.refresh_tick(clock.now + 10.0) == "stays"
    assert runner.refresh_tick(clock.now + 12.0) is None
    # three hundred seconds after the first read: the rules again, and the
    # fast cadence re-based on them
    assert runner.refresh_tick(clock.now + 300.0) == "rules"
    assert platform.rules_reads == 2
    assert runner.refresh_tick(clock.now + 304.0) is None
    assert runner.refresh_tick(clock.now + 305.0) == "stays"
    assert runner.state.rules.succeeded == 2 and runner.state.stays.succeeded == 3
    assert runner.state.rules.last_error is None
    # the thread itself: one pass of the loop reads the rules and sleeps until the fast tick
    waited = []
    runner._sleep = lambda seconds: (waited.append(seconds), runner._stop.set())
    runner._refresh_loop()
    assert platform.rules_reads == 3
    assert waited == [5.0], "the loop did not sleep until the next fast tick"


def test_a_refresh_that_could_not_run_is_counted_and_said_and_the_cache_is_as_it_was():
    platform = FakePlatform()
    platform.stay_opened("s-1", plate="CAR-1")
    runner, _clock, cache = a_runner(platform)
    assert runner.refresh_rules() is True
    platform.online = False
    assert runner.refresh_rules() is False
    assert runner.refresh_stays() is False
    assert runner.state.rules.failed == 1 and runner.state.stays.failed == 1
    assert "unreachable" in runner.state.rules.last_error
    assert cache.stays_cursor == "1" and list(cache.stays) == ["s-1"]
    assert runner.state.rules.last_success_at is not None
    # a read that RAISES something unexpected is the same: counted, said, survived
    platform.online = True
    platform.get_rules = lambda: (_ for _ in ()).throw(RuntimeError("a bug in the fake"))
    assert runner.refresh_rules() is False
    assert runner.state.rules.failed == 2
    assert runner.state.rules.last_error == "RuntimeError: a bug in the fake"


def test_a_standalone_lane_runs_the_lane_thread_and_no_refresh_thread():
    from conftest import build_lane

    controller, _ = build_lane(a_config(), DecisionCache(), [], arrivals=0)
    runner = LaneRunner(controller, client=None, rules_refresh_s=300.0, stays_refresh_s=5.0)
    runner.start()
    try:
        assert [t.name for t in runner._threads] == ["lane"]
        deadline = time.time() + 5
        while runner.state.lane_turns == 0 and time.time() < deadline:
            time.sleep(0.01)
        assert runner.state.lane_turns >= 1
    finally:
        runner.stop()
    assert not runner.running


def test_the_lane_thread_survives_a_turn_that_raises_and_says_so(caplog):
    from conftest import build_lane

    controller, _ = build_lane(a_config(), DecisionCache(), [], arrivals=0)
    runner = LaneRunner(controller, client=None, rules_refresh_s=300.0, stays_refresh_s=5.0)
    boom = {"left": 2}

    def failing_run_once(timeout=None):
        if boom["left"]:
            boom["left"] -= 1
            raise RuntimeError("the loop driver threw")
        time.sleep(0.01)

    controller.run_once = failing_run_once
    with caplog.at_level(logging.ERROR, logger="lane_controller.runner"):
        runner.start()
        try:
            deadline = time.time() + 5
            while runner.state.lane_turns < 4 and time.time() < deadline:
                time.sleep(0.01)
        finally:
            runner.stop()
    assert runner.state.lane_turn_errors == 2
    assert runner.state.lane_turns >= 4, "the loop did not go round again after the raise"
    assert sum("a turn of the lane loop raised" in r.message for r in caplog.records) == 2


def test_with_a_platform_both_threads_run_and_the_first_rules_read_happens_at_once():
    platform = FakePlatform()
    platform.stay_opened("s-1", plate="CAR-1")
    from conftest import build_lane

    cache = DecisionCache()
    controller, _ = build_lane(a_config(), cache, [], arrivals=0)
    runner = LaneRunner(controller, client=platform, rules_refresh_s=300.0, stays_refresh_s=0.05)
    runner.start()
    try:
        assert sorted(t.name for t in runner._threads) == ["lane", "refresh"]
        deadline = time.time() + 5
        while platform.rules_reads == 0 or len(platform.stays_reads) < 2:
            if time.time() >= deadline:
                break
            time.sleep(0.01)
        assert platform.rules_reads == 1, "the rules were not read at start"
        assert len(platform.stays_reads) >= 2, "the fast cadence did not tick"
        assert cache.stays_cursor == "1" and not cache.is_stale()
    finally:
        runner.stop()
    assert not runner.running


def test_a_cadence_pair_the_wrong_way_round_is_refused():
    from conftest import build_lane

    controller, _ = build_lane(a_config(), DecisionCache(), [], arrivals=0)
    with pytest.raises(ValueError, match="stays_refresh_s"):
        LaneRunner(controller, client=FakePlatform(), rules_refresh_s=5.0, stays_refresh_s=6.0)


# ---------------------------------------------------------------------------
# serve: the wiring, and what it says about itself
# ---------------------------------------------------------------------------


def test_wire_lane_without_a_platform_is_the_standalone_lane_and_says_so():
    wired = cli.wire_lane(a_config(), platform=None)
    assert wired.client is None
    assert wired.controller.events._transport is None
    assert wired.controller.session_lookup is None
    assert isinstance(wired.controller.identifier, StubVehicleIdentifier)
    assert wired.seams["platform"].startswith("NONE")
    assert wired.seams["identifier"].startswith("SIMULATED")
    assert all(wired.seams[s].startswith("SIMULATED") for s in ("loops", "camera", "barrier"))


def test_wire_lane_with_a_platform_drains_the_outbox_to_it_and_names_the_session_at_an_exit():
    client = PlatformClient("http://platform.test", "tok")
    config = a_config(direction="exit", vehicle_id_url="http://127.0.0.1:8088")
    exit_lane = cli.wire_lane(config, platform=client)
    assert isinstance(exit_lane.controller.events._transport, PlatformTransport)
    assert exit_lane.controller.events._transport._client is client
    assert exit_lane.controller.session_lookup is not None
    assert isinstance(exit_lane.controller.identifier, VehicleIdClient)
    assert exit_lane.controller.identifier.endpoint == "http://127.0.0.1:8088"
    assert exit_lane.seams["platform"].startswith("REAL http://platform.test")
    assert exit_lane.seams["identifier"].startswith("REAL Vehicle ID")
    assert exit_lane.seams["barrier"].startswith("SIMULATED"), "no relay driver ships"
    entry_lane = cli.wire_lane(a_config(direction="entry"), platform=client)
    assert entry_lane.controller.session_lookup is None, "only an exit names its session"


def test_the_platform_is_configured_whole_or_not_at_all(tmp_path, capsys):
    token = tmp_path / "device.token"
    token.write_text("device-token\n")
    # server_url and no token: refused, naming both
    with pytest.raises(SystemExit) as refused:
        cli.platform_client_for(a_config(server_url="http://platform.test"), None)
    assert refused.value.code == 2 and "--platform-token-file" in capsys.readouterr().err
    # a token and no server_url: refused
    with pytest.raises(SystemExit) as refused:
        cli.platform_client_for(a_config(), token)
    assert refused.value.code == 2 and "server_url" in capsys.readouterr().err
    # neither: standalone
    assert cli.platform_client_for(a_config(), None) is None
    # both: a client at that address with that token
    client = cli.platform_client_for(a_config(server_url="http://platform.test/"), token)
    assert isinstance(client, PlatformClient)
    assert client.base_url == "http://platform.test" and client._token == "device-token"


def test_serve_refuses_a_half_configured_platform_before_binding(tmp_path, capsys, monkeypatch):
    example = Path(__file__).resolve().parent.parent / "config" / "lane.example.toml"
    config = tmp_path / "lane.toml"
    config.write_text(example.read_text())  # carries server_url
    # No server may bind in this test: a `serve` that got past the refusal
    # would otherwise serve for ever, and the fail-control that removes the
    # refusal would hang instead of going red.
    bound = []
    monkeypatch.setattr(cli, "make_server", lambda *a, **k: bound.append(True) or NeverServes())
    with pytest.raises(SystemExit) as refused:
        cli.main(["serve", "--config", str(config), "--port", "0"])
    assert refused.value.code == 2
    assert "--platform-token-file" in capsys.readouterr().err
    assert bound == [], "serve bound a port before refusing the half-configured platform"


class NeverServes:
    def serve_forever(self):
        return None

    def server_close(self):
        return None


def test_serve_starts_the_loop_beside_the_contract_and_stops_it(tmp_path, monkeypatch):
    """`serve` with a platform: the runner starts before the server serves and
    stops after it; the seam table names the platform REAL. The HTTP server is
    replaced by one that returns at once, so nothing binds for long."""
    example = Path(__file__).resolve().parent.parent / "config" / "lane.example.toml"
    config = tmp_path / "lane.toml"
    config.write_text(example.read_text().replace(
        'server_url = "https://platform.example/api/lane-events"', 'server_url = "http://127.0.0.1:9"'))
    token = tmp_path / "device.token"
    token.write_text("device-token")
    started: dict = {}

    class FakeServer:
        def serve_forever(self):
            started["runner_running"] = started["runner"].running

        def server_close(self):
            started["closed"] = True

    real_start = LaneRunner.start

    def capturing_start(self):
        started["runner"] = self
        real_start(self)

    monkeypatch.setattr(LaneRunner, "start", capturing_start)
    monkeypatch.setattr(cli, "make_server", lambda *a, **k: FakeServer())
    code = cli.main(["serve", "--config", str(config), "--platform-token-file", str(token)])
    assert code == 0
    assert started["runner_running"] is True, "the loop was not running while the server served"
    assert started["runner"].running is False, "the loop was not stopped when the server closed"
    assert started["closed"] is True
    assert started["runner"]._threads == [] and started["runner"].client is not None


# ---------------------------------------------------------------------------
# the demo reads the payloads 0013 and 0016 serve, not the hourly figure
# ---------------------------------------------------------------------------


def test_the_demo_reads_no_hourly_figure_from_either_payload():
    demo = Path(__file__).resolve().parent.parent / "src" / "lane_controller" / "demo.py"
    source = demo.read_text()
    for gone in ("hourly_minor'", "hourly_minor_applied"):
        assert gone not in source, f"the demo still reads {gone}"
    assert "rate_plans" in source and "plan_version" in source and "fee_minor" in source
