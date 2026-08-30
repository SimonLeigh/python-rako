"""``get_info`` and ``discover_devices`` against a live bridge. Read-only."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from python_rako import Bridge, RoomLight
from python_rako.model import ChannelLight

if TYPE_CHECKING:
    import aiohttp

pytestmark = pytest.mark.live


async def test_get_info(udp_bridge: Bridge, http_session: aiohttp.ClientSession) -> None:
    info = await udp_bridge.get_info(http_session)
    assert info.hostMAC  # every bridge reports its own MAC in rako.xml
    assert info.version


async def test_discover_devices_yields_at_least_one_room(
    udp_bridge: Bridge, http_session: aiohttp.ClientSession
) -> None:
    lights, _ventilation = await udp_bridge.discover_devices(http_session)
    assert lights, "expected at least one light device from rako.xml"
    room_ids = {light.room_id for light in lights}
    assert len(room_ids) >= 1
    assert any(isinstance(light, RoomLight) for light in lights)
    assert all(isinstance(light, RoomLight | ChannelLight) for light in lights)
