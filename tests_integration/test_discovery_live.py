"""Bridge discovery against a live bridge.

Read-only: broadcasts a discovery probe and, if the environment names a
specific bridge, cross-checks the reply against it.
"""

from __future__ import annotations

import os

import pytest

from python_rako import discover_bridge

pytestmark = pytest.mark.live


async def test_discover_bridge_finds_one() -> None:
    description = await discover_bridge()
    assert description["host"]
    assert description["port"]
    assert description["mac"]
    assert description["name"]


async def test_discovery_matches_env_host() -> None:
    """If RAKO_BRIDGE_HOST is set, sanity-check discovery finds that bridge."""
    expected_host = os.environ.get("RAKO_BRIDGE_HOST")
    if not expected_host:
        pytest.skip("RAKO_BRIDGE_HOST not set; nothing to cross-check discovery against")
    description = await discover_bridge()
    assert description["host"] == expected_host


async def test_discovery_matches_env_mac() -> None:
    expected_mac = os.environ.get("RAKO_BRIDGE_MAC")
    if not expected_mac:
        pytest.skip("RAKO_BRIDGE_MAC not set; nothing to cross-check discovery against")
    description = await discover_bridge()
    assert description["mac"] == expected_mac
