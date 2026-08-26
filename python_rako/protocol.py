"""Wire protocol for the Rako bridge's UDP interface (port 9761).

This module owns *all* framing: it turns raw datagrams into typed, frozen
messages and turns command requests back into bytes.  It has no I/O and no
state, so it is cheap to unit-test against the packet captures recorded in
``hacs_rako/docs/BRIDGE_BEHAVIOUR.md``.

Packet shapes
-------------
Both command requests and status broadcasts share a frame::

    <type> <bytes_to_follow> <room_hi> <room_lo> <channel> <command> <data...> <crc>

``bytes_to_follow`` counts everything after itself, i.e. ``5 + len(data)``.
Requests start with ``'R'`` (0x52) and status broadcasts with ``'S'`` (0x53).
The *only* difference between the two is the checksum domain: a request's
checksum covers the ``bytes_to_follow`` byte, a status message's does not
(``Accessing the Rako Bridge`` v2.2.2, p.12).

Room numbers are 10 bits wide and split across two bytes, so rooms above 255
are addressable (``room_hi`` only ever carries the two high bits in practice).

Robustness
----------
Decoding never raises for well-formed-but-unrecognised traffic.  An unknown
instruction code becomes an :class:`UnknownStatusMessage` carrying the raw
payload, and a non-status datagram becomes one of the :class:`NonStatusPacket`
subclasses.  Nothing the bridge (or a phone on the LAN) can send is silently
dropped, because a dropped message is a state-divergence bug waiting to happen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from python_rako.const import (
    COMMAND_ERROR_RESPONSE,
    COMMAND_SUCCESS_RESPONSE,
    FLAG_FADE_DEFAULT_RATE,
    FLAG_FADE_DOWN,
    FLAG_SENSOR_ORIGIN,
    FLAG_USE_DEFAULT_FADE_RATE,
    SCENE_COMMAND_TO_NUMBER,
    STATUS_HEADER_LENGTH,
    CommandType,
    FadeDirection,
    MessageOrigin,
    MessageType,
)
from python_rako.model import (
    ChannelStatusMessage,
    SceneCache,
    SceneStatusMessage,
    StatusMessage,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "AckPacket",
    "CacheReplyPacket",
    "ChannelStatusMessage",
    "Custom232Message",
    "DiscoveryPacket",
    "FadeMessage",
    "HolidayMessage",
    "IdentMessage",
    "LevelToggleMessage",
    "NonStatusPacket",
    "SceneStatusMessage",
    "StatusMessage",
    "StopFadeMessage",
    "StoreMessage",
    "UnknownPacket",
    "UnknownStatusMessage",
    "calc_crc",
    "decode_packet",
    "decode_scene_cache_hex",
    "decode_status_message",
    "encode_command",
    "encode_fade",
    "encode_fade_down",
    "encode_fade_up",
    "encode_ident",
    "encode_off",
    "encode_set_level",
    "encode_set_scene",
    "encode_stop_fade",
    "encode_store",
    "validate_crc",
]


# ---------------------------------------------------------------------------
# Status message types
# ---------------------------------------------------------------------------
#
# ``SceneStatusMessage`` and ``ChannelStatusMessage`` live in ``model`` for
# backwards compatibility and are re-exported here; every other message type is
# defined below.


@dataclass(frozen=True)
class FadeMessage(StatusMessage):
    """A fade is running on the circuit (FADE 0x32, FADE_UP 0x01, FADE_DOWN 0x02).

    Emitted when a keypad fade button is *pressed*; the matching
    :class:`StopFadeMessage` arrives on release.  **No level is broadcast when
    the fade stops**, so the channel's true level is unknowable afterwards
    (``BRIDGE_BEHAVIOUR.md`` facts 1 and 3).
    """

    direction: FadeDirection
    use_default_rate: bool = False


@dataclass(frozen=True)
class StopFadeMessage(StatusMessage):
    """A running fade was stopped (STOP 0x0F) -- typically a button release."""


@dataclass(frozen=True)
class StoreMessage(StatusMessage):
    """A keypad finished saving a scene (STORE 0x0D).

    Scene *definitions* have changed, so any cached scene->level table is stale.
    """


@dataclass(frozen=True)
class IdentMessage(StatusMessage):
    """The circuit was asked to pulse / flash its identify pattern (IDENT 0x08)."""


@dataclass(frozen=True)
class Custom232Message(StatusMessage):
    """A custom RS-232 string was triggered (CUSTOM_232 0x2D)."""

    string_id: int


@dataclass(frozen=True)
class HolidayMessage(StatusMessage):
    """Holiday-mode playback/record was changed (HOLIDAY 0x2F).

    ``mode`` is the low two bits of the flags byte: 0 stop playback,
    1 start playback, 2 start record, 3 stop record.
    """

    mode: int


@dataclass(frozen=True)
class LevelToggleMessage(StatusMessage):
    """Undocumented instruction 0x33 -- **empirically derived**.

    This command is absent from the official instruction table (which jumps
    0x32 -> 0x34) and Rako document it only as "not the full extent of
    commands".  It was observed being broadcast by a wall keypad whose scene
    buttons were mapped to another room::

        [83, 8, 0, 158, 0, 51, 128, 255, 1, 175]   # lights came ON
        [83, 8, 0, 158, 0, 51, 128, 255, 0, 173]   # lights went OFF

    The only byte that varied was the third data byte, and it tracked the
    physical on/off result, so the payload is modelled as
    ``[flags, level, on_off]``.  :attr:`level` is therefore the level the
    circuit takes *when on*, and :attr:`is_on` says whether it was actually
    turned on; :attr:`effective_level` combines the two.

    Treat this interpretation as provisional: if a future capture contradicts
    it, only this class and its state handling need to change.
    """

    level: int
    is_on: bool

    @property
    def effective_level(self) -> int:
        """The level the circuit ends up at: :attr:`level` when on, else 0."""
        return self.level if self.is_on else 0


@dataclass(frozen=True)
class UnknownStatusMessage(StatusMessage):
    """A well-framed status broadcast carrying an instruction we do not model.

    Never dropped: consumers still receive room/channel/command/data so they
    can log it, fire an event, or trigger a reconciliation poll.
    """


# ---------------------------------------------------------------------------
# Non-status packets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NonStatusPacket:
    """A datagram on port 9761 that is not a status broadcast."""

    raw: tuple[int, ...]


@dataclass(frozen=True)
class DiscoveryPacket(NonStatusPacket):
    """A bridge-discovery probe (``'D'``).

    Other hosts on the LAN broadcast these -- the Rako phone app does so every
    few seconds.  They are normal traffic, not errors.
    """


@dataclass(frozen=True)
class CacheReplyPacket(NonStatusPacket):
    """A scene- (``'C'``) or level-cache (``'X'``) reply to a ``'Q'`` query."""

    cache: MessageType


@dataclass(frozen=True)
class AckPacket(NonStatusPacket):
    """The bridge's ``"AOK"`` / ``"AERROR"`` reply to a command request.

    Weak: it only confirms the bridge parsed the request, never that the
    circuit acted (v2.2.2 p.2).  Arrives ~750 ms after the command, i.e. well
    *after* the status echo, so it is diagnostics only.
    """

    ok: bool


@dataclass(frozen=True)
class QueryPacket(NonStatusPacket):
    """A cache query (``'Q'``) -- sent by us or another client on the LAN."""


@dataclass(frozen=True)
class CommandRequestPacket(NonStatusPacket):
    """A command request (``'R'``) seen on the wire, e.g. our own or another client's."""


@dataclass(frozen=True)
class UnknownPacket(NonStatusPacket):
    """A datagram we cannot classify at all."""


DecodedPacket = StatusMessage | NonStatusPacket


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------


def calc_crc(values: Iterable[int]) -> int:
    """Rako's one-byte checksum: ``(256 - sum(values)) mod 256``.

    The final ``& 0xFF`` matters: a payload summing to a multiple of 256 has
    checksum 0, not 256.
    """
    return (256 - sum(values) % 256) % 256


def _checksum_domain(byte_list: Sequence[int]) -> Sequence[int]:
    """The bytes a packet's trailing checksum is computed over.

    Status (``'S'``) messages exclude the bytes-to-follow byte; command
    requests (``'R'``) include it.
    """
    if byte_list and byte_list[0] == MessageType.STATUS.value:
        return byte_list[2:-1]
    return byte_list[1:-1]


def validate_crc(byte_list: Sequence[int]) -> bool:
    """Return whether the trailing checksum byte of ``byte_list`` is correct."""
    if len(byte_list) < 3:
        return False
    return calc_crc(_checksum_domain(byte_list)) == byte_list[-1]


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _origin_from_flags(flags: int | None) -> MessageOrigin:
    if flags is None:
        return MessageOrigin.UNKNOWN
    if flags & FLAG_SENSOR_ORIGIN:
        return MessageOrigin.SENSOR
    return MessageOrigin.CONTROL


#: Commands whose first data byte is a flags byte.
_FLAG_COMMANDS = frozenset(
    {
        CommandType.CUSTOM_232,
        CommandType.HOLIDAY,
        CommandType.SET_SCENE,
        CommandType.FADE,
        CommandType.LEVEL_TOGGLE,
        CommandType.SET_LEVEL,
    }
)

#: Legacy scene commands that carry the scene number in the instruction itself.
_LEGACY_SCENE_COMMANDS = frozenset(SCENE_COMMAND_TO_NUMBER)


def decode_status_message(byte_list: Sequence[int], *, strict: bool = False) -> StatusMessage:
    """Decode one ``'S'`` status broadcast into a typed message.

    ``strict=True`` raises :class:`ValueError` on a checksum mismatch; the
    default only logs, because a rejected packet is worse for state tracking
    than a possibly-corrupt one (and no corrupt packet has ever been observed).
    """
    if len(byte_list) < 6:
        raise ValueError(f"status message too short: {list(byte_list)}")

    if not validate_crc(byte_list):
        message = f"status message failed CRC check: {list(byte_list)}"
        if strict:
            raise ValueError(message)
        _LOGGER.warning("%s (accepting anyway)", message)

    declared_data_length = byte_list[1] - STATUS_HEADER_LENGTH
    # Trust the frame, but never read past the checksum byte.
    available = len(byte_list) - 7
    data_length = max(0, min(declared_data_length, available))

    room = (byte_list[2] << 8) | byte_list[3]
    channel = byte_list[4]
    raw_command = byte_list[5]
    data = tuple(byte_list[6 : 6 + data_length])
    raw = tuple(byte_list)

    try:
        command: CommandType | int = CommandType(raw_command)
    except ValueError:
        _LOGGER.debug(
            "Unknown Rako instruction 0x%02X room=%s channel=%s data=%s",
            raw_command,
            room,
            channel,
            list(data),
        )
        return UnknownStatusMessage(room, channel, command=raw_command, data=data, raw=raw)

    flags = data[0] if (command in _FLAG_COMMANDS and data) else None
    common: dict[str, Any] = {
        "command": command,
        "data": data,
        "flags": flags,
        "origin": _origin_from_flags(flags),
        "raw": raw,
    }

    if command in _LEGACY_SCENE_COMMANDS:
        # OFF / SC1-SC4: the scene is encoded in the instruction itself.
        return SceneStatusMessage(room, channel, SCENE_COMMAND_TO_NUMBER[command], **common)

    if command is CommandType.SET_SCENE:
        if len(data) < 2:
            return UnknownStatusMessage(room, channel, **common)
        return SceneStatusMessage(room, channel, data[1], **common)

    if command is CommandType.LEVEL_SET_LEGACY:
        # data == [level, level]; both bytes carry the level.
        if not data:
            return UnknownStatusMessage(room, channel, **common)
        return ChannelStatusMessage(room, channel, data[-1], **common)

    if command is CommandType.SET_LEVEL:
        if len(data) < 2:
            return UnknownStatusMessage(room, channel, **common)
        return ChannelStatusMessage(room, channel, data[1], **common)

    if command is CommandType.LEVEL_TOGGLE:
        if len(data) < 3:
            return UnknownStatusMessage(room, channel, **common)
        return LevelToggleMessage(room, channel, level=data[1], is_on=bool(data[2]), **common)

    if command is CommandType.FADE:
        direction = FadeDirection.DOWN if (flags or 0) & FLAG_FADE_DOWN else FadeDirection.UP
        return FadeMessage(
            room,
            channel,
            direction=direction,
            use_default_rate=bool((flags or 0) & FLAG_FADE_DEFAULT_RATE),
            **common,
        )

    if command in (CommandType.FADE_UP, CommandType.FADE_DOWN):
        direction = FadeDirection.UP if command is CommandType.FADE_UP else FadeDirection.DOWN
        return FadeMessage(room, channel, direction=direction, **common)

    if command is CommandType.STOP_FADING:
        return StopFadeMessage(room, channel, **common)

    if command is CommandType.STORE:
        return StoreMessage(room, channel, **common)

    if command is CommandType.IDENT:
        return IdentMessage(room, channel, **common)

    if command is CommandType.CUSTOM_232:
        return Custom232Message(room, channel, string_id=data[1] if len(data) > 1 else 0, **common)

    if command is CommandType.HOLIDAY:
        return HolidayMessage(room, channel, mode=(flags or 0) & 0x03, **common)

    # Known instruction code with no modelling yet -- still delivered.
    return UnknownStatusMessage(room, channel, **common)


def decode_packet(
    payload: bytes | bytearray | Sequence[int], *, strict: bool = False
) -> DecodedPacket:
    """Classify and decode any datagram received on the Rako UDP port.

    Returns a :class:`StatusMessage` subclass for ``'S'`` broadcasts and a
    :class:`NonStatusPacket` subclass for everything else.  Never raises for
    unrecognised input.
    """
    byte_list = list(payload)
    raw = tuple(byte_list)
    if not byte_list:
        return UnknownPacket(raw)

    first = byte_list[0]

    # "AOK" / "AERROR" are the only textual replies; check them by exact prefix
    # rather than "does this look like ASCII", so a status frame that happens to
    # contain printable bytes can never be misread as an ack.
    if first == ord("A"):
        text = _as_ascii(byte_list)
        if text is not None:
            if text.startswith(COMMAND_SUCCESS_RESPONSE):
                return AckPacket(raw, ok=True)
            if text.startswith(COMMAND_ERROR_RESPONSE):
                return AckPacket(raw, ok=False)

    if first == MessageType.STATUS.value:
        try:
            return decode_status_message(byte_list, strict=strict)
        except ValueError:
            if strict:
                raise
            _LOGGER.warning("Malformed status message: %s", byte_list)
            return UnknownPacket(raw)

    if first == MessageType.DISCOVERY.value and len(byte_list) == 1:
        return DiscoveryPacket(raw)

    if first == MessageType.SCENE_CACHE.value:
        return CacheReplyPacket(raw, cache=MessageType.SCENE_CACHE)

    if first == MessageType.LEVEL_CACHE.value:
        return CacheReplyPacket(raw, cache=MessageType.LEVEL_CACHE)

    if first == MessageType.QUERY.value:
        return QueryPacket(raw)

    if first == MessageType.REQUEST.value:
        return CommandRequestPacket(raw)

    _LOGGER.debug("Unclassifiable Rako datagram: %s", byte_list)
    return UnknownPacket(raw)


def _as_ascii(byte_list: Sequence[int]) -> str | None:
    """Decode as text if every byte is printable ASCII, else ``None``."""
    if not all(0x20 <= b < 0x7F or b in (0x0A, 0x0D) for b in byte_list):
        return None
    return bytes(byte_list).decode("ascii", errors="replace").strip()


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def decode_scene_cache_hex(text: str) -> SceneCache:
    """Parse the bridge's ``scenes.htm`` body into a :class:`SceneCache`.

    The page returns the live scene cache as a hex string, two bytes per room,
    each 16-bit word being ``scene << 10 | room`` -- the same record layout as
    the UDP ``'C'`` reply, which is why a 10-bit room number fits.  Words for
    room 0 and the all-ones filler are skipped.

    This is the reconciliation read of choice: it costs no UDP socket, so
    polling can never contend with the status listener.
    """
    digits = "".join(c for c in text if c in "0123456789abcdefABCDEF")
    scene_cache = SceneCache()
    for index in range(0, len(digits) - 3, 4):
        word = int(digits[index : index + 4], 16)
        if word in (0x0000, 0xFFFF):
            continue
        room = word & 0x03FF
        scene = word >> 10
        if room:
            scene_cache[room] = scene
    return scene_cache


def encode_command(
    room: int,
    channel: int,
    command: CommandType | int,
    data: Sequence[int] = (),
    *,
    message_type: MessageType = MessageType.REQUEST,
) -> list[int]:
    """Build the byte list for a Rako command request.

    ``room`` is 10-bit; it is split across the two room bytes.  The checksum
    covers the bytes-to-follow byte for requests and omits it for status
    frames, matching :func:`validate_crc`.
    """
    if not 0 <= room <= 0x3FF:
        raise ValueError(f"room must be a 10-bit value, got {room}")
    if not 0 <= channel <= 0xFF:
        raise ValueError(f"channel must be a byte, got {channel}")
    data = list(data)
    if len(data) > 7:
        raise ValueError(f"at most 7 data bytes are allowed, got {len(data)}")
    if any(not 0 <= b <= 0xFF for b in data):
        raise ValueError(f"data bytes must be 0-255, got {data}")

    command_value = command.value if isinstance(command, CommandType) else command

    body = [
        STATUS_HEADER_LENGTH + len(data),  # bytes to follow
        (room >> 8) & 0xFF,  # room high
        room & 0xFF,  # room low
        channel,
        command_value,
        *data,
    ]
    checksum_domain = body[1:] if message_type is MessageType.STATUS else body
    return [message_type.value, *body, calc_crc(checksum_domain)]


def encode_off(room: int, channel: int = 0) -> list[int]:
    """Turn a room/channel off (OFF 0x00)."""
    return encode_command(room, channel, CommandType.OFF)


def encode_set_scene(
    room: int, scene: int, channel: int = 0, *, use_default_rate: bool = True
) -> list[int]:
    """Select a scene for a room (SET_SCENE 0x31)."""
    flags = FLAG_USE_DEFAULT_FADE_RATE if use_default_rate else 0
    return encode_command(room, channel, CommandType.SET_SCENE, [flags, scene])


def encode_set_level(
    room: int, channel: int, level: int, *, use_default_rate: bool = True
) -> list[int]:
    """Drive a channel to an absolute level (SET_LEVEL 0x34)."""
    if not 0 <= level <= 255:
        raise ValueError(f"level must be 0-255, got {level}")
    flags = FLAG_USE_DEFAULT_FADE_RATE if use_default_rate else 0
    return encode_command(room, channel, CommandType.SET_LEVEL, [flags, level])


def encode_level_set_legacy(room: int, channel: int, level: int) -> list[int]:
    """Drive a channel to an absolute level using the legacy LEVEL_SET (0x0C).

    Both data bytes must carry the level.
    """
    if not 0 <= level <= 255:
        raise ValueError(f"level must be 0-255, got {level}")
    return encode_command(room, channel, CommandType.LEVEL_SET_LEGACY, [level, level])


def encode_fade(
    room: int,
    channel: int = 0,
    *,
    direction: FadeDirection = FadeDirection.UP,
    use_default_rate: bool = True,
    level: int = 0,
) -> list[int]:
    """Start a fade in ``direction`` (FADE 0x32).

    Must be terminated with :func:`encode_stop_fade`, exactly as a keypad does
    on button release.
    """
    flags = FLAG_FADE_DOWN if direction is FadeDirection.DOWN else 0
    if use_default_rate:
        flags |= FLAG_FADE_DEFAULT_RATE
    return encode_command(room, channel, CommandType.FADE, [flags, level])


def encode_fade_up(room: int, channel: int = 0) -> list[int]:
    """Start fading up using the legacy FADE_UP instruction (0x01)."""
    return encode_command(room, channel, CommandType.FADE_UP)


def encode_fade_down(room: int, channel: int = 0) -> list[int]:
    """Start fading down using the legacy FADE_DOWN instruction (0x02)."""
    return encode_command(room, channel, CommandType.FADE_DOWN)


def encode_stop_fade(room: int, channel: int = 0) -> list[int]:
    """Stop a running fade (STOP 0x0F)."""
    return encode_command(room, channel, CommandType.STOP_FADING)


def encode_ident(room: int, channel: int = 0) -> list[int]:
    """Make the circuit pulse / its LED flash the ident pattern (IDENT 0x08)."""
    return encode_command(room, channel, CommandType.IDENT)


def encode_store(room: int, channel: int = 0) -> list[int]:
    """Store the current levels as the room's current scene (STORE 0x0D)."""
    return encode_command(room, channel, CommandType.STORE)


def encode_custom_232(room: int, string_id: int, channel: int = 0) -> list[int]:
    """Trigger a custom RS-232 string (CUSTOM_232 0x2D)."""
    return encode_command(room, channel, CommandType.CUSTOM_232, [0, string_id])


def encode_holiday(room: int, mode: int, channel: int = 0) -> list[int]:
    """Control holiday mode (HOLIDAY 0x2F); ``mode`` is 0-3, see :class:`HolidayMessage`."""
    if not 0 <= mode <= 3:
        raise ValueError(f"holiday mode must be 0-3, got {mode}")
    return encode_command(room, channel, CommandType.HOLIDAY, [mode])
