"""Bridge-level state API: scenes.htm reads and snapshot assembly."""

import asyncio
import contextlib
import socket
import time

import aiohttp
import pytest

from python_rako import bridge as bridge_module
from python_rako.bridge import Bridge
from python_rako.const import RequestType
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


# ---------------------------------------------------------------------------
# UDP cache queries
# ---------------------------------------------------------------------------

SCENE_CACHE_FRAME = bytes([67, 3, 12, 28, 213])  # room 28, scene 3
EOF_FRAME = bytes([88, 255])


class _FakeCacheBridge:
    """A loopback UDP endpoint that answers cache queries like a bridge."""

    def __init__(self, *, send_eof: bool = False) -> None:
        self.send_eof = send_eof
        self.queries: list[list[int]] = []
        self.port = 0
        self._sock: socket.socket | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.setblocking(False)
        self.port = self._sock.getsockname()[1]
        self._task = asyncio.create_task(self._serve())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._sock is not None:
            self._sock.close()

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        assert self._sock is not None
        while True:
            data, addr = await loop.sock_recvfrom(self._sock, 512)
            self.queries.append(list(data))
            self._sock.sendto(SCENE_CACHE_FRAME, addr)
            if self.send_eof:
                self._sock.sendto(EOF_FRAME, addr)


async def test_a_scene_only_query_returns_without_waiting_for_an_eof(caplog):
    """The bridge sends no EOF after a 'C' frame; waiting burnt 2s per poll."""
    fake = _FakeCacheBridge(send_eof=False)
    await fake.start()
    try:
        bridge = Bridge("127.0.0.1", fake.port, "fake", "mac")
        started = time.monotonic()
        _, scene_cache = await bridge.get_cache_state(RequestType.SCENE_CACHE)
        elapsed = time.monotonic() - started
    finally:
        await fake.stop()

    assert scene_cache == SceneCache({28: 3})
    assert elapsed < 1.0
    assert "Timeout waiting" not in caplog.text


async def test_a_level_query_still_waits_for_the_eof_record():
    fake = _FakeCacheBridge(send_eof=True)
    await fake.start()
    try:
        bridge = Bridge("127.0.0.1", fake.port, "fake", "mac")
        started = time.monotonic()
        await bridge.get_cache_state(RequestType.LEVEL_CACHE)
        elapsed = time.monotonic() - started
    finally:
        await fake.stop()
    assert elapsed < 1.0


async def test_a_genuinely_missing_reply_still_warns(caplog):
    """Silence is still worth a warning; only the expected end is not."""
    silent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    silent.bind(("127.0.0.1", 0))
    try:
        bridge = Bridge("127.0.0.1", silent.getsockname()[1], "fake", "mac")
        await bridge.get_cache_state(RequestType.SCENE_CACHE)
    finally:
        silent.close()
    assert "Timeout waiting for SCENE_CACHE response" in caplog.text


# ---------------------------------------------------------------------------
# Level-table loading
# ---------------------------------------------------------------------------


async def test_an_empty_level_table_is_not_re_read_on_every_snapshot(monkeypatch):
    """An empty LevelCache is falsy; truthiness would re-query (and time out)."""
    bridge = Bridge(BRIDGE_HOST, 9761, "bridge", "mac")
    calls: list = []

    async def fake_cache_state(cache_type=None):
        calls.append(cache_type)
        return LevelCache(), SceneCache({6: 2})

    monkeypatch.setattr(bridge, "get_cache_state", fake_cache_state)

    await bridge.get_state_snapshot()
    await bridge.get_state_snapshot()
    await bridge.get_state_snapshot()

    level_queries = [c for c in calls if c is RequestType.LEVEL_CACHE]
    assert len(level_queries) == 1


async def test_an_empty_level_table_is_retried_once_the_interval_passes(monkeypatch):
    bridge = Bridge(BRIDGE_HOST, 9761, "bridge", "mac")
    calls: list = []

    async def fake_cache_state(cache_type=None):
        calls.append(cache_type)
        return LevelCache(), SceneCache({6: 2})

    monkeypatch.setattr(bridge, "get_cache_state", fake_cache_state)
    await bridge.get_state_snapshot()
    # Pretend the retry interval has elapsed.
    bridge._level_table_attempted_at -= bridge_module.LEVEL_TABLE_RETRY_INTERVAL + 1
    await bridge.get_state_snapshot()

    assert len([c for c in calls if c is RequestType.LEVEL_CACHE]) == 2


async def test_assigning_a_level_cache_counts_as_loaded(monkeypatch):
    bridge = Bridge(BRIDGE_HOST, 9761, "bridge", "mac")
    bridge.level_cache = LEVEL_TABLE
    calls: list = []

    async def fake_cache_state(cache_type=None):
        calls.append(cache_type)
        return LevelCache(), SceneCache({6: 2})

    monkeypatch.setattr(bridge, "get_cache_state", fake_cache_state)
    await bridge.get_state_snapshot()
    assert RequestType.LEVEL_CACHE not in calls


async def test_refresh_level_table_re_reads_and_replaces(monkeypatch):
    bridge = Bridge(BRIDGE_HOST, 9761, "bridge", "mac")
    bridge.level_cache = LEVEL_TABLE
    replacement = LevelCache({RoomChannel(6, 1): LevelCacheItem(0x80, 6, 1, {1: 7, 2: 8})})

    async def fake_cache_state(cache_type=None):
        assert cache_type is RequestType.LEVEL_CACHE
        return replacement, SceneCache()

    monkeypatch.setattr(bridge, "get_cache_state", fake_cache_state)
    assert await bridge.refresh_level_table() is replacement
    assert bridge.level_cache is replacement
