"""OP-5-8: the lane's HTTP service bounds how long and how many.

A socket that connects and sends nothing used to hold a thread for ever, and
nothing capped how many such threads there were: a box with one lane on it
runs out of threads before it runs out of anything else.
"""

from __future__ import annotations

import socket
import time
import urllib.request

from lane_controller import service as service_module
from lane_controller.service import LaneService, _Handler, make_server
from serving import serving
from test_lane_contract import full_lane


def _address(base: str) -> tuple[str, int]:
    host, port = base.removeprefix("http://").rsplit(":", 1)
    return host, int(port)


def test_the_handler_has_a_connection_timeout():
    assert _Handler.timeout == 10


def test_a_silent_connection_is_dropped_after_the_timeout(monkeypatch):
    monkeypatch.setattr(_Handler, "timeout", 0.3)
    with serving(make_server(LaneService(full_lane()), port=0)) as base:
        idle = socket.create_connection(_address(base), timeout=3)
        started = time.monotonic()
        try:
            assert idle.recv(1) == b"", "the server must close a silent socket"
        except TimeoutError as exc:
            raise AssertionError("a silent socket was held past the handler's timeout") from exc
        finally:
            idle.close()
        assert time.monotonic() - started < 2


def test_connections_past_the_cap_are_closed_and_the_rest_still_served(monkeypatch):
    monkeypatch.setattr(service_module, "MAX_CONNECTIONS", 3)
    with serving(make_server(LaneService(full_lane()), port=0)) as base:
        held = [socket.create_connection(_address(base), timeout=3) for _ in range(3)]
        time.sleep(0.2)
        extra = socket.create_connection(_address(base), timeout=3)
        try:
            assert extra.recv(1) == b"", "a connection past the cap must be closed"
        except TimeoutError as exc:
            raise AssertionError("a connection past the cap was held open") from exc
        finally:
            extra.close()
        for sock in held:
            sock.close()
        time.sleep(0.2)
        # THE CONTROL: once the held ones are gone, the lane answers again.
        with urllib.request.urlopen(f"{base}/v1/lane", timeout=3) as response:
            assert response.status == 200
