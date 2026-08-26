from collections.abc import Iterable
from dataclasses import dataclass, field

from python_rako.const import CommandType, MessageOrigin, MessageType


@dataclass(frozen=True)
class RoomChannel:
    room_id: int
    channel_id: int


@dataclass
class Light:
    room_id: int
    room_title: str
    channel_id: int

    @property
    def room_channel(self) -> RoomChannel:
        return RoomChannel(self.room_id, self.channel_id)


@dataclass
class RoomLight(Light):
    channel_id: int = 0


@dataclass
class ChannelLight(Light):
    channel_type: str
    channel_name: str
    channel_levels: str


@dataclass
class Ventilation:
    room_id: int
    room_title: str
    channel_id: int

    @property
    def room_channel(self) -> RoomChannel:
        return RoomChannel(self.room_id, self.channel_id)


@dataclass
class RoomVentilation(Ventilation):
    channel_id: int = 0


@dataclass
class ChannelVentilation(Ventilation):
    channel_type: str
    channel_name: str
    channel_levels: str


@dataclass(frozen=True)
class BridgeInfo:
    version: str
    buildDate: str
    hostName: str
    hostIP: str
    hostMAC: str
    hwStatus: str
    dbVersion: str
    requirepassword: str
    passhash: str
    charset: str


# Message: Bridge to Client
@dataclass
class UnsupportedMessage:
    pass


@dataclass
class EOFResponse:
    pass


@dataclass
class LevelCacheItem:
    active_deleted_reserved: int
    room: int
    channel: int
    scene_levels: dict[int, int]  # scene, level


# pylint: disable=E1101
class LevelCache(dict[RoomChannel, LevelCacheItem]):
    """dict of: RoomChannel, LevelCacheItem"""

    def get_channel_level(self, room_channel: RoomChannel, scene: int) -> int:
        level_cache_item = self.get(room_channel)
        if level_cache_item:
            return level_cache_item.scene_levels.get(scene, 0)
        return 0

    def get_channel_levels(self, room: int, scene: int) -> Iterable[tuple[int, int]]:
        for lci in self.values():
            if lci.room == room:
                brightness = lci.scene_levels.get(scene, 0)
                yield lci.channel, brightness


class SceneCache(dict[int, int]):
    """dict of: room id, scene number"""

    pass


@dataclass(frozen=True)
class StatusMessage:
    """Base class for every decoded ``'S'`` status broadcast.

    ``room`` and ``channel`` (plus the semantic payload added by subclasses) are
    the *identity* of the message and take part in ``==``/``hash``.  The
    protocol-level provenance fields are keyword-only and excluded from
    comparison so that ``SceneStatusMessage(13, 0, 4)`` written by a consumer
    still equals the fully-populated message produced by the decoder.

    Use :attr:`dedupe_key` when you need to tell two byte-identical broadcasts
    apart (the bridge re-broadcasts some keypad events ~200 ms apart).
    """

    room: int
    channel: int
    command: CommandType | int | None = field(default=None, kw_only=True, compare=False)
    data: tuple[int, ...] = field(default=(), kw_only=True, compare=False)
    flags: int | None = field(default=None, kw_only=True, compare=False)
    origin: MessageOrigin = field(default=MessageOrigin.UNKNOWN, kw_only=True, compare=False)
    raw: tuple[int, ...] = field(default=(), kw_only=True, compare=False, repr=False)

    @property
    def command_value(self) -> int | None:
        """The numeric instruction code, whether or not it is a known command."""
        if isinstance(self.command, CommandType):
            return self.command.value
        return self.command

    @property
    def room_channel(self) -> "RoomChannel":
        return RoomChannel(self.room, self.channel)

    @property
    def dedupe_key(self) -> tuple[int, int, int | None, tuple[int, ...]]:
        """Identity used to suppress duplicate broadcasts of the same event."""
        return (self.room, self.channel, self.command_value, self.data)


@dataclass(frozen=True)
class SceneStatusMessage(StatusMessage):
    """A room was put into a scene (SET_SCENE, SC1-SC4 or OFF)."""

    scene: int


@dataclass(frozen=True)
class ChannelStatusMessage(StatusMessage):
    """A channel was driven to an absolute level (SET_LEVEL or LEVEL_SET)."""

    brightness: int


# Message: Client to Bridge
@dataclass
class CommandUDP:
    room: int
    channel: int
    command: CommandType
    data: list[int]
    message_type: MessageType = MessageType.REQUEST


@dataclass
class CommandHTTP:
    room: int
    channel: int

    def as_params(self) -> dict[str, int]:
        raise NotImplementedError()


@dataclass
class CommandSceneHTTP(CommandHTTP):
    scene: int

    def as_params(self) -> dict[str, int]:
        return {
            "room": self.room,
            "ch": self.channel,
            "sc": self.scene,
        }


@dataclass
class CommandLevelHTTP(CommandHTTP):
    level: int

    def as_params(self) -> dict[str, int]:
        return {
            "room": self.room,
            "ch": self.channel,
            "lev": self.level,
        }
