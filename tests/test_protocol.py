"""Decoder/encoder tests.

Every ``CAPTURE_*`` byte list in this file is a real packet recorded from a
Rako WTC-Bridge during the Phase-0 characterisation described in
``hacs_rako/docs/BRIDGE_BEHAVIOUR.md``, or an example printed in the official
protocol document ``Accessing the Rako Bridge`` v2.2.2.  They are the ground
truth for the decoder.
"""

import pytest

from python_rako.const import (
    MAX_ROOM_ID,
    CommandType,
    FadeDirection,
    MessageOrigin,
    MessageType,
)
from python_rako.exceptions import RakoBridgeError, RakoProtocolError
from python_rako.model import ChannelStatusMessage, SceneStatusMessage
from python_rako.protocol import (
    AckPacket,
    CacheReplyPacket,
    Custom232Message,
    DiscoveryPacket,
    FadeMessage,
    HolidayMessage,
    IdentMessage,
    LevelToggleMessage,
    StopFadeMessage,
    StoreMessage,
    UnknownPacket,
    UnknownStatusMessage,
    calc_crc,
    decode_packet,
    decode_scene_cache_hex,
    decode_status_message,
    encode_command,
    encode_custom_232,
    encode_fade,
    encode_fade_down,
    encode_fade_up,
    encode_holiday,
    encode_ident,
    encode_level_set_legacy,
    encode_off,
    encode_set_level,
    encode_set_scene,
    encode_stop_fade,
    encode_store,
    validate_crc,
)

# --- Captures from the official protocol document -------------------------
# v2.2.2 p.12: "Room 5, Channel 0, Scene 4"
DOC_SET_SCENE_R5 = [0x53, 0x0A, 0x00, 0x05, 0x00, 0x31, 0x01, 0x04, 0, 0, 0, 0xC5]
# v2.2.2 p.12: "Room 100, Channel 0, Scene 3"
DOC_SET_SCENE_R100 = [0x53, 0x0A, 0x00, 0x64, 0x00, 0x31, 0x01, 0x03, 0, 0, 0, 0x67]

# --- Captures from BRIDGE_BEHAVIOUR.md ------------------------------------
# HA experiment: room 7 ch 2 SET_LEVEL 0 (the "off" echo)
CAPTURE_SET_LEVEL_OFF = [83, 7, 0, 7, 2, 52, 1, 0, 194]
# HA experiment: room 7 ch 2 SET_LEVEL 255 (the "on" echo)
CAPTURE_SET_LEVEL_ON = [83, 7, 0, 7, 2, 52, 1, 255, 195]
# Keypad experiment: room 9 fade up at the default rate
CAPTURE_FADE_UP = [83, 10, 0, 9, 0, 50, 128, 0, 0, 0, 0, 69]
# Keypad experiment: undocumented 0x33, room 158 -> lights ON
CAPTURE_LEVEL_TOGGLE_ON = [83, 8, 0, 158, 0, 51, 128, 255, 1, 175]
# ... and the follow-up press that turned them OFF (checksum recomputed: the
# log records the decoded payload rather than the raw bytes for this one)
CAPTURE_LEVEL_TOGGLE_OFF = [83, 8, 0, 158, 0, 51, 128, 255, 0, 176]
# App experiment: room 6 scene 2 via legacy SC2
CAPTURE_SC2_LEGACY = [83, 5, 0, 6, 0, 4, 246]
# App experiment: room 6 ch 1 slider drag -> true level 129 via legacy LEVEL_SET
CAPTURE_LEVEL_SET_LEGACY = [83, 7, 0, 6, 1, 12, 129, 129, 235]
# Keypad experiment: fade button release
CAPTURE_STOP = [83, 5, 0, 9, 0, 15, 232]
# Keypad experiment: "off" press on the room 9 keypad -> scene 0
CAPTURE_SET_SCENE_OFF = [83, 10, 0, 9, 0, 49, 1, 0, 0, 0, 0, 197]
# Overnight soak: PIR-originated scene 1 in room 145, flags=9 (sensor origin)
CAPTURE_SENSOR_SET_SCENE = [83, 10, 0, 145, 0, 49, 9, 1, 0, 0, 0, 52]


ALL_CAPTURES = [
    DOC_SET_SCENE_R5,
    DOC_SET_SCENE_R100,
    CAPTURE_SET_LEVEL_OFF,
    CAPTURE_SET_LEVEL_ON,
    CAPTURE_FADE_UP,
    CAPTURE_LEVEL_TOGGLE_ON,
    CAPTURE_LEVEL_TOGGLE_OFF,
    CAPTURE_SC2_LEGACY,
    CAPTURE_LEVEL_SET_LEGACY,
    CAPTURE_STOP,
    CAPTURE_SET_SCENE_OFF,
    CAPTURE_SENSOR_SET_SCENE,
]


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("packet", ALL_CAPTURES)
def test_every_capture_has_a_valid_crc(packet):
    """The status checksum excludes the bytes-to-follow byte (v2.2.2 p.12)."""
    assert validate_crc(packet)


@pytest.mark.parametrize("packet", ALL_CAPTURES)
def test_crc_rejects_a_corrupted_capture(packet):
    corrupted = list(packet)
    corrupted[-1] = (corrupted[-1] + 1) % 256
    assert not validate_crc(corrupted)


def test_crc_wraps_to_zero_not_256():
    # 0x80 + 0x80 == 0x100, so the checksum is 0, not 256.
    assert calc_crc([0x80, 0x80]) == 0


def test_request_checksum_includes_bytes_to_follow():
    # v2.2.2 p.14: room 7, channel 0, fade up -> checksum 0xF3
    assert encode_fade_up(7) == [0x52, 0x05, 0x00, 0x07, 0x00, 0x01, 0xF3]
    assert validate_crc(encode_fade_up(7))


def test_validate_crc_on_a_runt_packet():
    assert not validate_crc([0x53, 0x05])


def test_strict_mode_raises_on_bad_crc():
    bad = list(CAPTURE_SET_LEVEL_ON)
    bad[-1] = 0
    with pytest.raises(ValueError, match="CRC"):
        decode_status_message(bad, strict=True)


def test_lenient_mode_decodes_despite_bad_crc(caplog):
    bad = list(CAPTURE_SET_LEVEL_ON)
    bad[-1] = 0
    message = decode_status_message(bad)
    assert message == ChannelStatusMessage(7, 2, 255)
    assert "CRC" in caplog.text


# ---------------------------------------------------------------------------
# Decoding the captures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "packet,expected",
    [
        (DOC_SET_SCENE_R5, SceneStatusMessage(5, 0, 4)),
        (DOC_SET_SCENE_R100, SceneStatusMessage(100, 0, 3)),
        (CAPTURE_SET_LEVEL_OFF, ChannelStatusMessage(7, 2, 0)),
        (CAPTURE_SET_LEVEL_ON, ChannelStatusMessage(7, 2, 255)),
        (CAPTURE_SC2_LEGACY, SceneStatusMessage(6, 0, 2)),
        (CAPTURE_LEVEL_SET_LEGACY, ChannelStatusMessage(6, 1, 129)),
        (CAPTURE_SET_SCENE_OFF, SceneStatusMessage(9, 0, 0)),
        (CAPTURE_SENSOR_SET_SCENE, SceneStatusMessage(145, 0, 1)),
    ],
    ids=[
        "doc set_scene room 5 scene 4",
        "doc set_scene room 100 scene 3",
        "HA off echo",
        "HA on echo",
        "app scene select (legacy SC2)",
        "app slider drag -> true level",
        "keypad off press",
        "PIR occupancy scene",
    ],
)
def test_documented_meaning_of_each_capture(packet, expected):
    assert decode_status_message(packet) == expected


def test_fade_capture():
    message = decode_status_message(CAPTURE_FADE_UP)
    assert isinstance(message, FadeMessage)
    assert (message.room, message.channel) == (9, 0)
    assert message.direction is FadeDirection.UP
    assert message.use_default_rate is True
    assert message.data == (128, 0, 0, 0, 0)


def test_fade_down_direction_comes_from_flags_bit0():
    packet = encode_command(9, 0, CommandType.FADE, [0x81, 0], message_type=MessageType.STATUS)
    message = decode_status_message(packet)
    assert isinstance(message, FadeMessage)
    assert message.direction is FadeDirection.DOWN
    assert message.use_default_rate is True


def test_stop_capture():
    message = decode_status_message(CAPTURE_STOP)
    assert isinstance(message, StopFadeMessage)
    assert (message.room, message.channel) == (9, 0)


def test_undocumented_0x33_capture():
    """The empirically-derived ``[flags, level, on_off]`` reading."""
    on = decode_status_message(CAPTURE_LEVEL_TOGGLE_ON)
    off = decode_status_message(CAPTURE_LEVEL_TOGGLE_OFF)
    assert isinstance(on, LevelToggleMessage)
    assert isinstance(off, LevelToggleMessage)
    assert on.room == off.room == 158
    assert on.level == off.level == 255
    assert on.is_on is True
    assert off.is_on is False
    assert on.effective_level == 255
    assert off.effective_level == 0
    assert on.command is CommandType.LEVEL_TOGGLE


def test_legacy_fade_up_and_down_instructions():
    up = decode_status_message(
        encode_command(4, 0, CommandType.FADE_UP, message_type=MessageType.STATUS)
    )
    down = decode_status_message(
        encode_command(4, 0, CommandType.FADE_DOWN, message_type=MessageType.STATUS)
    )
    assert isinstance(up, FadeMessage) and up.direction is FadeDirection.UP
    assert isinstance(down, FadeMessage) and down.direction is FadeDirection.DOWN


def test_store_ident_custom232_and_holiday():
    store = decode_status_message(
        encode_command(4, 0, CommandType.STORE, message_type=MessageType.STATUS)
    )
    ident = decode_status_message(
        encode_command(4, 1, CommandType.IDENT, message_type=MessageType.STATUS)
    )
    custom = decode_status_message(
        encode_command(4, 0, CommandType.CUSTOM_232, [0, 7], message_type=MessageType.STATUS)
    )
    holiday = decode_status_message(
        encode_command(4, 0, CommandType.HOLIDAY, [2], message_type=MessageType.STATUS)
    )
    assert isinstance(store, StoreMessage)
    assert isinstance(ident, IdentMessage)
    assert isinstance(custom, Custom232Message) and custom.string_id == 7
    assert isinstance(holiday, HolidayMessage) and holiday.mode == 2


def test_ten_bit_room_numbers_round_trip():
    packet = encode_command(
        1019, 3, CommandType.SET_LEVEL, [1, 64], message_type=MessageType.STATUS
    )
    assert packet[2] == 0x03  # room high bits
    message = decode_status_message(packet)
    assert message == ChannelStatusMessage(1019, 3, 64)


# ---------------------------------------------------------------------------
# Origin
# ---------------------------------------------------------------------------


def test_sensor_origin_from_flags_bit3():
    """Rako PIRs send flags=9; keypads/app/HA send flags=1 (soak fact 17)."""
    sensor = decode_status_message(CAPTURE_SENSOR_SET_SCENE)
    keypad = decode_status_message(CAPTURE_SET_SCENE_OFF)
    assert sensor.origin is MessageOrigin.SENSOR
    assert sensor.flags == 9
    assert keypad.origin is MessageOrigin.CONTROL
    assert keypad.flags == 1


def test_messages_without_a_flags_byte_have_unknown_origin():
    assert decode_status_message(CAPTURE_SC2_LEGACY).origin is MessageOrigin.UNKNOWN
    assert decode_status_message(CAPTURE_STOP).origin is MessageOrigin.UNKNOWN
    assert decode_status_message(CAPTURE_LEVEL_SET_LEGACY).origin is MessageOrigin.UNKNOWN


# ---------------------------------------------------------------------------
# Unknown / malformed handling
# ---------------------------------------------------------------------------


def test_unknown_instruction_is_delivered_not_dropped():
    packet = encode_command(12, 1, 0x7E, [1, 2, 3], message_type=MessageType.STATUS)
    message = decode_status_message(packet)
    assert isinstance(message, UnknownStatusMessage)
    assert message.room == 12
    assert message.channel == 1
    assert message.command == 0x7E
    assert message.command_value == 0x7E
    assert message.data == (1, 2, 3)


def test_known_instruction_with_a_truncated_payload_is_not_dropped():
    # SET_SCENE with only a flags byte -- no scene number to read.
    packet = encode_command(12, 0, CommandType.SET_SCENE, [1], message_type=MessageType.STATUS)
    assert isinstance(decode_status_message(packet), UnknownStatusMessage)


def test_declared_length_longer_than_the_packet_is_clamped():
    packet = list(CAPTURE_SET_LEVEL_ON)
    packet[1] = 40  # lie about the length
    message = decode_status_message(packet)
    assert isinstance(message, ChannelStatusMessage)
    assert message.brightness == 255


def test_runt_status_message_raises():
    with pytest.raises(ValueError, match="too short"):
        decode_status_message([83, 1, 0])


# ---------------------------------------------------------------------------
# Packet classification
# ---------------------------------------------------------------------------


def test_classify_discovery_ping():
    assert isinstance(decode_packet(b"D"), DiscoveryPacket)


def test_classify_cache_replies():
    scene = decode_packet([67, 3, 12, 28, 213])
    level = decode_packet([88, 255])
    assert isinstance(scene, CacheReplyPacket)
    assert scene.cache is MessageType.SCENE_CACHE
    assert isinstance(level, CacheReplyPacket)
    assert level.cache is MessageType.LEVEL_CACHE


def test_classify_acks():
    ok = decode_packet(b"AOK")
    err = decode_packet(b"AERROR")
    assert isinstance(ok, AckPacket) and ok.ok is True
    assert isinstance(err, AckPacket) and err.ok is False


def test_classify_unknown_and_empty():
    assert isinstance(decode_packet([1, 2, 3, 4]), UnknownPacket)
    assert isinstance(decode_packet(b""), UnknownPacket)


def test_malformed_status_packet_is_classified_not_raised():
    assert isinstance(decode_packet([83, 1, 0]), UnknownPacket)


def test_decode_packet_returns_status_messages():
    assert decode_packet(bytes(CAPTURE_SET_LEVEL_ON)) == ChannelStatusMessage(7, 2, 255)


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def test_doc_example_room_7_scene_5():
    # v2.2.2 p.15
    assert encode_set_scene(7, 5) == [0x52, 0x07, 0x00, 0x07, 0x00, 0x31, 0x01, 0x05, 0xBB]


def test_doc_example_room_277_level():
    # v2.2.2 p.15 (legacy LEVEL_SET / "CAN_LEVEL" form)
    assert encode_command(277, 1, CommandType.LEVEL_SET_LEGACY, [0x01, 0xA3]) == [
        0x52,
        0x07,
        0x01,
        0x15,
        0x01,
        0x0C,
        0x01,
        0xA3,
        0x32,
    ]


@pytest.mark.parametrize(
    "byte_list,expected",
    [
        (encode_off(21), SceneStatusMessage(21, 0, 0)),
        (encode_set_scene(17, 2), SceneStatusMessage(17, 0, 2)),
        (encode_set_level(5, 1, 255), ChannelStatusMessage(5, 1, 255)),
        (encode_level_set_legacy(13, 1, 42), ChannelStatusMessage(13, 1, 42)),
    ],
)
def test_round_trip_encode_then_decode(byte_list, expected):
    """A request we build decodes to the message the bridge will echo back."""
    status = [MessageType.STATUS.value, *byte_list[1:-1]]
    status.append(calc_crc(status[2:]))
    assert decode_status_message(status) == expected


@pytest.mark.parametrize(
    "builder,command",
    [
        (lambda: encode_fade_up(9), CommandType.FADE_UP),
        (lambda: encode_fade_down(9), CommandType.FADE_DOWN),
        (lambda: encode_stop_fade(9), CommandType.STOP_FADING),
        (lambda: encode_ident(9, 1), CommandType.IDENT),
        (lambda: encode_store(9), CommandType.STORE),
        (lambda: encode_custom_232(9, 3), CommandType.CUSTOM_232),
        (lambda: encode_holiday(9, 1), CommandType.HOLIDAY),
        (lambda: encode_fade(9, direction=FadeDirection.DOWN), CommandType.FADE),
    ],
)
def test_every_sendable_command_encodes_with_a_valid_checksum(builder, command):
    byte_list = builder()
    assert byte_list[0] == MessageType.REQUEST.value
    assert byte_list[5] == command.value
    assert validate_crc(byte_list)


def test_encode_fade_sets_the_direction_and_rate_flags():
    up = encode_fade(9, direction=FadeDirection.UP)
    down = encode_fade(9, direction=FadeDirection.DOWN)
    manual = encode_fade(9, direction=FadeDirection.UP, use_default_rate=False)
    assert up[6] == 0x80
    assert down[6] == 0x81
    assert manual[6] == 0x00


@pytest.mark.parametrize(
    "kwargs",
    [
        {"room": 1024, "channel": 0, "command": CommandType.OFF},
        {"room": -1, "channel": 0, "command": CommandType.OFF},
        {"room": 1, "channel": 256, "command": CommandType.OFF},
        {"room": 1, "channel": 0, "command": CommandType.OFF, "data": [0] * 8},
        {"room": 1, "channel": 0, "command": CommandType.OFF, "data": [300]},
    ],
)
def test_encode_command_rejects_out_of_range_input(kwargs):
    with pytest.raises(ValueError):
        encode_command(**kwargs)


def test_encode_set_level_rejects_a_bad_level():
    with pytest.raises(ValueError):
        encode_set_level(1, 0, 256)
    with pytest.raises(ValueError):
        encode_level_set_legacy(1, 0, -1)
    with pytest.raises(ValueError):
        encode_holiday(1, 9)


# ---------------------------------------------------------------------------
# Message identity
# ---------------------------------------------------------------------------


def test_dedupe_key_distinguishes_the_bridges_own_repeats():
    """The bridge re-broadcasts some keypad events ~200 ms apart (soak fact 18)."""
    first = decode_status_message(CAPTURE_LEVEL_TOGGLE_ON)
    repeat = decode_status_message(CAPTURE_LEVEL_TOGGLE_ON)
    other = decode_status_message(CAPTURE_LEVEL_TOGGLE_OFF)
    assert first.dedupe_key == repeat.dedupe_key
    assert first.dedupe_key != other.dedupe_key


def test_provenance_fields_do_not_affect_equality():
    """So consumer-written literals still compare equal to decoded messages."""
    decoded = decode_status_message(CAPTURE_SENSOR_SET_SCENE)
    assert decoded == SceneStatusMessage(145, 0, 1)
    assert decoded.origin is MessageOrigin.SENSOR


def test_room_channel_helper():
    message = decode_status_message(CAPTURE_LEVEL_SET_LEGACY)
    assert message.room_channel.room_id == 6
    assert message.room_channel.channel_id == 1


# ---------------------------------------------------------------------------
# scenes.htm
# ---------------------------------------------------------------------------


def test_scenes_htm_matches_the_udp_cache_layout():
    # v2.2.2 p.9: 0x0C1C is room 28 scene 3, 0x040C is room 12 scene 1.
    assert decode_scene_cache_hex("0C1C040C") == {28: 3, 12: 1}


def test_scenes_htm_handles_whitespace_markup_and_case():
    assert decode_scene_cache_hex("\n0c1c 040C\r\n") == {28: 3, 12: 1}


def test_scenes_htm_supports_ten_bit_rooms():
    # room 1019 in scene 5 -> (5 << 10) | 1019
    word = (5 << 10) | 1019
    assert decode_scene_cache_hex(f"{word:04X}") == {1019: 5}


@pytest.mark.parametrize("text", ["", "FFFF", "0000", "AB"])
def test_scenes_htm_empty_and_filler_values(text):
    assert decode_scene_cache_hex(text) == {}


# ---------------------------------------------------------------------------
# Encoding errors
# ---------------------------------------------------------------------------


def test_out_of_range_input_raises_a_rako_error_that_is_still_a_value_error():
    """Rooms are 10-bit; older versions silently sent unroutable 16-bit frames."""
    with pytest.raises(RakoProtocolError, match="10-bit"):
        encode_command(1024, 0, CommandType.OFF)
    assert issubclass(RakoProtocolError, RakoBridgeError)
    assert issubclass(RakoProtocolError, ValueError)


@pytest.mark.parametrize(
    "call",
    [
        lambda: encode_command(1, 256, CommandType.OFF),
        lambda: encode_command(1, 0, CommandType.OFF, [0] * 8),
        lambda: encode_command(1, 0, CommandType.OFF, [300]),
        lambda: encode_set_level(1, 0, 256),
        lambda: encode_level_set_legacy(1, 0, -1),
        lambda: encode_holiday(1, 9),
    ],
)
def test_every_encoder_range_check_raises_the_same_type(call):
    with pytest.raises(RakoProtocolError):
        call()


def test_the_highest_addressable_room_still_encodes():
    assert MAX_ROOM_ID == 1023
    assert validate_crc(encode_command(MAX_ROOM_ID, 0, CommandType.OFF))
