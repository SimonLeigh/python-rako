from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Generator
from typing import Any

import aiohttp
import xmltodict
from asyncio_dgram.aio import DatagramServer

from python_rako.const import (
    COMMAND_SUCCESS_RESPONSE,
    CommandType,
    Flags,
    MessageType,
    RequestType,
)
from python_rako.exceptions import RakoBridgeError
from python_rako.helpers import (
    command_to_byte_list,
    deserialise_byte_list,
    get_dg_commander,
)
from python_rako.model import (
    BridgeInfo,
    ChannelLight,
    ChannelVentilation,
    CommandHTTP,
    CommandLevelHTTP,
    CommandSceneHTTP,
    CommandUDP,
    EOFResponse,
    LevelCache,
    RoomLight,
    RoomVentilation,
    SceneCache,
)

_LOGGER = logging.getLogger(__name__)


class _BridgeCommander:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    async def set_room_scene(self, room_id: int, scene: int) -> None:
        """Set the scene of a room."""
        raise NotImplementedError()

    async def set_room_brightness(self, room_id: int, brightness: int) -> None:
        """Set the brightness of a room."""
        await self.set_channel_brightness(room_id, 0, brightness)

    async def set_channel_brightness(
        self, room_id: int, channel_id: int, brightness: int
    ) -> None:
        """Set the brightness of a channel."""
        raise NotImplementedError()


class BridgeCommanderUDP(_BridgeCommander):
    async def set_room_scene(self, room_id: int, scene: int) -> None:
        """Set the scene of a room."""
        command = CommandUDP(
            room=room_id,
            channel=0,
            command=CommandType.SET_SCENE,
            data=[Flags.USE_DEFAULT_FADE_RATE.value, scene],
        )
        await self._send_command(command)

    async def set_channel_brightness(
        self, room_id: int, channel_id: int, brightness: int
    ) -> None:
        """Set the brightness of a channel."""
        command = CommandUDP(
            room=room_id,
            channel=channel_id,
            command=CommandType.SET_LEVEL,
            data=[Flags.USE_DEFAULT_FADE_RATE.value, brightness],
        )
        await self._send_command(command)

    async def _send_command(self, command: CommandUDP) -> None:
        _LOGGER.debug("Sending command: %s", command)
        byte_list = command_to_byte_list(command)
        async with get_dg_commander(self.host, self.port) as dg_client:
            _LOGGER.debug("Sending command bytes: %s", byte_list)
            await dg_client.send(bytes(byte_list))
            data, _ = await dg_client.recv()

        if data.decode("utf8").strip() != COMMAND_SUCCESS_RESPONSE:
            _LOGGER.warning("Bad response after command %s %s", command, data)


class BridgeCommanderHTTP(_BridgeCommander):
    def __init__(self, host: str, port: int, aiohttp_session: aiohttp.ClientSession):
        super().__init__(host, port)
        self.aiohttp_session = aiohttp_session

    @property
    def _command_url(self) -> str:
        return f"http://{self.host}/rako.cgi"

    async def set_room_scene(self, room_id: int, scene: int) -> None:
        """Set the scene of a room."""
        command = CommandSceneHTTP(
            room=room_id,
            channel=0,
            scene=scene,
        )
        await self._send_command(command)

    async def set_channel_brightness(
        self, room_id: int, channel_id: int, brightness: int
    ) -> None:
        """Set the brightness of a channel."""
        command = CommandLevelHTTP(
            room=room_id,
            channel=channel_id,
            level=brightness,
        )
        await self._send_command(command)

    async def _send_command(self, command: CommandHTTP) -> None:
        params = command.as_params()
        _LOGGER.debug("Posting params %s", params)
        await self.aiohttp_session.post(self._command_url, params=params)


class Bridge:
    def __init__(
        self,
        host: str,
        port: int,
        name: str,
        mac: str,
        bridge_commander: _BridgeCommander | None = None,
    ):
        self.host = host
        self.port = port
        self.name = name
        self.mac = mac
        self._bridge_commander = (
            bridge_commander if bridge_commander else BridgeCommanderUDP(host, port)
        )
        self.level_cache: LevelCache = LevelCache()
        self.scene_cache: SceneCache = SceneCache()
        self._cached_xml: str | None = None

    @property
    def _discovery_url(self) -> str:
        return f"http://{self.host}/rako.xml"

    async def get_rako_xml(
        self, session: aiohttp.ClientSession, force_refresh: bool = False
    ) -> str:
        if self._cached_xml is None or force_refresh:
            async with session.get(self._discovery_url) as response:
                self._cached_xml = await response.text()
        assert self._cached_xml is not None
        return self._cached_xml

    async def discover_devices(
        self, session: aiohttp.ClientSession, force_refresh: bool = False
    ) -> tuple[
        list[RoomLight | ChannelLight], list[RoomVentilation | ChannelVentilation]
    ]:
        """Discover all devices by fetching XML once and parsing all device types.

        Returns a tuple of (lights, ventilation) to avoid race conditions.
        """
        rako_xml = await self.get_rako_xml(session, force_refresh)

        lights: list[RoomLight | ChannelLight] = []
        ventilation: list[RoomVentilation | ChannelVentilation] = []

        for device in self.get_devices_from_discovery_xml(rako_xml):
            if isinstance(device, (RoomLight, ChannelLight)):
                lights.append(device)
            elif isinstance(device, (RoomVentilation, ChannelVentilation)):
                ventilation.append(device)

        return lights, ventilation

    async def discover_lights(
        self, session: aiohttp.ClientSession, force_refresh: bool = False
    ) -> AsyncGenerator[RoomLight | ChannelLight]:
        """Discover lights by fetching XML once and filtering for lights."""
        lights, _ = await self.discover_devices(session, force_refresh)
        for light in lights:
            yield light

    async def discover_ventilation(
        self, session: aiohttp.ClientSession, force_refresh: bool = False
    ) -> AsyncGenerator[RoomVentilation | ChannelVentilation]:
        """Discover ventilation by fetching XML once and filtering for ventilation."""
        _, ventilation = await self.discover_devices(session, force_refresh)
        for vent in ventilation:
            yield vent

    async def discover_all_devices(
        self, session: aiohttp.ClientSession, force_refresh: bool = False
    ) -> AsyncGenerator[
        RoomLight | ChannelLight | RoomVentilation | ChannelVentilation
    ]:
        """Discover all devices by fetching XML once."""
        lights, ventilation = await self.discover_devices(session, force_refresh)
        for light in lights:
            yield light
        for vent in ventilation:
            yield vent

    async def get_info(
        self, session: aiohttp.ClientSession, force_refresh: bool = False
    ) -> BridgeInfo:
        try:
            rako_xml = await self.get_rako_xml(session, force_refresh)
            info = self.get_bridge_info_from_discovery_xml(rako_xml)
        except (KeyError, ValueError) as ex:
            raise RakoBridgeError(f"unsupported bridge: {ex}")
        except aiohttp.ClientError as ex:
            raise RakoBridgeError(f"cannot connect to bridge: {ex}")
        return info

    @staticmethod
    def get_bridge_info_from_discovery_xml(xml: str) -> BridgeInfo:
        xml_dict = xmltodict.parse(xml)
        info = xml_dict["rako"].get("info", dict())
        config = xml_dict["rako"].get("config", dict())
        return BridgeInfo(
            version=info.get("version"),
            buildDate=info.get("buildDate"),
            hostName=info.get("hostName"),
            hostIP=info.get("hostIP"),
            hostMAC=info.get("hostMAC"),
            hwStatus=info.get("hwStatus"),
            dbVersion=info.get("dbVersion"),
            requirepassword=config.get("requirepassword"),
            passhash=config.get("passhash"),
            charset=config.get("charset"),
        )

    @staticmethod
    def get_lights_from_discovery_xml(
        xml: str,
    ) -> Generator[RoomLight | ChannelLight]:
        for device in Bridge.get_devices_from_discovery_xml(xml, "Lights"):
            if isinstance(device, (RoomLight, ChannelLight)):
                yield device

    @staticmethod
    def get_all_devices_from_discovery_xml(
        xml: str,
    ) -> Generator[RoomLight | ChannelLight | RoomVentilation | ChannelVentilation]:
        """Get all devices (lights and ventilation) from discovery XML."""
        return Bridge.get_devices_from_discovery_xml(xml)

    @staticmethod
    def get_devices_from_discovery_xml(
        xml: str, device_types: str | list[str] | None = None
    ) -> Generator[RoomLight | ChannelLight | RoomVentilation | ChannelVentilation]:
        # Handle different input types for backward compatibility
        if device_types is None or device_types == "All":
            target_types = {"Lights", "Ventilation"}
        elif isinstance(device_types, str):
            target_types = {device_types}
        else:
            target_types = set(device_types)

        xml_dict = xmltodict.parse(xml, force_list={"Room"})
        for room in xml_dict["rako"]["rooms"]["Room"]:
            room_id = int(room["@id"])
            room_type = room.get("Type", "Lights")
            if room_type not in target_types:
                continue
            room_title = room["Title"]

            # Yield room-level device
            if room_type == "Lights":
                yield RoomLight(room_id, room_title)
            elif room_type == "Ventilation":
                yield RoomVentilation(room_id, room_title)

            # Yield channel-level devices
            channels_section = room.get("Channel", [])
            channels = (
                channels_section
                if isinstance(channels_section, list)
                else [channels_section]
            )
            for channel in channels:
                channel_id = int(channel["@id"])
                channel_type = channel.get("type", "Default")
                channel_name = channel["Name"]
                channel_levels = channel["Levels"]

                if room_type == "Lights":
                    yield ChannelLight(
                        room_id,
                        room_title,
                        channel_id,
                        channel_type,
                        channel_name,
                        channel_levels,
                    )
                elif room_type == "Ventilation":
                    yield ChannelVentilation(
                        room_id,
                        room_title,
                        channel_id,
                        channel_type,
                        channel_name,
                        channel_levels,
                    )

    async def next_pushed_message(self, dg_listener: DatagramServer) -> Any | None:
        resp = await dg_listener.recv()
        if not resp:
            return None

        data, addr = resp
        remote_ip, _ = addr
        if remote_ip != self.host:
            return None

        byte_list = list(data)
        _LOGGER.debug("Received bytes: %s", byte_list)
        message = deserialise_byte_list(byte_list)
        _LOGGER.debug("Deserialised received message as: %s", message)
        return message

    async def get_cache_state(
        self, cache_type: RequestType = RequestType.SCENE_LEVEL_CACHE
    ) -> tuple[LevelCache, SceneCache]:
        scene_cache = SceneCache()
        level_cache = LevelCache()
        async with get_dg_commander(self.host, self.port) as dg_client:
            _LOGGER.debug("Requesting cache: %s", cache_type)
            await dg_client.send(bytes([MessageType.QUERY.value, cache_type.value]))

            while True:
                try:
                    data, _ = await asyncio.wait_for(dg_client.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    _LOGGER.warning("Timeout waiting for cache response")
                    break

                response = deserialise_byte_list(list(data))
                if isinstance(response, EOFResponse):
                    break
                if isinstance(response, SceneCache):
                    scene_cache = response
                if isinstance(response, LevelCache):
                    level_cache = response
                _LOGGER.debug("Cache response: %s", response)

        return level_cache, scene_cache

    async def set_room_scene(self, room_id: int, scene: int) -> None:
        """Set the scene of a room."""
        await self._bridge_commander.set_room_scene(room_id, scene)

    async def set_room_brightness(self, room_id: int, brightness: int) -> None:
        """Set the brightness of a room."""
        await self._bridge_commander.set_room_brightness(room_id, brightness)

    async def set_channel_brightness(
        self, room_id: int, channel_id: int, brightness: int
    ) -> None:
        """Set the brightness of a channel."""
        await self._bridge_commander.set_channel_brightness(
            room_id, channel_id, brightness
        )
