from enum import Enum

RAKO_BRIDGE_DEFAULT_PORT = 9761
sentinel = object()

#: Number of header bytes in a status/request payload that are counted by the
#: "bytes to follow" byte but are not command data
#: (room high, room low, channel, command, checksum).
STATUS_HEADER_LENGTH = 5


class MessageType(Enum):
    QUERY = ord("Q")  # 81
    SCENE_CACHE = ord("C")  # 67
    LEVEL_CACHE = ord("X")  # 88
    REQUEST = ord("R")  # 82
    STATUS = ord("S")  # 83
    DISCOVERY = ord("D")  # 68


class DataRecordType(Enum):
    DATA = 4
    EOF = 255


class RequestType(Enum):
    SCENE_CACHE = 1
    LEVEL_CACHE = 32
    SCENE_LEVEL_CACHE = 33


class Flags(Enum):
    USE_DEFAULT_FADE_RATE = 1


class CommandType(Enum):
    """Rako UDP instruction codes.

    Values up to and including ``SET_LEVEL`` come from the official instruction
    table (``Accessing the Rako Bridge`` v2.2.2, p.13).  ``LEVEL_TOGGLE`` (0x33)
    is *undocumented* and was derived empirically -- see
    :class:`python_rako.protocol.LevelToggleMessage`.
    """

    OFF = 0
    FADE_UP = 1
    FADE_DOWN = 2
    SC1_LEGACY = 3
    SC2_LEGACY = 4
    SC3_LEGACY = 5
    SC4_LEGACY = 6
    IDENT = 8
    LEVEL_SET_LEGACY = 12
    STORE = 13
    STOP_FADING = 15
    CUSTOM_232 = 45  # 0x2D
    HOLIDAY = 47  # 0x2F
    SET_SCENE = 49  # 0x31
    FADE = 50  # 0x32
    LEVEL_TOGGLE = 51  # 0x33 -- undocumented, empirically derived
    SET_LEVEL = 52  # 0x34


class FadeDirection(Enum):
    """Direction of a FADE (0x32) / FADE_UP (0x01) / FADE_DOWN (0x02) message."""

    UP = "up"
    DOWN = "down"


class MessageOrigin(Enum):
    """Who caused a status broadcast.

    Derived from bit 3 of the flags byte.  Rako occupancy sensors (PIRs) send
    ``flags=9`` (``0b1001``) where keypads, the Rako app and third-party
    integrations send ``flags=1``; see ``BRIDGE_BEHAVIOUR.md`` fact 17.  The bit
    is undocumented, so treat it as a strong hint rather than a guarantee.
    """

    CONTROL = "control"
    SENSOR = "sensor"
    UNKNOWN = "unknown"


#: Flags byte bit masks.
FLAG_USE_DEFAULT_FADE_RATE = 0x01
#: FADE (0x32) only: bit 0 is the direction (0 = up, 1 = down).
FLAG_FADE_DOWN = 0x01
#: FADE (0x32) only: bit 7 requests the configured default fade rate.
FLAG_FADE_DEFAULT_RATE = 0x80
#: Undocumented: bit 3 marks a message originated by a sensor rather than a
#: keypad / app / third-party control.
FLAG_SENSOR_ORIGIN = 0x08


COMMAND_SUCCESS_RESPONSE = "AOK"
COMMAND_ERROR_RESPONSE = "AERROR"


SCENE_NUMBER_TO_COMMAND = {
    1: CommandType.SC1_LEGACY,
    2: CommandType.SC2_LEGACY,
    3: CommandType.SC3_LEGACY,
    4: CommandType.SC4_LEGACY,
    0: CommandType.OFF,
}
SCENE_COMMAND_TO_NUMBER = {v: k for k, v in SCENE_NUMBER_TO_COMMAND.items()}
