"""The decision cache survives a restart with the platform unreachable, and
what it holds at rest has a stated bound.

`DecisionCache` said it "will become something durable on the Jetson (SQLite
on the controller is the intended shape)". Until now a lane that lost the
platform and then restarted came back holding nothing. Now, with a
`[lane] cache_path`, it comes back holding what it last held -- as old as its
last refresh, not as old as the restart -- and a car with a rule still exits.

Each claim is paired with the case that falsifies it, and
`scripts/durable_fail_control.py` breaks the store in five ways and requires
this file to go red. Nothing here decides an exit from the plans or the
registers: that is its own round. What survives the restart is what the
platform handed the lane, whole, and the existing plate-rule decision is the
proof that a decision can be made from it.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from pathlib import Path

import pytest

from fake_platform import FakePlatform
from lane_controller import CameraConfig, DecisionCache, GateConfig, LaneConfig, Rule, cli
from lane_controller.durable import (
    CACHE_DIR_MODE,
    CACHE_FILE_MODE,
    CacheDirectoryUnsafe,
    DurableStore,
    cache_directory_fault,
    ensure_cache_directory,
)
from lane_controller.interfaces import VehicleIdentity
from lane_controller.runner import LaneRunner
from lane_controller.sync import sync_rules, sync_stays

A_REGISTER = {"garage": "gp-1", "registrations": [{"vehicle_identity": "PASS-1", "pass": "p-1"}]}


def a_config(**overrides) -> LaneConfig:
    fields = dict(
        lane_id="lane-durable",
        site_id="site-durable",
        camera=CameraConfig(camera_id="sim-cam", rtsp_url="", frames_per_read=1),
        gate=GateConfig(),
    )
    fields.update(overrides)
    return LaneConfig(**fields)


@pytest.fixture
def cache_dir(tmp_path):
    """A directory this process owns, narrowed -- the shape the store requires."""
    directory = tmp_path / "lane-cache"
    directory.mkdir(mode=CACHE_DIR_MODE)
    directory.chmod(CACHE_DIR_MODE)
    return directory


def holds_no_personal_data(state) -> bool:
    """What the store may hold past the bound: nothing, or the garage's entry
    settings alone -- no rule, no register, no stay, no banned car, no plan."""
    if state is None:
        return True
    return (
        state.get("refreshed_at") is None
        and not state.get("rules")
        and not state.get("entitlements")
        and not state.get("stays")
        and not state.get("banned")
        and not state.get("plans")
    )


def filled_platform() -> FakePlatform:
    platform = FakePlatform()
    platform.entitlements["garage_pass"] = {"consulted": True, "register": A_REGISTER}
    platform.stay_opened("s-1", plate="CAR-1")
    return platform


# ---------------------------------------------------------------------------
# the restart
# ---------------------------------------------------------------------------


def test_the_whole_cache_survives_a_restart_with_the_platform_unreachable(cache_dir):
    path = cache_dir / "cache.sqlite"
    platform = filled_platform()
    first = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    assert first.is_stale(), "the premise: a fresh store holds nothing"
    sync_rules(platform, first)
    platform.stay_opened("s-2", plate="CAR-2")
    sync_stays(platform, first)
    assert list(first.stays) == ["s-1", "s-2"] and first.stays_cursor == "2"

    # THE RESTART: a new object, the platform gone, nothing but the file.
    platform.online = False
    second = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    assert sync_rules(platform, second) is None, "the premise: the platform is unreachable"
    assert not second.is_stale()
    assert second.default_action == "allow"
    assert second.plans == first.plans
    assert second.space_class == "standard"
    assert second.entitlements["garage_pass"]["register"] == A_REGISTER
    assert second.entitlements_complete is True
    assert second.stays == first.stays and second.stays_cursor == "2"
    assert second.state() == first.state(), "everything the first held, the second holds"
    # CONTROL: without a store, the same restart holds nothing.
    bare = DecisionCache(max_age_seconds=3600)
    assert bare.is_stale() and bare.stays == {} and bare.plans == []
    # and the second's next delta continues from the restored cursor
    platform.online = True
    platform.stay_closed("s-1")
    sync_stays(platform, second)
    assert platform.stays_reads[-1] == "2" and list(second.stays) == ["s-2"]


def test_a_car_with_a_rule_still_gets_in_after_the_restart(cache_dir):
    """End to end through the lane: a plate rule written by the platform,
    the platform gone, the process restarted, the barrier vends. An entry
    lane: an exit opens only for a settled car, and a car a plate rule allows
    is not settled by the rule alone (`exit_case`). The stub
    identifier stands in for Vehicle ID here -- the socket-counter control
    with the real one on loopback belongs to the exit-decision round."""
    from conftest import build_lane

    path = cache_dir / "cache.sqlite"
    platform = FakePlatform()
    platform.get_rules = lambda: {**FakePlatform.get_rules(platform), "plate_rules": [
        {"plate": "MEMBER-1", "allow": True, "rate_plan": "monthly"}]}
    warm = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    assert sync_rules(platform, warm) is not None
    platform.online = False

    def restarted_lane():
        cache = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
        identity = VehicleIdentity(plate="MEMBER-1", plate_region="FL", make=None, model=None,
                                   color=None, confidence=0.99)
        return build_lane(a_config(direction="entry"), cache, [identity], arrivals=1)

    controller, vend = restarted_lane()
    decision = controller.run_once()
    assert decision is not None and decision.should_vend, decision
    assert decision.reason and vend.vends, "the barrier did not vend after the restart"
    # CONTROL: the same restart with the file gone falls back, no vend.
    path.unlink()
    controller, vend = restarted_lane()
    decision = controller.run_once()
    assert decision is not None and not decision.should_vend
    assert decision.fallback is not None and vend.vends == []


def test_a_restored_cache_is_as_old_as_its_refresh_not_as_old_as_the_restart(cache_dir):
    path = cache_dir / "cache.sqlite"
    platform = filled_platform()
    long_ago = time.time() - 7200
    first = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    first.load_payload(platform.get_rules(), now=long_ago)
    assert first.is_stale(), "the premise: two hours old, one hour bound"
    # a shorter gap: restored, and still exactly as old
    recent = time.time() - 600
    first.load_payload(platform.get_rules(), now=recent)
    second = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    assert second._refreshed_at == recent
    assert not second.is_stale()
    assert second.is_stale(now=recent + 3601), "the age counts from the refresh, not the load"


def test_every_change_is_written_whole_and_the_file_is_never_half_of_two_states(cache_dir):
    path = cache_dir / "cache.sqlite"
    platform = filled_platform()
    cache = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    sync_rules(platform, cache)
    store = DurableStore(path)
    assert store.read() == cache.state()
    platform.stay_opened("s-9", plate="CAR-9")
    sync_stays(platform, cache)
    assert store.read() == cache.state(), "the delta was not written"
    assert store.read()["stays_cursor"] == "2"
    # one row, one key: the shape that cannot be half-written
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT count(*) FROM cache").fetchone() == (1,)


def test_a_store_of_another_shape_or_no_store_at_all_restores_nothing_and_raises_nothing(cache_dir):
    path = cache_dir / "cache.sqlite"
    assert DurableStore(path).read() is None
    with sqlite3.connect(path) as c:
        c.execute(
            "INSERT OR REPLACE INTO cache (key, format, state) VALUES ('decision_cache', 99, '{}')"
        )
    assert DurableStore(path).read() is None, "a different format is not this one"
    cache = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    assert cache.is_stale() and cache.stays == {}


# ---------------------------------------------------------------------------
# the bound: held exactly as long as it is trusted
# ---------------------------------------------------------------------------


def test_a_cache_past_its_bound_is_wiped_from_memory_and_disk_and_not_restored(cache_dir):
    path = cache_dir / "cache.sqlite"
    platform = filled_platform()
    cache = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    sync_rules(platform, cache)
    refreshed = cache._refreshed_at
    assert cache.stays and cache.entitlements and DurableStore(path).read() is not None
    # inside the bound: nothing happens
    assert cache.expire_if_past_bound(now=refreshed + 3600) is False
    assert cache.stays and DurableStore(path).read() is not None
    # past it: memory and disk, both
    assert cache.expire_if_past_bound(now=refreshed + 3601) is True
    assert cache.stays == {} and cache.entitlements == {} and cache.plans == []
    assert cache.is_stale()
    assert holds_no_personal_data(DurableStore(path).read()), "the file still holds the plates"
    # what outlives it is the garage's entry settings, which name no car
    assert cache.default_action == "allow"
    assert DurableStore(path).read()["default_action"] == "allow"
    # and a second call is a no-op, not a second wipe
    assert cache.expire_if_past_bound(now=refreshed + 9999) is False


def test_a_store_older_than_the_bound_is_wiped_on_start_never_read_into_memory(cache_dir):
    path = cache_dir / "cache.sqlite"
    platform = filled_platform()
    stale = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    stale.load_payload(platform.get_rules(), now=time.time() - 3601)
    assert DurableStore(path).read()["stays"], "the premise: plates are on disk"
    restarted = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    assert restarted.stays == {} and restarted.entitlements == {} and restarted.is_stale()
    assert holds_no_personal_data(DurableStore(path).read()), (
        "a store past its bound survived the start"
    )
    assert restarted.default_action == "allow", "the garage's settings did not"


def test_the_bound_is_the_staleness_age_one_number(cache_dir):
    """The age at which the lane stops trusting the cache is the age at which
    it stops holding it: `expire_if_past_bound` fires exactly where
    `is_stale` starts, and never before it."""
    cache = DecisionCache(max_age_seconds=100, store=DurableStore(cache_dir / "c.sqlite"))
    cache.load([Rule(plate="X", allow=True)], now=1000.0)
    for now in (1000.0, 1050.0, 1100.0):
        assert not cache.is_stale(now=now) and not cache.expire_if_past_bound(now=now)
    assert cache.is_stale(now=1100.5)
    assert cache.expire_if_past_bound(now=1100.5)


def test_the_runner_enforces_the_bound_on_every_tick(cache_dir):
    from conftest import build_lane

    path = cache_dir / "cache.sqlite"
    platform = filled_platform()
    cache = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    controller, _ = build_lane(a_config(), cache, [], arrivals=0)
    wall = {"now": 1_000_000.0}
    runner = LaneRunner(controller, client=platform, rules_refresh_s=300.0, stays_refresh_s=5.0,
                        clock=lambda: 0.0, sleep=lambda s: None, wall_clock=lambda: wall["now"])
    runner.prime_schedule()
    assert runner.refresh_tick(0.0) == "rules"
    cache._refreshed_at = wall["now"]  # the fake's payload was loaded at real time; pin it
    cache._persist()
    # the platform goes away; the lane keeps what it holds, up to the bound
    platform.online = False
    wall["now"] += 3600
    assert runner.refresh_tick(5.0) == "stays"
    assert cache.stays and runner.state.cache_expiries == 0
    wall["now"] += 1
    assert runner.refresh_tick(10.0) == "stays"
    assert runner.state.cache_expiries == 1
    assert cache.stays == {} and holds_no_personal_data(DurableStore(path).read())
    # the platform returns: the next rules read refills it
    platform.online = True
    assert runner.refresh_tick(300.0) == "rules"
    assert cache.stays and DurableStore(path).read() is not None


def test_a_refresh_replaces_the_file_whole_so_a_plate_that_left_is_gone_from_the_box(cache_dir):
    path = cache_dir / "cache.sqlite"
    platform = filled_platform()
    cache = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    sync_rules(platform, cache)
    assert "PASS-1" in path.read_bytes().decode("utf-8", "replace")
    emptied = {"garage": "gp-1", "registrations": []}
    platform.entitlements["garage_pass"] = {"consulted": True, "register": emptied}
    platform.stay_closed("s-1")
    sync_rules(platform, cache)
    text = DurableStore(path).read()
    assert "PASS-1" not in str(text) and text["stays"] == {}
    # the bytes on disk may still hold the old page until SQLite reuses it:
    # VACUUM is not this round's claim; the READ is what the next process gets
    assert DurableStore(path).read()["entitlements"]["garage_pass"]["register"] == emptied


# ---------------------------------------------------------------------------
# the file and the directory: the process's own
# ---------------------------------------------------------------------------


def test_the_file_is_0600_and_narrowed_on_every_write(cache_dir):
    path = cache_dir / "cache.sqlite"
    store = DurableStore(path)
    assert stat.S_IMODE(path.stat().st_mode) == CACHE_FILE_MODE
    path.chmod(0o644)
    store.write({"refreshed_at": time.time()})
    assert stat.S_IMODE(path.stat().st_mode) == CACHE_FILE_MODE, "a write did not narrow the file"
    path.chmod(0o644)
    store.read()
    assert stat.S_IMODE(path.stat().st_mode) == CACHE_FILE_MODE, "a read did not narrow the file"


@pytest.mark.parametrize(
    ("mode", "owner", "process", "leaf", "faulty"),
    [
        (0o700, 1000, 1000, True, False),
        (0o750, 1000, 1000, True, True),   # group can read the plates
        (0o700, 1001, 1000, True, True),   # somebody else's
        (0o700, 0, 1000, True, True),      # root's, at the LEAF: not this process's
        (0o755, 0, 1000, False, False),    # root's ancestor, readable: fine
        (0o777, 0, 1000, False, True),     # a world-writable ancestor
        (0o1777, 0, 1000, False, False),   # /tmp: sticky, not writable in the sense that matters
        (0o755, 1001, 1000, False, True),  # an ancestor somebody else owns
    ],
)
def test_the_directory_decision_owner_first_then_mode_sticky_ancestors_allowed(
    mode, owner, process, leaf, faulty
):
    fault = cache_directory_fault(mode, owner, process, leaf=leaf)
    assert (fault is not None) == faulty, fault


def test_a_directory_that_is_not_the_processes_own_refuses_before_the_port_opens(tmp_path):
    wide = tmp_path / "wide"
    wide.mkdir()
    wide.chmod(0o755)
    with pytest.raises(CacheDirectoryUnsafe, match="wider than 0700"):
        DurableStore(wide / "cache.sqlite")
    with pytest.raises(CacheDirectoryUnsafe, match="relative"):
        DurableStore(Path("relative/cache.sqlite"))
    # a path that does not exist yet is created narrow, every component
    fresh = tmp_path / "a" / "b" / "cache.sqlite"
    DurableStore(fresh)
    for component in (fresh.parent, fresh.parent.parent):
        assert stat.S_IMODE(component.stat().st_mode) == CACHE_DIR_MODE
    assert ensure_cache_directory(fresh.parent) == fresh.parent.resolve()


def test_serve_refuses_an_unsafe_cache_path_and_wires_a_safe_one(tmp_path, monkeypatch, capsys):
    example = Path(__file__).resolve().parent.parent / "config" / "lane.example.toml"
    base = example.read_text().replace(
        'server_url = "https://platform.example/api/lane-events"', '# server_url unset'
    )
    wide = tmp_path / "wide"
    wide.mkdir()
    wide.chmod(0o755)
    unsafe = tmp_path / "unsafe.toml"
    unsafe.write_text(base.replace(
        '# cache_path = "/var/lib/lane-controller/decision-cache.sqlite"',
        f'cache_path = "{wide}/cache.sqlite"'))
    bound = []
    monkeypatch.setattr(cli, "make_server", lambda *a, **k: bound.append(True))
    assert cli.main(["serve", "--config", str(unsafe)]) == 2
    assert "wider than 0700" in capsys.readouterr().err
    assert bound == [], "serve bound a port before refusing the cache path"
    # a safe one: the cache is DURABLE and the seam table says so
    safe_dir = tmp_path / "own"
    raw = _toml_dict(base)
    raw["lane"]["cache_path"] = str(safe_dir / "c.sqlite")
    config = LaneConfig.from_dict(raw)
    wired = cli.wire_lane(config, platform=None)
    assert wired.seams["cache"].startswith(f"DURABLE {safe_dir}/c.sqlite (empty")
    assert wired.controller.cache._store is not None
    assert stat.S_IMODE(safe_dir.stat().st_mode) == CACHE_DIR_MODE
    # and a memory-only lane says that too
    assert cli.wire_lane(a_config(), platform=None).seams["cache"].startswith("MEMORY ONLY")


def _toml_dict(text: str) -> dict:
    import tomllib

    return tomllib.loads(text)


def test_the_example_config_names_the_bound_and_the_forgery_path():
    example = (Path(__file__).resolve().parent.parent / "config" / "lane.example.toml").read_text()
    assert "# cache_path =" in example
    assert "rules_max_age_seconds above" in example and "opens the barrier" in example
    assert os.path.isabs("/var/lib/lane-controller/decision-cache.sqlite")


# ---------------------------------------------------------------------------
# OP-5-13: a row deleted or replaced is gone from the FILE, not only the table.
#
# SQLite's DELETE marks pages free and leaves their bytes where they were, so
# a plate that left the register, a wiped cache or a delivered stay could be
# read straight out of the file with `strings`. The bytes of the file are the
# test, not what a SELECT returns.
# ---------------------------------------------------------------------------

SECRET = "ZZ-SECRET-PLATE-0513"


def _a_state_with(plate: str) -> dict:
    return {"rules": {plate: "allow"}, "padding": "x" * 20_000}


def test_a_replaced_or_wiped_cache_leaves_no_plate_in_the_file(cache_dir):
    path = cache_dir / "c.sqlite"
    store = DurableStore(path)
    store.write(_a_state_with(SECRET))
    assert SECRET.encode() in path.read_bytes(), "the premise: the plate is on disk"

    store.write(_a_state_with("OTHER"))
    assert SECRET.encode() not in path.read_bytes(), "a replaced row left the plate behind"

    store.write(_a_state_with(SECRET))
    store.wipe()
    assert SECRET.encode() not in path.read_bytes(), "a wiped row left the plate behind"


def test_a_removed_outbox_action_leaves_no_plate_in_the_file(cache_dir):
    from lane_controller.durable import OutboxStore
    from lane_controller.events import LaneEvent

    path = cache_dir / "o.sqlite"
    store = OutboxStore(path)
    event = LaneEvent(kind="session_open", lane_id="l", at=1.0,
                      detail={"plate": SECRET, "padding": "x" * 20_000})
    store.add(event)
    assert SECRET.encode() in path.read_bytes(), "the premise: the plate is on disk"
    store.remove([event.event_id])
    assert SECRET.encode() not in path.read_bytes()


def test_a_cache_file_from_an_earlier_build_is_cleaned_at_start(cache_dir):
    """A file whose deleted row sits in a free page from before secure_delete."""
    path = cache_dir / "c.sqlite"
    DurableStore(path)
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(
            "INSERT INTO cache (key, format, state) VALUES ('old', 1, ?)", (SECRET * 2000,)
        )
    with connection:
        connection.execute("DELETE FROM cache WHERE key = 'old'")
    connection.close()
    assert SECRET.encode() in path.read_bytes(), "the premise: the residue is in the file"
    DurableStore(path)
    assert SECRET.encode() not in path.read_bytes()


# ---------------------------------------------------------------------------
# OP-5-6: the files are bound to the lane, garage and platform that wrote them.
# ---------------------------------------------------------------------------


def test_another_lanes_cache_is_wiped_and_this_lanes_is_kept(cache_dir):
    from lane_controller.durable import store_identity

    path = cache_dir / "c.sqlite"
    mine = store_identity("lane-a", "site-1", "https://p.example")
    DurableStore(path, identity=mine).write({"rules": {"A-1": "allow"}})
    # THE CONTROL: the same lane restarting keeps what it held.
    assert DurableStore(path, identity=mine).read() == {"rules": {"A-1": "allow"}}
    for other in (
        store_identity("lane-b", "site-1", "https://p.example"),
        store_identity("lane-a", "site-2", "https://p.example"),
        store_identity("lane-a", "site-1", "https://other.example"),
    ):
        DurableStore(path, identity=mine).write({"rules": {"A-1": "allow"}})
        assert DurableStore(path, identity=other).read() is None


def test_another_lanes_outbox_with_stays_in_it_is_refused_never_sent_or_dropped(cache_dir):
    from lane_controller.durable import OutboxStore, StoreIdentityMismatch, store_identity
    from lane_controller.events import LaneEvent

    path = cache_dir / "o.sqlite"
    mine = store_identity("lane-a", "site-1", None)
    other = store_identity("lane-b", "site-1", None)
    OutboxStore(path, identity=mine).add(
        LaneEvent(kind="session_open", lane_id="lane-a", at=1.0, detail={"plate": "A-1"})
    )
    with pytest.raises(StoreIdentityMismatch):
        OutboxStore(path, identity=other)
    held = OutboxStore(path, identity=mine).held()
    assert [e.detail["plate"] for e in held] == ["A-1"], "the stay is still there"


def test_an_empty_outbox_of_another_lane_is_taken_and_an_unstamped_one_is_adopted(cache_dir):
    from lane_controller.durable import OutboxStore, StoreIdentityMismatch, store_identity

    path = cache_dir / "o.sqlite"
    OutboxStore(path)  # an earlier build: no stamp
    OutboxStore(path, identity=store_identity("lane-a", "s", None))
    OutboxStore(path, identity=store_identity("lane-b", "s", None))  # empty: re-stamped
    with pytest.raises(StoreIdentityMismatch):
        store = OutboxStore(path, identity=store_identity("lane-b", "s", None))
        from lane_controller.events import LaneEvent

        store.add(LaneEvent(kind="session_open", lane_id="lane-b", at=1.0, detail={}))
        OutboxStore(path, identity=store_identity("lane-a", "s", None))


def test_wire_lane_stamps_the_cache_with_the_configured_lane(cache_dir):
    path = cache_dir / "c.sqlite"
    first = cli.wire_lane(a_config(cache_path=str(path)), platform=None)
    first.controller.cache.load([Rule(plate="A-1", allow=True)], default_action="allow")
    assert DurableStore(path).read() is not None
    cli.wire_lane(a_config(cache_path=str(path), lane_id="lane-other"), platform=None)
    assert DurableStore(path).read() is None or not DurableStore(path).read().get("rules")


def test_an_outbox_from_an_earlier_build_is_cleaned_once_on_its_first_stamp(cache_dir):
    """OP-5-13, the outbox: a stay an earlier build removed (DELETE without
    secure_delete) left its plate in a free page. The first start that stamps
    the file VACUUMs it; later starts do not rewrite it again."""
    from lane_controller.durable import OutboxStore, store_identity

    path = cache_dir / "o.sqlite"
    OutboxStore(path)  # the tables, as an earlier build made them
    connection = sqlite3.connect(path)  # an earlier build: no secure_delete
    with connection:
        connection.execute(
            "INSERT INTO outbox (event_id, at, kind, lane_id, detail) VALUES (?, 1, ?, ?, ?)",
            ("e-1", "session_open", "lane-a", SECRET * 2000),
        )
    with connection:
        connection.execute("DELETE FROM outbox WHERE event_id = 'e-1'")
    connection.close()
    assert SECRET.encode() in path.read_bytes(), "the premise: the residue is in the file"

    mine = store_identity("lane-a", "s", None)
    OutboxStore(path, identity=mine)
    assert SECRET.encode() not in path.read_bytes()

    vacuums = []
    import lane_controller.durable as durable_module

    real = durable_module._vacuum
    durable_module._vacuum = lambda p: (vacuums.append(p), real(p))[1]
    try:
        OutboxStore(path, identity=mine)
    finally:
        durable_module._vacuum = real
    assert vacuums == [], "a stamped outbox is not rewritten on every start"


def test_an_outbox_whose_vacuum_fails_keeps_every_row_and_tries_again(cache_dir, monkeypatch):
    from lane_controller import durable as durable_module
    from lane_controller.durable import OutboxStore, store_identity
    from lane_controller.events import LaneEvent

    path = cache_dir / "o.sqlite"
    OutboxStore(path).add(
        LaneEvent(kind="session_open", lane_id="lane-a", at=1.0, detail={"plate": "A-1"})
    )

    def full_disk(p):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(durable_module, "_vacuum", full_disk)
    store = OutboxStore(path, identity=store_identity("lane-a", "s", None))
    assert [e.detail["plate"] for e in store.held()] == ["A-1"]
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        assert c.execute("SELECT value FROM meta WHERE key='identity'").fetchone() is None, (
            "left unstamped, so the next start tries again"
        )


def test_one_platform_typed_two_ways_is_one_identity():
    from lane_controller.durable import store_identity

    one = store_identity("l", "s", "https://p.example")
    for same in ("https://p.example/", "HTTPS://P.Example", "https://p.example:443/"):
        assert store_identity("l", "s", same) == one, same
    # THE CONTROL: another host, port or path is another platform.
    for other in ("https://q.example", "https://p.example:8443", "https://p.example/v2"):
        assert store_identity("l", "s", other) != one, other


# ---------------------------------------------------------------------------
# A FULL DISK: a lane that `main` would start still starts.
#
# Re-review of #60: every start re-wrote the identity stamp and VACUUMed the
# cache with no try, so on a full disk `serve` stopped starting where `main`
# did. The disk here is SQLite answering SQLITE_FULL to every statement that
# would write -- what a real full filesystem does (checked by hand on a 1 MB
# tmpfs) -- while a read, and a CREATE ... IF NOT EXISTS of a table that is
# already there, go through, as they do on a full disk.
# ---------------------------------------------------------------------------

_WRITES = ("INSERT", "REPLACE", "UPDATE", "DELETE", "VACUUM", "CREATE")


class _FullDiskConnection:
    def __init__(self, real):
        self._real = real

    def _would_write(self, sql: str) -> bool:
        text = " ".join(sql.split()).upper()
        if not text.startswith(_WRITES):
            return False
        if text.startswith("CREATE TABLE IF NOT EXISTS "):
            name = text.split()[5].split("(")[0].lower()
            return self._real.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone() is None
        return True

    def execute(self, sql, *args):
        if self._would_write(sql):
            raise sqlite3.OperationalError("database or disk is full")
        return self._real.execute(sql, *args)

    def executemany(self, sql, *args):
        if self._would_write(sql):
            raise sqlite3.OperationalError("database or disk is full")
        return self._real.executemany(sql, *args)

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def close(self):
        self._real.close()


@pytest.fixture
def full_disk(monkeypatch):
    from lane_controller import durable as durable_module

    real_connect = sqlite3.connect

    def turn_on():
        monkeypatch.setattr(
            durable_module.sqlite3, "connect",
            lambda *a, **k: _FullDiskConnection(real_connect(*a, **k)),
        )

    return turn_on


def _a_stay():
    from lane_controller.events import LaneEvent

    return LaneEvent(kind="session_open", lane_id="lane-a", at=1.0, detail={"plate": "A-1"})


def test_stamped_stores_start_on_a_full_disk_and_keep_everything(cache_dir, full_disk):
    from lane_controller.durable import OutboxStore, store_identity

    mine = store_identity("lane-a", "s", None)
    DurableStore(cache_dir / "c.sqlite", identity=mine).write({"rules": {"A-1": "allow"}})
    OutboxStore(cache_dir / "o.sqlite", identity=mine).add(_a_stay())
    full_disk()
    cache = DurableStore(cache_dir / "c.sqlite", identity=mine)
    outbox = OutboxStore(cache_dir / "o.sqlite", identity=mine)
    assert cache.read() == {"rules": {"A-1": "allow"}}
    assert [e.detail["plate"] for e in outbox.held()] == ["A-1"]


def test_unstamped_stores_from_an_earlier_build_start_on_a_full_disk(cache_dir, full_disk):
    from lane_controller.durable import OutboxStore, store_identity

    DurableStore(cache_dir / "c.sqlite").write({"rules": {"A-1": "allow"}})
    OutboxStore(cache_dir / "o.sqlite").add(_a_stay())
    full_disk()
    mine = store_identity("lane-a", "s", None)
    assert DurableStore(cache_dir / "c.sqlite", identity=mine).read() == {
        "rules": {"A-1": "allow"}
    }
    held = OutboxStore(cache_dir / "o.sqlite", identity=mine).held()
    assert [e.detail["plate"] for e in held] == ["A-1"]


def test_another_lanes_files_on_a_full_disk_are_still_refused(cache_dir, full_disk):
    """The start goes on past a write the disk will not take -- never past an
    identity that does not match: another lane's cache that cannot be wiped,
    and another lane's outbox with stays in it, still stop `serve`."""
    from lane_controller.durable import OutboxStore, StoreIdentityMismatch, store_identity

    theirs = store_identity("lane-b", "s", None)
    DurableStore(cache_dir / "c.sqlite", identity=theirs).write({"rules": {"B-1": "allow"}})
    OutboxStore(cache_dir / "o.sqlite", identity=theirs).add(_a_stay())
    full_disk()
    mine = store_identity("lane-a", "s", None)
    with pytest.raises(StoreIdentityMismatch):
        DurableStore(cache_dir / "c.sqlite", identity=mine)
    with pytest.raises(StoreIdentityMismatch):
        OutboxStore(cache_dir / "o.sqlite", identity=mine)


def test_another_lanes_empty_outbox_on_a_full_disk_is_refused_and_not_written(
    cache_dir, full_disk, tmp_path, monkeypatch, capsys
):
    """Re-review of #60: an EMPTY outbox stamped for another lane that a full
    disk will not let this lane re-stamp was logged and used, unstamped for
    this lane -- so this lane's stays went into the other lane's file. It is
    refused, as the cache is, and the file is left exactly as it was."""
    from lane_controller import cli
    from lane_controller.durable import OutboxStore, StoreIdentityMismatch, store_identity

    path = cache_dir / "o.sqlite"
    OutboxStore(path, identity=store_identity("lane-b", "s", None))
    before = path.read_bytes()
    full_disk()
    with pytest.raises(StoreIdentityMismatch):
        OutboxStore(path, identity=store_identity("lane-a", "s", None))
    assert path.read_bytes() == before, "the other lane's file was written"

    # And `serve` exits 2 on it, before the port opens.
    def refuse(*args, **kwargs):
        raise StoreIdentityMismatch("another lane's outbox")

    monkeypatch.setattr(cli, "wire_lane", refuse)
    monkeypatch.setattr(cli, "platform_client_for", lambda config, token_file: None)
    example = Path(__file__).resolve().parent.parent / "config" / "lane.example.toml"
    assert cli.main(["serve", "--config", str(example)]) == 2
    assert "another lane's outbox" in capsys.readouterr().err
