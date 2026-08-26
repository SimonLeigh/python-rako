"""Bridge-level state API: scenes.htm reads and snapshot assembly."""

import aiohttp
import pytest

from python_rako.bridge import Bridge
from python_rako.exceptions import RakoBridgeError
from python_rako.model import LevelCache, LevelCacheItem, RoomChannel, SceneCache
from python_rako.state import StateSource

# TEST-NET-1 (RFC 5737).
BRIDGE_HOST = "192.0.2.10"

LEVEL_TABLE = LevelCache(
    {
        RoomChannel(6, 1): LevelCacheItem(0x80, 6, 1, {1: 255, 2: 26}),
        RoomChannel(9, 1): LevelCacheItem(0x80, 9, 1, {1: 255, 2: 192}),
    }
)


def make_bridge() -> Bridge:
    bridge = Bridge(BRIDGE_HOST, 9761, "bridge", "00:00:00:00:00:00")
    bridge.level_cache = LEVEL_TABLE
    return bridge


async def test_get_scene_cache_http(aresponses):
    """scenes.htm returns the cache as hex words of ``scene << 10 | room``."""
    aresponses.add(
        BRIDGE_HOST,
        "/scenes.htm",
        "GET",
        aresponses.Response(text="0C1C040C", status=200),
    )
    bridge = make_bridge()
    async with aiohttp.ClientSession() as session:
        scene_cache = await bridge.get_scene_cache_http(session)
    assert scene_cache == SceneCache({28: 3, 12: 1})


async def test_get_scene_cache_http_raises_on_a_bad_response(aresponses):
    aresponses.add(BRIDGE_HOST, "/scenes.htm", "GET", aresponses.Response(text="", status=500))
    bridge = make_bridge()
    async with aiohttp.ClientSession() as session:
        with pytest.raises(RakoBridgeError, match="scene cache"):
            await bridge.get_scene_cache_http(session)


async def test_get_state_snapshot_over_http(aresponses):
    # Room 6 in scene 2; room 9 deliberately absent (fade-controlled).
    aresponses.add(
        BRIDGE_HOST,
        "/scenes.htm",
        "GET",
        aresponses.Response(text="0806", status=200),
    )
    bridge = make_bridge()
    async with aiohttp.ClientSession() as session:
        snapshot = await bridge.get_state_snapshot(session)

    assert snapshot.room_scene(6) == 2
    assert snapshot.channel_level(6, 1) == 26
    assert snapshot.channel_state(6, 1).source is StateSource.SCENE_DERIVED

    assert snapshot.room_scene(9) is None
    assert snapshot.channel_level(9, 1) is None
    assert snapshot.channel_state(9, 1).source is StateSource.UNKNOWN_AFTER_FADE


async def test_get_state_snapshot_falls_back_to_the_udp_query(aresponses, monkeypatch):
    aresponses.add(BRIDGE_HOST, "/scenes.htm", "GET", aresponses.Response(text="", status=404))
    bridge = make_bridge()

    async def fake_cache_state(cache_type=None):
        return LEVEL_TABLE, SceneCache({6: 1})

    monkeypatch.setattr(bridge, "get_cache_state", fake_cache_state)

    async with aiohttp.ClientSession() as session:
        snapshot = await bridge.get_state_snapshot(session)

    assert snapshot.room_scene(6) == 1
    assert snapshot.channel_level(6, 1) == 255


async def test_get_state_snapshot_without_a_session_uses_udp(monkeypatch):
    bridge = make_bridge()
    calls: list = []

    async def fake_cache_state(cache_type=None):
        calls.append(cache_type)
        return LEVEL_TABLE, SceneCache({9: 2})

    monkeypatch.setattr(bridge, "get_cache_state", fake_cache_state)
    snapshot = await bridge.get_state_snapshot()

    assert snapshot.room_scene(9) == 2
    assert snapshot.channel_level(9, 1) == 192
    assert calls  # the UDP query was used


async def test_get_state_snapshot_refreshes_the_level_table_on_demand(monkeypatch):
    bridge = make_bridge()
    refreshed = LevelCache({RoomChannel(6, 1): LevelCacheItem(0x80, 6, 1, {1: 10, 2: 20})})
    seen: list = []

    async def fake_cache_state(cache_type=None):
        seen.append(cache_type)
        return refreshed, SceneCache({6: 2})

    monkeypatch.setattr(bridge, "get_cache_state", fake_cache_state)
    snapshot = await bridge.get_state_snapshot(refresh_level_table=True)

    assert snapshot.channel_level(6, 1) == 20
    assert len(seen) == 2  # level table, then scene cache
