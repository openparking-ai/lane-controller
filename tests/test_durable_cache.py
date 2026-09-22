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


def test_a_car_with_a_rule_still_exits_after_the_restart(cache_dir):
    """End to end through the lane: a plate rule written by the platform,
    the platform gone, the process restarted, the barrier vends. The stub
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
        return build_lane(a_config(direction="exit"), cache, [identity], arrivals=1)

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
    assert cache.default_action is None and cache.is_stale()
    assert DurableStore(path).read() is None, "the file still holds the plates"
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
    assert DurableStore(path).read() is None, "a store past its bound survived the start"


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
    assert cache.stays == {} and DurableStore(path).read() is None
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
