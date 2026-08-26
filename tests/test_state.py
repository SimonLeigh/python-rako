"""State-snapshot tests.

The `test_replay_*` tests push the exact broadcast sequences recorded in
``hacs_rako/docs/BRIDGE_BEHAVIOUR.md`` through ``apply`` and assert the state
that results, so the model is pinned to observed bridge behaviour rather than
to an interpretation of it.
"""

import pytest

from python_rako.const import CommandType, FadeDirection
from python_rako.model import (
    ChannelStatusMessage,
    LevelCache,
    LevelCacheItem,
    RoomChannel,
    SceneCache,
    SceneStatusMessage,
)
from python_rako.protocol import (
    Custom232Message,
    FadeMessage,
    IdentMessage,
    LevelToggleMessage,
    StopFadeMessage,
    StoreMessage,
    UnknownStatusMessage,
    decode_status_message,
)
from python_rako.state import (
    BridgeStateSnapshot,
    ChannelState,
    RoomState,
    StateSource,
)

# Room 6 has two channels; scene 2 dims channel 1 to 26 (the value the Phase-0
# cache read reported while the true level was 129).
LEVEL_TABLE = LevelCache(
    {
        RoomChannel(6, 1): LevelCacheItem(0x80, 6, 1, {1: 255, 2: 26, 3: 128, 4: 64}),
        RoomChannel(6, 2): LevelCacheItem(0x80, 6, 2, {1: 255, 2: 192, 3: 128, 4: 64}),
        RoomChannel(7, 2): LevelCacheItem(0x80, 7, 2, {1: 255, 2: 192, 3: 128, 4: 64}),
        RoomChannel(9, 1): LevelCacheItem(0x80, 9, 1, {1: 255, 2: 192, 3: 128, 4: 64}),
    }
)


def empty_snapshot() -> BridgeStateSnapshot:
    return BridgeStateSnapshot(level_table=LEVEL_TABLE)


# ---------------------------------------------------------------------------
# Replaying the captured sequences
# ---------------------------------------------------------------------------


def test_replay_app_experiment_room_6():
    """SC2 -> slider drag -> SC1, from the 2026-08-25 app experiment.

    The slider's true level must survive until the next scene selection
    replaces it.
    """
    snapshot = empty_snapshot()

    snapshot = snapshot.apply(SceneStatusMessage(6, 0, 2))
    assert snapshot.room_scene(6) == 2
    assert snapshot.channel_level(6, 1) == 26  # the scene's defined level
    assert snapshot.channel_state(6, 1).source is StateSource.SCENE_DERIVED

    snapshot = snapshot.apply(ChannelStatusMessage(6, 1, 129))
    assert snapshot.channel_level(6, 1) == 129
    assert snapshot.channel_state(6, 1).source is StateSource.LEVEL_BROADCAST
    assert snapshot.room_scene(6) == 2  # a level change does not change the scene
    assert snapshot.channel_level(6, 2) == 192  # untouched by the slider

    snapshot = snapshot.apply(SceneStatusMessage(6, 0, 1))
    assert snapshot.room_scene(6) == 1
    assert snapshot.channel_level(6, 1) == 255  # the scene overrides the slider
    assert snapshot.channel_state(6, 1).source is StateSource.SCENE_DERIVED


def test_replay_ha_experiment_room_7():
    """The off and on echoes observed for an HA-originated command."""
    snapshot = empty_snapshot()

    snapshot = snapshot.apply(
        ChannelStatusMessage(7, 2, 0), source=StateSource.COMMAND_ECHO
    )
    assert snapshot.channel_level(7, 2) == 0
    assert snapshot.channel_state(7, 2).source is StateSource.COMMAND_ECHO
    assert snapshot.channel_state(7, 2).is_estimated is False

    snapshot = snapshot.apply(
        ChannelStatusMessage(7, 2, 255), source=StateSource.COMMAND_ECHO
    )
    assert snapshot.channel_level(7, 2) == 255


def test_replay_keypad_experiment():
    """Fade press/release, an off press, and two 0x33 toggles."""
    snapshot = empty_snapshot()
    # Start from a known state so the fade has something to invalidate.
    snapshot = snapshot.apply(SceneStatusMessage(9, 0, 1))
    assert snapshot.channel_level(9, 1) == 255

    fade = FadeMessage(9, 0, direction=FadeDirection.UP, command=CommandType.FADE)
    snapshot = snapshot.apply(fade)
    assert snapshot.channel_level(9, 1) is None
    assert snapshot.channel_state(9, 1).source is StateSource.UNKNOWN_AFTER_FADE
    # The bridge deletes a faded room from its scene cache; so do we.
    assert snapshot.room_scene(9) is None

    snapshot = snapshot.apply(StopFadeMessage(9, 0, command=CommandType.STOP_FADING))
    # No level is broadcast when a fade stops, so it is still unknown.
    assert snapshot.channel_level(9, 1) is None
    assert snapshot.channel_state(9, 1).source is StateSource.UNKNOWN_AFTER_FADE

    snapshot = snapshot.apply(SceneStatusMessage(9, 0, 0))
    assert snapshot.room_scene(9) == 0
    assert snapshot.channel_level(9, 1) == 0

    on = LevelToggleMessage(
        158, 0, level=255, is_on=True, command=CommandType.LEVEL_TOGGLE
    )
    off = LevelToggleMessage(
        158, 0, level=255, is_on=False, command=CommandType.LEVEL_TOGGLE
    )
    snapshot = snapshot.apply(on)
    assert snapshot.channel_level(158, 0) == 255
    assert snapshot.channel_state(158, 0).source is StateSource.LEVEL_BROADCAST
    snapshot = snapshot.apply(off)
    assert snapshot.channel_level(158, 0) == 0


def test_replay_from_raw_bytes():
    """The same sequence, decoded from the captured packets end to end."""
    packets = [
        [83, 5, 0, 6, 0, 4, 246],  # room 6 scene 2 (legacy SC2)
        [83, 7, 0, 6, 1, 12, 129, 129, 235],  # room 6 ch1 slider -> 129
    ]
    snapshot = empty_snapshot()
    for packet in packets:
        snapshot = snapshot.apply(decode_status_message(packet))
    assert snapshot.room_scene(6) == 2
    assert snapshot.channel_level(6, 1) == 129
    assert snapshot.channel_level(6, 2) == 192


def test_a_sensor_scene_burst_is_applied_like_any_other_scene():
    snapshot = empty_snapshot().apply(
        decode_status_message([83, 10, 0, 145, 0, 49, 9, 1, 0, 0, 0, 52])
    )
    assert snapshot.room_scene(145) == 1


# ---------------------------------------------------------------------------
# apply() rules
# ---------------------------------------------------------------------------


def test_store_marks_the_level_table_stale():
    snapshot = empty_snapshot()
    assert snapshot.level_table_stale is False
    snapshot = snapshot.apply(StoreMessage(6, 0, command=CommandType.STORE))
    assert snapshot.level_table_stale is True
    # ... and refreshing the table clears the flag.
    snapshot = snapshot.with_level_table(LEVEL_TABLE)
    assert snapshot.level_table_stale is False


@pytest.mark.parametrize(
    "message",
    [
        IdentMessage(6, 1, command=CommandType.IDENT),
        Custom232Message(6, 0, string_id=3, command=CommandType.CUSTOM_232),
        UnknownStatusMessage(6, 0, command=0x7E, data=(1, 2)),
    ],
)
def test_messages_without_level_information_leave_the_state_alone(message):
    snapshot = empty_snapshot().apply(SceneStatusMessage(6, 0, 2))
    assert snapshot.apply(message) is snapshot


def test_a_room_level_broadcast_fans_out_to_every_known_channel():
    snapshot = empty_snapshot().apply(ChannelStatusMessage(6, 0, 100))
    assert snapshot.channel_level(6, 0) == 100
    assert snapshot.channel_level(6, 1) == 100
    assert snapshot.channel_level(6, 2) == 100
    assert snapshot.channel_level(7, 2) is None


def test_a_channel_scene_message_only_speaks_for_that_channel():
    snapshot = empty_snapshot().apply(SceneStatusMessage(6, 0, 1))
    snapshot = snapshot.apply(SceneStatusMessage(6, 1, 2))
    assert snapshot.channel_level(6, 1) == 26
    assert snapshot.channel_level(6, 2) == 255  # untouched


def test_a_room_fade_invalidates_every_channel_in_the_room():
    snapshot = empty_snapshot().apply(SceneStatusMessage(6, 0, 1))
    snapshot = snapshot.apply(
        FadeMessage(6, 0, direction=FadeDirection.DOWN, command=CommandType.FADE)
    )
    assert snapshot.channel_level(6, 1) is None
    assert snapshot.channel_level(6, 2) is None
    assert snapshot.channel_state(6, 1).is_known is False


def test_the_room_level_falls_back_to_the_scene_brightness():
    """A room with no level-table entry still gets a usable room-level value."""
    snapshot = BridgeStateSnapshot().apply(SceneStatusMessage(200, 0, 3))
    assert snapshot.channel_level(200, 0) == 128


def test_apply_is_pure():
    original = empty_snapshot()
    updated = original.apply(SceneStatusMessage(6, 0, 2))
    assert original.rooms == {}
    assert updated is not original
    assert updated.room_scene(6) == 2


def test_with_restored_marks_provenance():
    snapshot = empty_snapshot().with_restored(6, 1, 42)
    assert snapshot.channel_level(6, 1) == 42
    assert snapshot.channel_state(6, 1).source is StateSource.RESTORED
    assert snapshot.channel_state(6, 1).is_estimated is True


# ---------------------------------------------------------------------------
# from_caches()
# ---------------------------------------------------------------------------


def test_from_caches_derives_levels_for_cached_rooms():
    snapshot = BridgeStateSnapshot.from_caches(SceneCache({6: 2}), LEVEL_TABLE)
    assert snapshot.room_scene(6) == 2
    assert snapshot.channel_level(6, 1) == 26
    assert snapshot.channel_state(6, 1).source is StateSource.SCENE_DERIVED


def test_rooms_absent_from_the_scene_cache_are_unknown_not_off():
    """The bridge deletes fade-controlled rooms from its cache by design."""
    snapshot = BridgeStateSnapshot.from_caches(SceneCache({6: 2}), LEVEL_TABLE)

    assert snapshot.room_scene(9) is None
    assert 9 in snapshot.rooms
    state = snapshot.channel_state(9, 1)
    assert state is not None
    assert state.level is None
    assert state.is_known is False
    assert state.source is StateSource.UNKNOWN_AFTER_FADE
    # Emphatically not zero:
    assert snapshot.channel_level(9, 1) != 0


def test_from_caches_with_an_empty_cache():
    snapshot = BridgeStateSnapshot.from_caches(SceneCache(), LevelCache())
    assert snapshot.rooms == {}
    assert snapshot.channels == {}


# ---------------------------------------------------------------------------
# reconcile()
# ---------------------------------------------------------------------------


def test_reconcile_keeps_a_true_level_when_the_cached_scene_agrees():
    """The Phase-0 worked example: scene 2 tracked, slider set 129, cache says 2."""
    tracked = (
        empty_snapshot()
        .apply(SceneStatusMessage(6, 0, 2))
        .apply(ChannelStatusMessage(6, 1, 129))
    )
    fresh = BridgeStateSnapshot.from_caches(SceneCache({6: 2}), LEVEL_TABLE)

    reconciled = tracked.reconcile(fresh)

    assert reconciled.room_scene(6) == 2
    assert reconciled.channel_level(6, 1) == 129
    assert reconciled.channel_state(6, 1).source is StateSource.LEVEL_BROADCAST


def test_reconcile_adopts_the_cache_when_the_scene_differs():
    tracked = (
        empty_snapshot()
        .apply(SceneStatusMessage(6, 0, 2))
        .apply(ChannelStatusMessage(6, 1, 129))
    )
    fresh = BridgeStateSnapshot.from_caches(SceneCache({6: 1}), LEVEL_TABLE)

    reconciled = tracked.reconcile(fresh)

    assert reconciled.room_scene(6) == 1
    assert reconciled.channel_level(6, 1) == 255
    assert reconciled.channel_state(6, 1).source is StateSource.SCENE_DERIVED


def test_reconcile_does_not_overwrite_a_command_echo():
    tracked = empty_snapshot().apply(SceneStatusMessage(7, 0, 2)).apply(
        ChannelStatusMessage(7, 2, 255), source=StateSource.COMMAND_ECHO
    )
    fresh = BridgeStateSnapshot.from_caches(SceneCache({7: 2}), LEVEL_TABLE)

    reconciled = tracked.reconcile(fresh)

    assert reconciled.channel_level(7, 2) == 255
    assert reconciled.channel_state(7, 2).source is StateSource.COMMAND_ECHO


def test_reconcile_leaves_rooms_absent_from_the_cache_alone():
    """Absence from the scene cache means fade-controlled, not off."""
    tracked = empty_snapshot().apply(ChannelStatusMessage(9, 1, 200))
    fresh = BridgeStateSnapshot.from_caches(SceneCache({6: 1}), LevelCache())

    reconciled = tracked.reconcile(fresh)

    assert reconciled.channel_level(9, 1) == 200
    assert reconciled.channel_state(9, 1).source is StateSource.LEVEL_BROADCAST


def test_reconcile_adopts_rooms_we_have_never_seen():
    tracked = empty_snapshot()
    fresh = BridgeStateSnapshot.from_caches(SceneCache({6: 3}), LEVEL_TABLE)

    reconciled = tracked.reconcile(fresh)

    assert reconciled.room_scene(6) == 3
    assert reconciled.channel_level(6, 1) == 128


def test_reconcile_fills_in_channels_it_has_never_heard_of():
    """Agreement on the scene still lets the cache introduce new channels."""
    tracked = BridgeStateSnapshot(
        rooms={6: RoomState(2, 0.0)},
        channels={RoomChannel(6, 1): ChannelState(129, StateSource.LEVEL_BROADCAST, 0.0)},
        level_table=LEVEL_TABLE,
    )
    fresh = BridgeStateSnapshot.from_caches(SceneCache({6: 2}), LEVEL_TABLE)

    reconciled = tracked.reconcile(fresh)

    assert reconciled.channel_level(6, 1) == 129
    assert reconciled.channel_level(6, 2) == 192


def test_reconcile_clears_the_stale_flag_and_refreshes_the_table():
    tracked = empty_snapshot().apply(StoreMessage(6, 0, command=CommandType.STORE))
    fresh = BridgeStateSnapshot.from_caches(SceneCache({6: 2}), LEVEL_TABLE)
    reconciled = tracked.reconcile(fresh)
    assert reconciled.level_table_stale is False
    assert reconciled.level_table is LEVEL_TABLE


def test_room_channels_helper():
    snapshot = empty_snapshot().apply(SceneStatusMessage(6, 0, 2))
    channels = sorted(rc.channel_id for rc in snapshot.room_channels(6))
    assert channels == [0, 1, 2]
