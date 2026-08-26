from collections.abc import AsyncGenerator

import aiohttp
import pytest

from python_rako import Bridge, StatusListener, discover_bridge
from python_rako.bridge import BridgeCommanderHTTP


@pytest.fixture
async def udp_bridge() -> AsyncGenerator[Bridge, None]:
    """A bridge using the default UDP transport.

    Yielded rather than returned so ``close()`` runs: the command transport
    holds a socket, and leaking one per test eventually exhausts the loop.
    """
    bridge_desc = await discover_bridge()
    async with Bridge(**bridge_desc) as bridge:
        yield bridge


@pytest.fixture
async def http_bridge() -> AsyncGenerator[Bridge, None]:
    bridge_desc = await discover_bridge()
    async with aiohttp.ClientSession() as session:
        bridge_commander = BridgeCommanderHTTP(bridge_desc["host"], bridge_desc["port"], session)
        async with Bridge(**bridge_desc, bridge_commander=bridge_commander) as bridge:
            yield bridge


@pytest.fixture
async def verified_bridge() -> AsyncGenerator[Bridge, None]:
    """A bridge with a live status listener, so commands are echo-verified.

    This is the configuration the library is designed around: ``set_*`` waits
    for the bridge's own broadcast and raises if it never arrives.
    """
    bridge_desc = await discover_bridge()
    async with (
        StatusListener(bridge_desc["host"]) as listener,
        Bridge(**bridge_desc, listener=listener) as bridge,
    ):
        yield bridge
