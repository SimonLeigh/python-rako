"""A bridge state model that records *where each value came from*.

The Rako bridge cannot be asked what level a circuit is at.  The official
answer is to combine the scene cache (which room is in which scene) with the
level table (what level each channel takes in each scene) to "produce a good
approximation" (v2.2.2, p.8).  Status broadcasts, by contrast, carry the *true*
level -- an app slider drag broadcasts the real value.

Those two sources disagree, and Phase 0 caught the disagreement in the act: a
slider set room 6 channel 1 to 129 while the scene cache still said "room 6,
scene 2" whose defined level for that channel is 26.  A naive poll would
overwrite the true 129 with the approximate 26 and the lights would appear to
change in the UI without changing in the room.

So every channel value here carries a :class:`StateSource`, and
:meth:`BridgeStateSnapshot.reconcile` uses it to decide what a cache read is
allowed to overwrite.  Snapshots are immutable: :meth:`~BridgeStateSnapshot.apply`
returns a new snapshot, which makes the update path easy to test and safe to
hand to a UI thread.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING

from python_rako.helpers import convert_to_brightness
from python_rako.model import (
    ChannelStatusMessage,
    LevelCache,
    RoomChannel,
    SceneCache,
    SceneStatusMessage,
    StatusMessage,
)
from python_rako.protocol import (
    FadeMessage,
    LevelToggleMessage,
    StopFadeMessage,
    StoreMessage,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "BridgeStateSnapshot",
    "ChannelState",
    "RoomState",
    "StateSource",
]


class StateSource(Enum):
    """Where a channel's level came from, best evidence first."""

    #: A true level broadcast by the bridge (LEVEL_SET / SET_LEVEL / 0x33).
    LEVEL_BROADCAST = "level_broadcast"
    #: The echo of a command we sent -- equally true, but attributable to us.
    COMMAND_ECHO = "command_echo"
    #: Derived from the room's scene via the level table. An approximation.
    SCENE_DERIVED = "scene_derived"
    #: A fade ran and no level was broadcast when it stopped, so the true level
    #: is unknowable until the next broadcast.
    UNKNOWN_AFTER_FADE = "unknown_after_fade"
    #: Restored from a consumer's own persistence across a restart.
    RESTORED = "restored"


#: Sources that carry a level the bridge actually reported. A cache read must
#: never overwrite one of these while the room's scene is unchanged.
TRUE_LEVEL_SOURCES = frozenset({StateSource.LEVEL_BROADCAST, StateSource.COMMAND_ECHO})


@dataclass(frozen=True)
class ChannelState:
    """One channel's level and the provenance of that level."""

    level: int | None
    source: StateSource
    updated_at: float

    @property
    def is_known(self) -> bool:
        """Whether the level is actually known (as opposed to unknown-after-fade)."""
        return self.level is not None

    @property
    def is_estimated(self) -> bool:
        """Whether the level is an approximation rather than a reported value."""
        return self.source not in TRUE_LEVEL_SOURCES


@dataclass(frozen=True)
class RoomState:
    """A room's current scene.

    ``scene`` is ``None`` when the scene is genuinely unknown -- notably for
    rooms the bridge has dropped from its scene cache, which it does by design
    whenever a fade button is used.  ``None`` must never be treated as "off".
    """

    scene: int | None
    updated_at: float


@dataclass(frozen=True)
class BridgeStateSnapshot:
    """Immutable view of every room and channel the bridge has told us about."""

    rooms: Mapping[int, RoomState] = field(default_factory=dict)
    channels: Mapping[RoomChannel, ChannelState] = field(default_factory=dict)
    level_table: LevelCache = field(default_factory=LevelCache)
    #: Set when a STORE broadcast says a scene definition changed, so the
    #: scene->level table needs re-reading before it is trusted again.
    level_table_stale: bool = False
    updated_at: float = 0.0

    # -- reading -----------------------------------------------------------

    def room_scene(self, room: int) -> int | None:
        state = self.rooms.get(room)
        return state.scene if state else None

    def channel_state(self, room: int, channel: int) -> ChannelState | None:
        return self.channels.get(RoomChannel(room, channel))

    def channel_level(self, room: int, channel: int) -> int | None:
        state = self.channels.get(RoomChannel(room, channel))
        return state.level if state else None

    def room_channels(self, room: int) -> Iterable[RoomChannel]:
        return [rc for rc in self.channels if rc.room_id == room]

    # -- construction ------------------------------------------------------

    @classmethod
    def from_caches(
        cls,
        scene_cache: SceneCache,
        level_cache: LevelCache,
        *,
        now: float | None = None,
    ) -> BridgeStateSnapshot:
        """Build a snapshot from a scene-cache read plus the level table.

        Rooms in the level table but *absent* from the scene cache get
        ``scene=None`` and channels with ``level=None``.  This is deliberate:
        the bridge deletes a room from the scene cache as soon as a fade button
        is used on it, so absence means "unknown", never "off".  Reading it as
        off is what makes fade-controlled rooms show as off at startup.
        """
        now = time.time() if now is None else now
        rooms: dict[int, RoomState] = {}
        channels: dict[RoomChannel, ChannelState] = {}

        for room, scene in scene_cache.items():
            rooms[room] = RoomState(scene, now)
            channels.update(_derive_room_channels(level_cache, room, scene, now))

        for room_channel in level_cache:
            if room_channel.room_id in rooms:
                continue
            rooms.setdefault(room_channel.room_id, RoomState(None, now))
            channels[room_channel] = ChannelState(None, StateSource.UNKNOWN_AFTER_FADE, now)
            channels.setdefault(
                RoomChannel(room_channel.room_id, 0),
                ChannelState(None, StateSource.UNKNOWN_AFTER_FADE, now),
            )

        return cls(rooms=rooms, channels=channels, level_table=level_cache, updated_at=now)

    def with_restored(
        self, room: int, channel: int, level: int | None, *, now: float | None = None
    ) -> BridgeStateSnapshot:
        """Seed a channel from a consumer's own persisted state.

        Intended for start-up, so a fade-controlled room shows its last known
        level (flagged as estimated) instead of an unhelpful blank.
        """
        now = time.time() if now is None else now
        channels = dict(self.channels)
        channels[RoomChannel(room, channel)] = ChannelState(level, StateSource.RESTORED, now)
        return replace(self, channels=channels, updated_at=now)

    # -- the push path -----------------------------------------------------

    def apply(
        self,
        message: StatusMessage,
        *,
        now: float | None = None,
        source: StateSource | None = None,
    ) -> BridgeStateSnapshot:
        """Return the snapshot that results from ``message``.

        ``source`` overrides the provenance recorded for any level this message
        sets; pass :attr:`StateSource.COMMAND_ECHO` when the message is the
        verified echo of a command you sent.
        """
        now = time.time() if now is None else now
        rooms = dict(self.rooms)
        channels = dict(self.channels)
        level_table_stale = self.level_table_stale

        if isinstance(message, SceneStatusMessage):
            rooms[message.room] = RoomState(message.scene, now)
            derived = _derive_room_channels(
                self.level_table,
                message.room,
                message.scene,
                now,
                source=source or StateSource.SCENE_DERIVED,
            )
            if message.channel:
                # A scene message addressed to a single channel only speaks for
                # that channel.
                key = RoomChannel(message.room, message.channel)
                if key in derived:
                    channels[key] = derived[key]
            else:
                channels.update(derived)

        elif isinstance(message, ChannelStatusMessage | LevelToggleMessage):
            level = (
                message.effective_level
                if isinstance(message, LevelToggleMessage)
                else message.brightness
            )
            state = ChannelState(level, source or StateSource.LEVEL_BROADCAST, now)
            for key in self._targets(message.room, message.channel):
                channels[key] = state

        elif isinstance(message, FadeMessage):
            unknown = ChannelState(None, StateSource.UNKNOWN_AFTER_FADE, now)
            for key in self._targets(message.room, message.channel):
                channels[key] = unknown
            # The bridge drops a faded room from its scene cache, so our idea of
            # the room's scene is no longer meaningful either.
            rooms[message.room] = RoomState(None, now)

        elif isinstance(message, StopFadeMessage):
            # No level is broadcast when a fade stops, so the channel stays
            # unknown until something else reports it.
            unknown = ChannelState(None, StateSource.UNKNOWN_AFTER_FADE, now)
            for key in self._targets(message.room, message.channel):
                channels[key] = unknown

        elif isinstance(message, StoreMessage):
            # A keypad rewrote a scene definition; the level table is stale.
            level_table_stale = True

        else:
            # IDENT, CUSTOM_232, HOLIDAY, unknown instructions: interesting as
            # events, but they carry no level information.
            _LOGGER.debug("No state change for %s", message)
            return self

        return replace(
            self,
            rooms=rooms,
            channels=channels,
            level_table_stale=level_table_stale,
            updated_at=now,
        )

    def _targets(self, room: int, channel: int) -> list[RoomChannel]:
        """The channels a message for ``room``/``channel`` speaks for.

        Channel 0 addresses the whole room, so it fans out to every channel we
        know about in that room as well as the room-level entry itself.
        """
        key = RoomChannel(room, channel)
        if channel != 0:
            return [key]
        targets = {key}
        targets.update(rc for rc in self.channels if rc.room_id == room)
        targets.update(rc for rc in self.level_table if rc.room_id == room)
        return sorted(targets, key=lambda rc: rc.channel_id)

    def with_level_table(
        self, level_table: LevelCache, *, now: float | None = None
    ) -> BridgeStateSnapshot:
        """Replace the scene->level table (e.g. after a STORE broadcast)."""
        now = time.time() if now is None else now
        return replace(self, level_table=level_table, level_table_stale=False, updated_at=now)

    # -- the poll path -----------------------------------------------------

    def reconcile(
        self, fresh: BridgeStateSnapshot, *, now: float | None = None
    ) -> BridgeStateSnapshot:
        """Fold a freshly-read cache snapshot into this one.

        **The rule:** cache-derived values are applied to a room only when the
        cached scene *differs* from the scene we are tracking.  A difference
        means we missed a scene change and the cache is ahead of us.  Agreement
        means the cache is telling us what we already know, and any channel
        level we learned from a broadcast since that scene was selected is
        strictly better information than the scene's defined level.

        Worked example (the case observed in Phase 0)::

            tracked:  room 6 -> scene 2, channel 1 = 129 (LEVEL_BROADCAST)
                      (the level table says scene 2 means channel 1 = 26)
            cache:    room 6 -> scene 2

            scenes agree -> the cache learned nothing new
            result:   room 6 -> scene 2, channel 1 = 129 (LEVEL_BROADCAST)

        Had the cache said scene 3, the scene change would have been one we
        missed, and the room would be rebuilt from the cache instead.

        Rooms absent from ``fresh`` are left exactly as they are: the bridge
        deletes fade-controlled rooms from its scene cache, so absence is not
        evidence of anything.
        """
        now = time.time() if now is None else now
        rooms = dict(self.rooms)
        channels = dict(self.channels)

        for room, fresh_room in fresh.rooms.items():
            tracked = self.rooms.get(room)
            fresh_channels = {
                rc: state for rc, state in fresh.channels.items() if rc.room_id == room
            }

            if tracked is not None and tracked.scene == fresh_room.scene:
                # The cache agrees with us. Only fill in channels we have never
                # heard anything about.
                for room_channel, state in fresh_channels.items():
                    if room_channel not in channels:
                        channels[room_channel] = replace(state, updated_at=now)
                continue

            # The cached scene differs -- we missed a scene change.
            _LOGGER.debug(
                "Reconcile: room %s scene %s -> %s (missed scene change)",
                room,
                None if tracked is None else tracked.scene,
                fresh_room.scene,
            )
            rooms[room] = RoomState(fresh_room.scene, now)
            for room_channel, state in fresh_channels.items():
                channels[room_channel] = replace(state, updated_at=now)

        return replace(
            self,
            rooms=rooms,
            channels=channels,
            level_table=fresh.level_table or self.level_table,
            level_table_stale=False,
            updated_at=now,
        )


def _derive_room_channels(
    level_table: LevelCache,
    room: int,
    scene: int,
    now: float,
    *,
    source: StateSource = StateSource.SCENE_DERIVED,
) -> dict[RoomChannel, ChannelState]:
    """Scene x level-table -> per-channel levels, plus the room-level entry.

    This is the derivation the protocol document describes as "a good
    approximation"; it lives here so no consumer has to reimplement it.
    """
    derived: dict[RoomChannel, ChannelState] = {}
    levels = list(level_table.get_channel_levels(room, scene))
    for channel, level in levels:
        derived[RoomChannel(room, channel)] = ChannelState(level, source, now)

    room_key = RoomChannel(room, 0)
    if room_key not in derived:
        # The room itself is not in the level table; fall back to the scene's
        # nominal brightness so a whole-room light still has a value.
        room_level = max((lvl for _, lvl in levels), default=None)
        if room_level is None:
            room_level = convert_to_brightness(scene)
        derived[room_key] = ChannelState(room_level, source, now)
    return derived
