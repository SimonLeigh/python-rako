from __future__ import annotations

import asyncio
import logging
import socket
from asyncio.trsock import TransportSocket  # noqa
from typing import TypedDict

import asyncio_dgram

from python_rako.bridge import Bridge, BridgeCommanderHTTP, BridgeCommanderUDP  # noqa
from python_rako.const import RAKO_BRIDGE_DEFAULT_PORT, MessageType, RequestType  # noqa
from python_rako.exceptions import RakoBridgeError  # noqa
from python_rako.model import (  # noqa
    BridgeInfo,
    ChannelLight,
    ChannelStatusMessage,
    ChannelVentilation,
    LevelCache,
    LevelCacheItem,
    Light,
    RoomChannel,
    RoomLight,
    RoomVentilation,
    SceneCache,
    SceneStatusMessage,
    UnsupportedMessage,
    Ventilation,
)

_LOGGER = logging.getLogger(__name__)


class BridgeDescription(TypedDict, total=False):
    host: str
    port: int
    name: str
    mac: str


async def discover_bridge() -> BridgeDescription:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    server = await asyncio_dgram.from_socket(sock)
    await server.send(b"D", ("255.255.255.255", RAKO_BRIDGE_DEFAULT_PORT))
    msg, (host, port) = await server.recv()
    bridge_description: BridgeDescription = {"host": host, "port": port}
    try:
        name, mac = msg.decode("utf8").split()
        bridge_description["name"] = name
        bridge_description["mac"] = mac
    except ValueError:
        raise ValueError(f"Couldn't interpret discovery response message: {msg}")
    return bridge_description


def main() -> None:
    bridge_desc: BridgeDescription = asyncio.run(discover_bridge())
    print(bridge_desc)


if __name__ == "__main__":
    main()
