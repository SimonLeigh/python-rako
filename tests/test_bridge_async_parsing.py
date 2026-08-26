from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest

from python_rako.bridge import Bridge
from python_rako.model import BridgeInfo, ChannelLight, RoomLight


class _MockGetResponse:
    """Minimal stand-in for the aiohttp `session.get(...)` async context manager."""

    def __init__(self, text: str):
        self._text = text

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> _MockGetResponse:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_get_info_parses_xml_via_to_thread(
    rako_xml: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """XML parsing must be offloaded via asyncio.to_thread, not run inline on the loop."""
    calls: list[Any] = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy_to_thread)

    bridge = Bridge(host="127.0.0.1", port=9761, name="RAKOBRIDGE", mac="00:11:22:33:44:55")

    async with aiohttp.ClientSession() as session:
        with patch("aiohttp.ClientSession.get", return_value=_MockGetResponse(rako_xml)):
            info = await bridge.get_info(session)

    assert calls == [Bridge.get_bridge_info_from_discovery_xml]
    assert isinstance(info, BridgeInfo)
    assert info.hostName == "RAKOBRIDGE"


@pytest.mark.asyncio
async def test_discover_devices_parses_xml_via_to_thread(
    rako_xml: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Device discovery must also offload parsing, and still return correct devices."""
    calls: list[Any] = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy_to_thread)

    bridge = Bridge(host="127.0.0.1", port=9761, name="RAKOBRIDGE", mac="00:11:22:33:44:55")

    async with aiohttp.ClientSession() as session:
        with patch("aiohttp.ClientSession.get", return_value=_MockGetResponse(rako_xml)):
            lights, ventilation = await bridge.discover_devices(session)

    assert len(calls) == 1  # the materialising lambda ran off the event loop
    assert ventilation == []
    assert RoomLight(room_id=5, room_title="Living Room") in lights
    assert any(isinstance(light, ChannelLight) and light.room_id == 9 for light in lights)


@pytest.mark.asyncio
async def test_get_rako_xml_raises_bridge_error_when_response_text_is_missing() -> None:
    """The old bare `assert self._cached_xml is not None` is now an explicit,
    catchable RakoBridgeError -- e.g. if the bridge sends back an empty/missing body.
    """
    from python_rako.exceptions import RakoBridgeError

    bridge = Bridge(host="127.0.0.1", port=9761, name="RAKOBRIDGE", mac="00:11:22:33:44:55")

    async with aiohttp.ClientSession() as session:
        with patch(
            "aiohttp.ClientSession.get",
            return_value=_MockGetResponse(None),  # type: ignore[arg-type]
        ):
            with pytest.raises(RakoBridgeError):
                await bridge.get_rako_xml(session)
