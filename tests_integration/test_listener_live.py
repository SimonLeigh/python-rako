"""``StatusListener`` lifecycle against a live bridge. Read-only.

Also verifies the Phase 3 environment constraint noted in
``MODERNISATION_PLAN.md``: the listener sets ``SO_REUSEADDR``/``SO_REUSEPORT``
so a second instance (e.g. a dev HA alongside a prod HA) can bind the same
port on the same host, and BRIDGE_BEHAVIOUR.md's overnight soak (fact 22)
confirmed two concurrent listeners both receive every broadcast.
"""

from __future__ import annotations

import pytest

from python_rako import BridgeDescription, StatusListener

pytestmark = pytest.mark.live


async def test_listener_starts_and_is_healthy(bridge_description: BridgeDescription) -> None:
    async with StatusListener(bridge_description["host"]) as started:
        assert started.is_running
        assert started.local_port is not None
        health = started.health
        assert health.is_running
        assert health.restart_count == 0
        assert health.last_error is None


async def test_listener_stops_cleanly(bridge_description: BridgeDescription) -> None:
    started = StatusListener(bridge_description["host"])
    await started.start()
    assert started.is_running

    await started.stop()
    assert not started.is_running
    assert started.health.is_running is False


async def test_two_listeners_coexist_on_one_host(bridge_description: BridgeDescription) -> None:
    host = bridge_description["host"]
    async with StatusListener(host) as first, StatusListener(host) as second:
        assert first.is_running
        assert second.is_running
        assert first.health.last_error is None
        assert second.health.last_error is None
