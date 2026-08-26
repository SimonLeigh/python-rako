"""Shared fixtures for the live-bridge integration suite.

Every test under ``tests_integration/`` requires a real Rako bridge on the
network and is opt-in twice over:

* ``pyproject.toml`` sets ``testpaths = ["tests"]``, so a plain ``pytest`` run
  never even collects this directory.
* Everything here is also gated at runtime by ``RAKO_LIVE=1`` -- run
  ``pytest tests_integration`` without it and every test collects and skips
  cleanly, which is what CI (and any accidental invocation) sees.

Read-only tests need only ``RAKO_LIVE=1``. Tests that change light state
additionally require ``RAKO_LIVE_MUTATE=1`` plus a channel the maintainer has
chosen to be safely toggled, via ``RAKO_TEST_ROOM`` / ``RAKO_TEST_CHANNEL``.
See ``tests/README_TESTING.md`` for the full matrix and exact commands.

The bridge address is never hard-coded (this is a public repository): it
comes from ``RAKO_BRIDGE_HOST`` (+ optional ``RAKO_BRIDGE_PORT`` /
``RAKO_BRIDGE_MAC`` / ``RAKO_BRIDGE_NAME``), falling back to
``discover_bridge()`` when ``RAKO_BRIDGE_HOST`` is unset.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import aiohttp
import pytest

from python_rako import (
    RAKO_BRIDGE_DEFAULT_PORT,
    Bridge,
    BridgeDescription,
    StatusListener,
    discover_bridge,
)
from python_rako.bridge import BridgeCommanderHTTP

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

RAKO_LIVE = os.environ.get("RAKO_LIVE") == "1"
RAKO_LIVE_MUTATE = os.environ.get("RAKO_LIVE_MUTATE") == "1"

_SKIP_REASON = "set RAKO_LIVE=1 to run against a real bridge (see tests/README_TESTING.md)"
_SKIP_MUTATE_REASON = (
    "set RAKO_LIVE_MUTATE=1 to run tests that change light state (see tests/README_TESTING.md)"
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Tag every test here ``live``, and skip it unless ``RAKO_LIVE=1``.

    The marker is applied automatically rather than requiring
    ``@pytest.mark.live`` on every function, so nothing here can accidentally
    escape the gate; it is still registered in ``pyproject.toml`` so
    ``--strict-markers`` (part of the shared ``addopts``) doesn't reject it.
    """
    for item in items:
        item.add_marker(pytest.mark.live)
        if not RAKO_LIVE:
            item.add_marker(pytest.mark.skip(reason=_SKIP_REASON))


@pytest.fixture
def live_mutate() -> None:
    """Require ``RAKO_LIVE_MUTATE=1``. Depend on this in any mutating test."""
    if not RAKO_LIVE_MUTATE:
        pytest.skip(_SKIP_MUTATE_REASON)


@pytest.fixture
def test_channel(live_mutate: None) -> tuple[int, int]:
    """The (room, channel) the maintainer has nominated for mutating tests.

    Deliberately not defaulted to a real value: the maintainer must choose a
    channel they are happy to see switched off/on, dimmed and briefly faded by
    these tests.
    """
    room = os.environ.get("RAKO_TEST_ROOM")
    channel = os.environ.get("RAKO_TEST_CHANNEL")
    if room is None or channel is None:
        pytest.skip(
            "set RAKO_TEST_ROOM and RAKO_TEST_CHANNEL to a channel you are happy "
            "to have switched on/off/dimmed/faded by these tests"
        )
    return int(room), int(channel)


async def _resolve_bridge_description() -> BridgeDescription:
    host = os.environ.get("RAKO_BRIDGE_HOST")
    if host:
        return BridgeDescription(
            host=host,
            port=int(os.environ.get("RAKO_BRIDGE_PORT", RAKO_BRIDGE_DEFAULT_PORT)),
            name=os.environ.get("RAKO_BRIDGE_NAME", "rako-bridge"),
            mac=os.environ.get("RAKO_BRIDGE_MAC", ""),
        )
    return await discover_bridge()


@pytest.fixture
async def bridge_description() -> BridgeDescription:
    """The bridge to test against, from the environment or discovery."""
    return await _resolve_bridge_description()


@pytest.fixture
async def http_session() -> AsyncGenerator[aiohttp.ClientSession, None]:
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
async def listener(bridge_description: BridgeDescription) -> AsyncGenerator[StatusListener, None]:
    """A started, supervised status listener; stopped cleanly on teardown."""
    async with StatusListener(bridge_description["host"]) as started:
        yield started


@pytest.fixture
async def udp_bridge(bridge_description: BridgeDescription) -> AsyncGenerator[Bridge, None]:
    """A bridge with no listener attached, so its commands are unverified."""
    async with Bridge(**bridge_description) as bridge:
        yield bridge


@pytest.fixture
async def http_bridge(
    bridge_description: BridgeDescription, http_session: aiohttp.ClientSession
) -> AsyncGenerator[Bridge, None]:
    """A bridge using the HTTP command transport."""
    commander = BridgeCommanderHTTP(
        bridge_description["host"], bridge_description["port"], http_session
    )
    async with Bridge(**bridge_description, bridge_commander=commander) as bridge:
        yield bridge


@pytest.fixture
async def verified_bridge(
    bridge_description: BridgeDescription, listener: StatusListener
) -> AsyncGenerator[Bridge, None]:
    """A bridge with a live listener attached, so commands are echo-verified.

    This is the configuration the library is designed around: ``set_*`` waits
    for the bridge's own broadcast and raises if it never arrives.
    """
    async with Bridge(**bridge_description, listener=listener) as bridge:
        yield bridge
