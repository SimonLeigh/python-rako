from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, cast

import aiohttp
import xmltodict

from python_rako.commands import (
    DEFAULT_RETRIES,
    DEFAULT_VERIFY_TIMEOUT,
    CommandSender,
    CommandSpec,
    EchoVerifier,
    UdpCommandSender,
    execute_command,
    fade_command,
    level_command,
    scene_command,
    stop_command,
)
from python_rako.const import (
    COMMAND_SUCCESS_RESPONSE,
    CommandType,
    FadeDirection,
    Flags,
    MessageType,
    RequestType,
)
from python_rako.exceptions import RakoBridgeError, RakoCommandError, RakoConnectionError
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
from python_rako.protocol import decode_scene_cache_hex
from python_rako.state import BridgeStateSnapshot

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

    from asyncio_dgram.aio import DatagramServer

    from python_rako.listener import StatusListener
    from python_rako.model import StatusMessage

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

    async def set_channel_brightness(self, room_id: int, channel_id: int, brightness: int) -> None:
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

    async def set_channel_brightness(self, room_id: int, channel_id: int, brightness: int) -> None:
        """Set the brightness of a channel."""
        command = CommandUDP(
            room=room_id,
            channel=channel_id,
            command=CommandType.SET_LEVEL,
            data=[Flags.USE_DEFAULT_FADE_RATE.value, brightness],
        )
        await self._send_command(command)

    async def _send_command(self, command: CommandUDP) -> None:
        """Send command with retry logic."""
        await self._send_command_with_retry(command, max_retries=2)

    async def _send_command_with_retry(self, command: CommandUDP, max_retries: int = 2) -> bool:
        """Send command with retry logic."""
        _LOGGER.debug("Sending command: %s", command)
        byte_list = command_to_byte_list(command)

        for attempt in range(max_retries + 1):
            try:
                async with get_dg_commander(self.host, self.port) as dg_client:
                    _LOGGER.debug("Sending command bytes: %s (attempt %d)", byte_list, attempt + 1)
                    await dg_client.send(bytes(byte_list))
                    try:
                        # Add timeout to prevent indefinite blocking
                        data, _ = await asyncio.wait_for(dg_client.recv(), timeout=3.0)
                        if data.decode("utf8").strip() != COMMAND_SUCCESS_RESPONSE:
                            _LOGGER.warning("Bad response after command %s: %s", command, data)
                        return True
                    except TimeoutError:
                        # Many bridges don't send consistent responses, just log and continue
                        _LOGGER.debug(
                            "No response received for command %s (timeout after 3s)", command
                        )
                        return True  # Consider timeout as success for UDP commands
            except (ConnectionError, OSError) as e:
                if attempt == max_retries:
                    _LOGGER.error("Command failed after %d attempts: %s", max_retries + 1, e)
                    raise RakoConnectionError(
                        f"Failed to send command after {max_retries + 1} attempts: {e}"
                    ) from e
                _LOGGER.warning("Command attempt %d failed, retrying: %s", attempt + 1, e)
                await asyncio.sleep(0.5 * (2**attempt))  # Exponential backoff
        return False


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

    async def set_channel_brightness(self, room_id: int, channel_id: int, brightness: int) -> None:
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


class _HttpCommandSender(CommandSender):
    """Adapts the HTTP commander to the CommandSpec transport interface.

    ``rako.cgi`` only exposes scene selection and channel levels, so anything
    else (fades, stop, ident, store) has to go over UDP.  That is reported as an
    error rather than silently switched, so a caller is never surprised about
    which transport carried a command.
    """

    def __init__(self, commander: BridgeCommanderHTTP):
        self._commander = commander

    async def send(self, spec: CommandSpec) -> None:
        if spec.command is CommandType.SET_SCENE and len(spec.data) > 1:
            await self._commander.set_room_scene(spec.room, spec.data[1])
            return
        if spec.command is CommandType.SET_LEVEL and len(spec.data) > 1:
            await self._commander.set_channel_brightness(spec.room, spec.channel, spec.data[1])
            return
        if spec.command is CommandType.OFF:
            await self._commander.set_room_scene(spec.room, 0)
            return
        raise RakoCommandError(
            f"{spec.command.name} is not available over the HTTP transport; "
            "use the UDP commander for this command"
        )


class Bridge:
    def __init__(
        self,
        host: str,
        port: int,
        name: str,
        mac: str,
        bridge_commander: _BridgeCommander | None = None,
        *,
        listener: StatusListener | None = None,
        verify_timeout: float = DEFAULT_VERIFY_TIMEOUT,
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
        self._xml_fetch_lock = asyncio.Lock()
        self._last_cache_refresh: float = 0
        self.verify_timeout = verify_timeout
        self._echo_verifier = EchoVerifier()
        self._listener: StatusListener | None = None
        self._command_sender: CommandSender | None = None
        if listener is not None:
            self.attach_listener(listener)

    # -- echo verification -------------------------------------------------

    @property
    def listener(self) -> StatusListener | None:
        """The status listener used to verify commands, if one is attached."""
        return self._listener

    def attach_listener(self, listener: StatusListener) -> None:
        """Use ``listener``'s broadcasts to verify the commands we send.

        Without a listener the ``set_*`` methods still work, but they cannot
        confirm anything and say so rather than reporting false success.
        """
        self._listener = listener
        self._echo_verifier.attach(listener)

    def detach_listener(self) -> None:
        self._echo_verifier.detach()
        self._listener = None

    @property
    def _sender(self) -> CommandSender:
        if self._command_sender is None:
            if isinstance(self._bridge_commander, BridgeCommanderHTTP):
                self._command_sender = _HttpCommandSender(self._bridge_commander)
            else:
                self._command_sender = UdpCommandSender(self.host, self.port)
        return self._command_sender

    async def send_command(
        self,
        spec: CommandSpec,
        *,
        verify: bool = True,
        verify_timeout: float | None = None,
        retries: int = DEFAULT_RETRIES,
    ) -> StatusMessage | None:
        """Send a command and return the bridge's echo confirming it happened.

        Returns ``None`` when the command was not verified -- ``verify=False``,
        no listener attached, or a command the bridge does not echo.  It never
        claims success it cannot demonstrate.

        :raises RakoCommandError: no matching broadcast arrived after the
            initial send and ``retries`` resends.
        """
        return await execute_command(
            self._sender,
            spec,
            verifier=self._echo_verifier if self._listener is not None else None,
            verify=verify,
            verify_timeout=self.verify_timeout if verify_timeout is None else verify_timeout,
            retries=retries,
        )

    async def close(self) -> None:
        """Release command-transport resources. Does not stop the listener."""
        if self._command_sender is not None:
            await self._command_sender.close()

    @property
    def _discovery_url(self) -> str:
        return f"http://{self.host}/rako.xml"

    async def get_rako_xml(
        self, session: aiohttp.ClientSession, force_refresh: bool = False
    ) -> str:
        async with self._xml_fetch_lock:
            if self._cached_xml is None or force_refresh:
                async with session.get(self._discovery_url) as response:
                    self._cached_xml = await response.text()
        if self._cached_xml is None:
            raise RakoBridgeError("Failed to fetch bridge discovery XML")
        return self._cached_xml

    async def discover_devices(
        self, session: aiohttp.ClientSession, force_refresh: bool = False
    ) -> tuple[list[RoomLight | ChannelLight], list[RoomVentilation | ChannelVentilation]]:
        """Discover all devices by fetching XML once and parsing all device types.

        Returns a tuple of (lights, ventilation) to avoid race conditions.
        """
        rako_xml = await self.get_rako_xml(session, force_refresh)

        # Parsing (xmltodict) is a blocking, potentially slow operation for a
        # large rako.xml, so run it off the event loop.
        devices = await asyncio.to_thread(
            lambda: list(self.get_devices_from_discovery_xml(rako_xml))
        )

        lights: list[RoomLight | ChannelLight] = []
        ventilation: list[RoomVentilation | ChannelVentilation] = []

        for device in devices:
            if isinstance(device, RoomLight | ChannelLight):
                lights.append(device)
            elif isinstance(device, RoomVentilation | ChannelVentilation):
                ventilation.append(device)

        return lights, ventilation

    async def discover_lights(
        self, session: aiohttp.ClientSession, force_refresh: bool = False
    ) -> AsyncGenerator[RoomLight | ChannelLight, None]:
        """Discover lights by fetching XML once and filtering for lights."""
        lights, _ = await self.discover_devices(session, force_refresh)
        for light in lights:
            yield light

    async def discover_ventilation(
        self, session: aiohttp.ClientSession, force_refresh: bool = False
    ) -> AsyncGenerator[RoomVentilation | ChannelVentilation, None]:
        """Discover ventilation by fetching XML once and filtering for ventilation."""
        _, ventilation = await self.discover_devices(session, force_refresh)
        for vent in ventilation:
            yield vent

    async def get_info(
        self, session: aiohttp.ClientSession, force_refresh: bool = False
    ) -> BridgeInfo:
        try:
            rako_xml = await self.get_rako_xml(session, force_refresh)
            # Parsing (xmltodict) is a blocking operation, so run it off the event loop.
            info = await asyncio.to_thread(self.get_bridge_info_from_discovery_xml, rako_xml)
        except (KeyError, ValueError) as ex:
            raise RakoBridgeError(f"unsupported bridge: {ex}") from ex
        except aiohttp.ClientError as ex:
            raise RakoBridgeError(f"cannot connect to bridge: {ex}") from ex
        return info

    @staticmethod
    def get_bridge_info_from_discovery_xml(xml: str) -> BridgeInfo:
        xml_dict = xmltodict.parse(xml)
        info = xml_dict["rako"].get("info", {})
        config = xml_dict["rako"].get("config", {})
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
    def get_devices_from_discovery_xml(
        xml: str, device_types: str | list[str] | None = None
    ) -> Generator[RoomLight | ChannelLight | RoomVentilation | ChannelVentilation, None, None]:
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
                channels_section if isinstance(channels_section, list) else [channels_section]
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
        # Cast addr to correct type since asyncio-dgram lacks type hints
        addr = cast("tuple[str, int]", addr)
        remote_ip, _ = addr
        if remote_ip != self.host:
            return None

        byte_list = list(bytes(data))
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
                except TimeoutError:
                    _LOGGER.warning("Timeout waiting for cache response")
                    break

                response = deserialise_byte_list(list(bytes(data)))
                if isinstance(response, EOFResponse):
                    break
                if isinstance(response, SceneCache):
                    scene_cache = response
                if isinstance(response, LevelCache):
                    level_cache = response
                _LOGGER.debug("Cache response: %s", response)

        return level_cache, scene_cache

    async def refresh_cache_if_stale(self, max_age_seconds: int = 300) -> None:
        """Refresh level and scene cache if older than max_age_seconds."""
        current_time = time.time()
        if current_time - self._last_cache_refresh > max_age_seconds:
            try:
                self.level_cache, self.scene_cache = await self.get_cache_state()
                self._last_cache_refresh = current_time
                _LOGGER.debug("Cache refreshed for bridge %s", self.mac)
            except OSError as e:
                _LOGGER.warning("Failed to refresh cache for bridge %s: %s", self.mac, e)

    async def get_scene_cache_http(self, session: aiohttp.ClientSession) -> SceneCache:
        """Read the live scene cache over HTTP from ``scenes.htm``.

        Preferred over the UDP query for reconciliation polling: it uses no UDP
        socket, so it can never contend with the status listener.
        """
        url = f"http://{self.host}/scenes.htm"
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                text = await response.text()
        except aiohttp.ClientError as ex:
            raise RakoBridgeError(f"cannot read scene cache from bridge: {ex}") from ex
        scene_cache = decode_scene_cache_hex(text)
        _LOGGER.debug("Scene cache from %s: %d rooms", url, len(scene_cache))
        return scene_cache

    async def get_state_snapshot(
        self,
        session: aiohttp.ClientSession | None = None,
        *,
        refresh_level_table: bool = False,
    ) -> BridgeStateSnapshot:
        """Build a full state snapshot from the bridge's caches.

        The scene cache is read over HTTP when a session is supplied, falling
        back to the UDP query.  The level table is read over UDP and then
        reused, because scene *definitions* only change when a scene is stored
        (watch for STORE broadcasts, or pass ``refresh_level_table=True``).

        Rooms absent from the scene cache come back with ``scene=None`` and
        channels of unknown level -- never as "off".
        """
        if refresh_level_table or not self.level_cache:
            self.level_cache, _ = await self.get_cache_state(RequestType.LEVEL_CACHE)

        scene_cache: SceneCache | None = None
        if session is not None:
            try:
                scene_cache = await self.get_scene_cache_http(session)
            except RakoBridgeError as ex:
                _LOGGER.warning("scenes.htm unavailable (%s); falling back to the UDP query", ex)
        if scene_cache is None:
            _, scene_cache = await self.get_cache_state(RequestType.SCENE_CACHE)

        self.scene_cache = scene_cache
        return BridgeStateSnapshot.from_caches(scene_cache, self.level_cache)

    async def set_room_scene(
        self, room_id: int, scene: int, *, verify: bool = True
    ) -> StatusMessage | None:
        """Set the scene of a room; returns the bridge's echo.

        .. note::
           Since 0.5.0 this raises :class:`RakoCommandError` if a listener is
           attached and the bridge never confirms the change.  Update your
           state from the returned message rather than optimistically.
        """
        return await self.send_command(scene_command(room_id, scene), verify=verify)

    async def set_room_level(
        self, room_id: int, level: int, *, verify: bool = True
    ) -> StatusMessage | None:
        """Set the level of every channel in a room (channel 0)."""
        return await self.set_channel_level(room_id, 0, level, verify=verify)

    async def set_channel_level(
        self, room_id: int, channel_id: int, level: int, *, verify: bool = True
    ) -> StatusMessage | None:
        """Set the level of a single channel; returns the bridge's echo."""
        return await self.send_command(level_command(room_id, channel_id, level), verify=verify)

    async def fade_up(
        self, room_id: int, channel_id: int = 0, *, verify: bool = True
    ) -> StatusMessage | None:
        """Start fading up, exactly as holding a keypad's up button does.

        Must be terminated with :meth:`stop_fade`.  No level is broadcast when
        the fade stops, so the resulting level is genuinely unknown.
        """
        return await self.send_command(
            fade_command(room_id, channel_id, direction=FadeDirection.UP),
            verify=verify,
        )

    async def fade_down(
        self, room_id: int, channel_id: int = 0, *, verify: bool = True
    ) -> StatusMessage | None:
        """Start fading down; must be terminated with :meth:`stop_fade`."""
        return await self.send_command(
            fade_command(room_id, channel_id, direction=FadeDirection.DOWN),
            verify=verify,
        )

    async def stop_fade(
        self, room_id: int, channel_id: int = 0, *, verify: bool = True
    ) -> StatusMessage | None:
        """Stop a running fade."""
        return await self.send_command(stop_command(room_id, channel_id), verify=verify)

    async def set_room_brightness(
        self, room_id: int, brightness: int, *, verify: bool = True
    ) -> StatusMessage | None:
        """Deprecated alias for :meth:`set_room_level`."""
        return await self.set_room_level(room_id, brightness, verify=verify)

    async def set_channel_brightness(
        self, room_id: int, channel_id: int, brightness: int, *, verify: bool = True
    ) -> StatusMessage | None:
        """Deprecated alias for :meth:`set_channel_level`."""
        return await self.set_channel_level(room_id, channel_id, brightness, verify=verify)
