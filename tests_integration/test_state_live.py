"""Scene cache HTTP/UDP agreement and state-snapshot construction. Read-only.

Verifies the facts recorded in ``hacs_rako/docs/BRIDGE_BEHAVIOUR.md``:
fact 13 (``scenes.htm`` decodes identically to the UDP cache query) and fact 2
/ D3 (rooms the bridge has dropped from its scene cache -- fade-controlled
rooms -- must come back ``unknown``, never ``off``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from python_rako import Bridge, RequestType

if TYPE_CHECKING:
    import aiohttp

pytestmark = pytest.mark.live


async def test_http_scene_cache_matches_udp(
    udp_bridge: Bridge, http_session: aiohttp.ClientSession
) -> None:
    http_cache = await udp_bridge.get_scene_cache_http(http_session)
    _level_cache, udp_cache = await udp_bridge.get_cache_state(RequestType.SCENE_CACHE)
    assert dict(http_cache) == dict(udp_cache)


async def test_state_snapshot_builds(
    udp_bridge: Bridge, http_session: aiohttp.ClientSession
) -> None:
    snapshot = await udp_bridge.get_state_snapshot(http_session)
    assert snapshot.rooms, "expected at least one room in the snapshot"
    assert snapshot.channels, "expected at least one channel in the snapshot"


async def test_rooms_absent_from_scene_cache_are_unknown_not_off(
    udp_bridge: Bridge, http_session: aiohttp.ClientSession
) -> None:
    """A room the bridge has deleted from its scene cache (fade-controlled --
    BRIDGE_BEHAVIOUR.md fact 2) must have ``room_scene() is None``, never 0."""
    snapshot = await udp_bridge.get_state_snapshot(http_session)
    absent_from_scene_cache = set(snapshot.rooms) - set(udp_bridge.scene_cache)
    if not absent_from_scene_cache:
        pytest.skip(
            "every room in the level table currently has a scene-cache entry "
            "on this installation; nothing to check here right now"
        )
    for room in absent_from_scene_cache:
        assert snapshot.room_scene(room) is None
