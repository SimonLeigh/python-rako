"""Mutating command tests against a live bridge.

Everything here requires ``RAKO_LIVE_MUTATE=1`` plus ``RAKO_TEST_ROOM`` /
``RAKO_TEST_CHANNEL`` (see the ``test_channel`` fixture in ``conftest.py`` and
``tests/README_TESTING.md``). Every test restores the level it found the
channel at before it started, so re-running the suite is safe, and none of
them assume a starting state.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from python_rako import Bridge, ChannelStatusMessage
from python_rako.protocol import FadeMessage, StopFadeMessage

if TYPE_CHECKING:
    import aiohttp

pytestmark = pytest.mark.live


async def test_set_level_round_trip(
    verified_bridge: Bridge,
    http_session: aiohttp.ClientSession,
    test_channel: tuple[int, int],
) -> None:
    """0 -> verified echo -> restore the previous level -> verified echo."""
    room, channel = test_channel
    snapshot = await verified_bridge.get_state_snapshot(http_session)
    previous_level = snapshot.channel_level(room, channel)
    if previous_level is None:
        # Genuinely unknown (e.g. a fade-controlled room) -- pick a safe,
        # fully-on default rather than guessing at "the" previous level.
        previous_level = 255

    try:
        off_echo = await verified_bridge.set_channel_level(room, channel, 0)
        assert isinstance(off_echo, ChannelStatusMessage)
        assert off_echo.brightness == 0
    finally:
        restore_echo = await verified_bridge.set_channel_level(room, channel, previous_level)
        assert isinstance(restore_echo, ChannelStatusMessage)
        assert restore_echo.brightness == previous_level


async def test_command_echo_latency(
    verified_bridge: Bridge,
    http_session: aiohttp.ClientSession,
    test_channel: tuple[int, int],
) -> None:
    """BRIDGE_BEHAVIOUR.md fact 14: echo observed at 144-306ms; assert < 1.5s.

    1.5s is the library's own DEFAULT_VERIFY_TIMEOUT (~5x headroom over the
    observed range); a command that verifies at all has, by construction,
    already met it, so this also pins the measured numbers for visibility.
    """
    room, channel = test_channel
    snapshot = await verified_bridge.get_state_snapshot(http_session)
    previous_level = snapshot.channel_level(room, channel)
    if previous_level is None:
        previous_level = 255

    # Alternate two levels distinct from `previous_level` so every command in
    # the sample actually changes the channel and produces a fresh echo.
    levels = [255, 128] if previous_level != 255 else [128, 64]
    latencies: list[float] = []
    try:
        for level in levels:
            start = time.monotonic()
            echo = await verified_bridge.set_channel_level(room, channel, level)
            latencies.append(time.monotonic() - start)
            assert isinstance(echo, ChannelStatusMessage)
            assert echo.brightness == level
    finally:
        await verified_bridge.set_channel_level(room, channel, previous_level)

    samples = ", ".join(f"{lat * 1000:.0f}ms" for lat in latencies)
    print(
        f"\nsend->echo latency for room {room} ch {channel}: "
        f"min={min(latencies) * 1000:.0f}ms max={max(latencies) * 1000:.0f}ms "
        f"samples=[{samples}]"
    )
    for latency in latencies:
        assert latency < 1.5, f"echo took {latency:.2f}s, exceeding the 1.5s verify window"


async def test_fade_up_then_stop_produces_fade_and_stop_broadcasts(
    verified_bridge: Bridge, test_channel: tuple[int, int]
) -> None:
    """A keypad-style press/release: FADE echoes the press, STOP the release.

    No level is broadcast when the fade stops (BRIDGE_BEHAVIOUR.md facts 1 and
    3), so this test deliberately does not try to restore "the" previous
    level afterwards -- there is nothing to restore to.
    """
    room, channel = test_channel

    fade_echo = await verified_bridge.fade_up(room, channel)
    assert isinstance(fade_echo, FadeMessage)
    assert (fade_echo.room, fade_echo.channel) == (room, channel)

    stop_echo = await verified_bridge.stop_fade(room, channel)
    assert isinstance(stop_echo, StopFadeMessage)
    assert (stop_echo.room, stop_echo.channel) == (room, channel)


async def test_command_without_listener_returns_none(
    udp_bridge: Bridge,
    http_session: aiohttp.ClientSession,
    test_channel: tuple[int, int],
) -> None:
    """No listener attached => the caller learns nothing, never a false True.

    Replays the channel's own current level (a no-op on the actual lights) so
    this stays a genuine unverified UDP command without changing anything.
    """
    room, channel = test_channel
    snapshot = await udp_bridge.get_state_snapshot(http_session)
    previous_level = snapshot.channel_level(room, channel)
    if previous_level is None:
        pytest.skip(f"room {room} channel {channel} has no known level to replay unverified")

    result = await udp_bridge.set_channel_level(room, channel, previous_level)
    assert result is None
