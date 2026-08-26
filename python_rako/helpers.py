from __future__ import annotations

import logging
import socket
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

import asyncio_dgram

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from asyncio_dgram.aio import DatagramClient, DatagramServer

from python_rako.const import (
    SCENE_COMMAND_TO_NUMBER,
    CommandType,
    DataRecordType,
    MessageType,
    sentinel,
)
from python_rako.model import (
    ChannelStatusMessage,
    CommandUDP,
    EOFResponse,
    LevelCache,
    LevelCacheItem,
    RoomChannel,
    SceneCache,
    SceneStatusMessage,
    StatusMessage,
    UnsupportedMessage,
)

_LOGGER = logging.getLogger(__name__)


@asynccontextmanager
async def get_dg_listener(port: int, listen_host: str = "0.0.0.0") -> AsyncIterator[DatagramServer]:
    server: DatagramServer | None = None
    try:
        # Create socket with broadcast capability for receiving status messages
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((listen_host, port))

        # asyncio-dgram lacks type hints; from_socket() is typed as
        # DatagramServer | DatagramClient, but a bound (unconnected) socket
        # always yields a DatagramServer.
        server = cast("DatagramServer", await asyncio_dgram.from_socket(sock))
        yield server
    finally:
        if server:
            server.close()


@asynccontextmanager
async def get_dg_commander(host: str, port: int) -> AsyncIterator[DatagramClient]:
    client: DatagramClient | None = None
    try:
        client = await asyncio_dgram.connect((host, port))
        yield client
    finally:
        if client:
            client.close()


def deserialise_byte_list(
    byte_list: list[int],
) -> UnsupportedMessage | EOFResponse | StatusMessage | SceneCache | LevelCache:
    try:
        message_type = MessageType(byte_list[0])
    except ValueError:
        _LOGGER.warning("Unsupported UDP message type byte_list=%s", byte_list)
        return UnsupportedMessage()

    try:
        if message_type == MessageType.STATUS:
            return deserialise_status_message(byte_list)

        if message_type == MessageType.SCENE_CACHE:
            return deserialise_scene_cache_message(byte_list)

        if message_type == MessageType.LEVEL_CACHE:
            if byte_list[1] == DataRecordType.EOF.value:
                return EOFResponse()
            if byte_list[1] == DataRecordType.DATA.value:
                return deserialise_level_cache_message(byte_list)
    except (ValueError, KeyError):
        _LOGGER.warning(
            "Unsupported UDP message: message_type=%s, byte_list=%s",
            message_type,
            byte_list,
        )
    return UnsupportedMessage()


def deserialise_status_message(byte_list: list[int]) -> StatusMessage:
    data_length = byte_list[1] - 5
    room = byte_list[2] * 256 + byte_list[3]
    channel = byte_list[4]
    command = CommandType(byte_list[5])
    data = byte_list[6 : 6 + data_length]
    if command in (CommandType.LEVEL_SET_LEGACY, CommandType.SET_LEVEL):
        return ChannelStatusMessage(
            room=room,
            channel=channel,
            brightness=data[1],
        )

    # command is one of SET_SCENE or SC1_LEGACY, SC2_LEGACY, SC3_LEGACY, SC4_LEGACY
    scene = data[1] if command == CommandType.SET_SCENE else SCENE_COMMAND_TO_NUMBER[command]

    return SceneStatusMessage(
        room=room,
        channel=channel,
        scene=scene,
    )


def deserialise_level_cache_message(byte_list: list[int]) -> LevelCache:
    scene_cache: dict[RoomChannel, LevelCacheItem] = {}
    it = iter(byte_list)
    next(it)  # message type
    for b in it:
        if b != DataRecordType.DATA.value:
            break
        lc = LevelCacheItem(next(it), next(it), next(it), {i: next(it) for i in range(1, 18, 1)})
        scene_cache[RoomChannel(lc.room, lc.channel)] = lc
    return LevelCache(scene_cache)


def deserialise_scene_cache_message(byte_list: list[int]) -> SceneCache:
    """Deserialise a scene-cache message.

    Each room's entry is a 2-byte record ``hi, lo`` encoding
    ``scene << 10 | room`` (room is 10 bits: 2 bits carried in the low
    bits of ``hi``, 8 bits in ``lo``), so rooms above 255 are representable.
    """
    scene_cache = SceneCache()
    it = iter(byte_list)
    next(it)  # message type
    next(it)  # undocumented. following bytes?
    for hi in it:
        lo = next(it, sentinel)
        if lo == sentinel:
            continue
        room = ((hi & 0x03) << 8) | lo  # type: ignore[operator]
        scene = hi >> 2
        scene_cache[room] = scene
    return scene_cache


def calc_crc(byte_list: list[int]) -> int:
    return 256 - sum(byte_list) % 256


def command_to_byte_list(command: CommandUDP) -> list[int]:
    checksum_list: list[int] = [
        5 + len(command.data),  # following bytes
        int(command.room / 256),  # high room number
        command.room % 256,  # low room number
        command.channel,  # channel
        command.command.value,  # command
        *command.data,
    ]

    byte_list: list[int] = [
        command.message_type.value,
        *checksum_list,
        calc_crc(checksum_list),
    ]

    return byte_list


_scene_brightness = {
    # rako_scene_number: brightness
    1: 255,
    2: 192,
    3: 128,
    4: 64,
    0: 0,
}


def convert_to_brightness(scene_number: int) -> int:
    # scenes can exist outside of 0-4.
    # rather than KeyError, lets return mid-level brightness
    return _scene_brightness.get(scene_number, 128)


_scene_windows = {
    # rako_scene: (brightness_high, brightness_low)
    1: {"low": 224, "high": 256},  # expect 255 (100%)
    2: {"low": 160, "high": 224},  # expect 192 (75%)
    3: {"low": 96, "high": 160},  # expect 128 (50%)
    4: {"low": 1, "high": 96},  # expect 64 (25%)
    0: {"low": 0, "high": 1},  # expect 0 (0%)
}


def convert_to_scene(brightness: int) -> int:
    """
    Return the rako scene of the light.

    This directly corresponds to the value of the button on the app and is accessed through the
    brightness
    :param brightness: int representing brightness 0-255
    """

    scene = next(k for k, v in _scene_windows.items() if v["low"] <= brightness < v["high"])
    return scene
