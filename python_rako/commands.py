"""Sending commands to the bridge, verified by the echoed status broadcast.

Neither transport confirms that anything happened.  An HTTP "Success!" only
means the bridge received the request, and the UDP ``"AOK"`` only means it
parsed it (``Accessing the Rako Bridge`` v2.2.2, p.2).  Phase 0 observed the
failure this hides: two commands in one evening produced no light change, no
broadcast and no cache change -- they never took effect -- while the old
library reported success because a UDP receive timeout was treated as an ack.

The bridge *does* broadcast a status message for every change it actually
performs, within 144-306 ms of the command; its own ``"AOK"`` trails at
677-770 ms.  So the echo is both faster and more truthful than the ack, and it
is what this module waits for:

    send -> await matching echo (<=1.5 s) -> on silence resend once
         -> on silence again raise RakoCommandError

The echoed message is returned so the caller updates state from what the bridge
says happened, never from what we asked for.

Echo matching
-------------
The bridge does not always echo the instruction we sent.  A scene selection may
come back as SET_SCENE *or* as a legacy SC1-SC4 (the protocol document
recommends monitoring the legacy forms precisely because they "appear in
feedback"), and both decode to :class:`SceneStatusMessage`, so matching is done
on the decoded *meaning* rather than the instruction byte.

When several commands are in flight, each echo goes to the waiter with the best
match: an exact value match wins over a same-target-different-value match, and
ties are broken oldest-first.  A message tagged
:attr:`~python_rako.const.MessageOrigin.SENSOR` never satisfies a loose match,
so an occupancy sensor firing in the same room cannot be mistaken for our echo.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Any

import asyncio_dgram

from python_rako.const import (
    FLAG_FADE_DOWN,
    SCENE_COMMAND_TO_NUMBER,
    CommandType,
    FadeDirection,
    MessageOrigin,
    MessageType,
)
from python_rako.exceptions import RakoCommandError, RakoConnectionError
from python_rako.model import ChannelStatusMessage, SceneStatusMessage, StatusMessage
from python_rako.protocol import (
    AckPacket,
    FadeMessage,
    StopFadeMessage,
    decode_packet,
    encode_command,
    encode_fade_down,
    encode_fade_up,
    encode_set_level,
    encode_set_scene,
    encode_stop_fade,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "CommandSender",
    "CommandSpec",
    "EchoVerifier",
    "MatchQuality",
    "UdpCommandSender",
    "as_status_frame",
    "execute_command",
    "fade_command",
    "level_command",
    "scene_command",
    "spec_from_frame",
    "stop_command",
]

#: Echo arrives in 144-306 ms; 1.5 s is roughly 5x headroom.
DEFAULT_VERIFY_TIMEOUT = 1.5
#: One resend on silence, then fail. Beyond that we are fighting the network.
DEFAULT_RETRIES = 1
#: The AOK trails the echo by ~500 ms; we read it only for diagnostics.
ACK_READ_TIMEOUT = 2.0

#: Failures that mean "this socket is no longer usable", as opposed to a bug.
#: asyncio_dgram raises its own TransportClosed rather than an OSError.
_SOCKET_ERRORS = (OSError, asyncio_dgram.TransportClosed)


class MatchQuality(IntEnum):
    """How well an observed broadcast matches a command awaiting verification."""

    NONE = 0
    #: Right target and right kind of change, but not the value we asked for
    #: (the bridge may clamp a level to a configured minimum, for instance).
    LOOSE = 1
    #: Right target, right kind, right value.
    EXACT = 2


@dataclass(frozen=True)
class CommandSpec:
    """A command to send, and the echo that will confirm it happened."""

    room: int
    channel: int
    command: CommandType
    data: tuple[int, ...] = ()

    def to_byte_list(self) -> list[int]:
        return encode_command(self.room, self.channel, self.command, self.data)

    @property
    def expects_echo(self) -> bool:
        """Whether this command produces a status broadcast worth waiting for."""
        return self.command in _ECHOING_COMMANDS

    @property
    def expected_scene(self) -> int | None:
        if self.command is CommandType.SET_SCENE:
            return self.data[1] if len(self.data) > 1 else None
        return SCENE_COMMAND_TO_NUMBER.get(self.command)

    @property
    def expected_level(self) -> int | None:
        if self.command is CommandType.SET_LEVEL:
            return self.data[1] if len(self.data) > 1 else None
        if self.command is CommandType.LEVEL_SET_LEGACY:
            return self.data[-1] if self.data else None
        return None

    @property
    def expected_direction(self) -> FadeDirection | None:
        if self.command is CommandType.FADE_UP:
            return FadeDirection.UP
        if self.command is CommandType.FADE_DOWN:
            return FadeDirection.DOWN
        if self.command is CommandType.FADE and self.data:
            # Bit 0 of the flags byte means different things per opcode: on FADE
            # (0x32) it is the direction, while on SET_SCENE/SET_LEVEL the same
            # bit is FLAG_USE_DEFAULT_FADE_RATE. Both happen to be 0x01.
            return FadeDirection.DOWN if self.data[0] & FLAG_FADE_DOWN else FadeDirection.UP
        return None

    def match(self, message: StatusMessage) -> MatchQuality:
        """Score ``message`` as a possible echo of this command."""
        if message.room != self.room or message.channel != self.channel:
            return MatchQuality.NONE

        # A sensor-originated broadcast is somebody else's event, so it may
        # only ever satisfy an exact match.
        loose_allowed = message.origin is not MessageOrigin.SENSOR

        expected_scene = self.expected_scene
        if expected_scene is not None:
            if isinstance(message, SceneStatusMessage):
                if message.scene == expected_scene:
                    return MatchQuality.EXACT
                return MatchQuality.LOOSE if loose_allowed else MatchQuality.NONE
            # OFF is often realised as a level of 0 on the circuit.
            if (
                expected_scene == 0
                and isinstance(message, ChannelStatusMessage)
                and message.brightness == 0
            ):
                return MatchQuality.EXACT
            return MatchQuality.NONE

        expected_level = self.expected_level
        if expected_level is not None:
            if isinstance(message, ChannelStatusMessage):
                if message.brightness == expected_level:
                    return MatchQuality.EXACT
                return MatchQuality.LOOSE if loose_allowed else MatchQuality.NONE
            return MatchQuality.NONE

        expected_direction = self.expected_direction
        if expected_direction is not None:
            if isinstance(message, FadeMessage):
                if message.direction is expected_direction:
                    return MatchQuality.EXACT
                return MatchQuality.LOOSE if loose_allowed else MatchQuality.NONE
            return MatchQuality.NONE

        if self.command is CommandType.STOP_FADING:
            return MatchQuality.EXACT if isinstance(message, StopFadeMessage) else MatchQuality.NONE

        return MatchQuality.EXACT if message.command is self.command else MatchQuality.NONE


#: Commands the bridge is known (or expected) to echo as a status broadcast.
_ECHOING_COMMANDS = frozenset(
    {
        CommandType.OFF,
        CommandType.FADE_UP,
        CommandType.FADE_DOWN,
        CommandType.SC1_LEGACY,
        CommandType.SC2_LEGACY,
        CommandType.SC3_LEGACY,
        CommandType.SC4_LEGACY,
        CommandType.LEVEL_SET_LEGACY,
        CommandType.STOP_FADING,
        CommandType.SET_SCENE,
        CommandType.FADE,
        CommandType.SET_LEVEL,
    }
)


# ---------------------------------------------------------------------------
# Command specs
# ---------------------------------------------------------------------------


def spec_from_frame(byte_list: Sequence[int]) -> CommandSpec:
    """Read a request frame back into a :class:`CommandSpec`.

    The builders below go through :mod:`python_rako.protocol` and then parse the
    result, so the payload layout for every command is defined in exactly one
    place -- the ``encode_*`` function -- and cannot drift from what the encoder
    (and its tests) produce.
    """
    room = (byte_list[2] << 8) | byte_list[3]
    return CommandSpec(
        room=room,
        channel=byte_list[4],
        command=CommandType(byte_list[5]),
        data=tuple(byte_list[6:-1]),
    )


def scene_command(room: int, scene: int, channel: int = 0) -> CommandSpec:
    """Select a scene for a room (SET_SCENE 0x31)."""
    return spec_from_frame(encode_set_scene(room, scene, channel))


def level_command(room: int, channel: int, level: int) -> CommandSpec:
    """Drive a channel to an absolute level (SET_LEVEL 0x34)."""
    return spec_from_frame(encode_set_level(room, channel, level))


def fade_command(
    room: int, channel: int = 0, *, direction: FadeDirection = FadeDirection.UP
) -> CommandSpec:
    """Start a fade the way a keypad does; terminate it with :func:`stop_command`.

    Uses the press/release pair FADE_UP (0x01) / FADE_DOWN (0x02) rather than
    the parameterised FADE (0x32), because that is the form the protocol
    document's worked example uses and the form keypads broadcast -- so the
    echo we wait for looks like the one a keypad produces.
    """
    frame = encode_fade_up(room, channel)
    if direction is FadeDirection.DOWN:
        frame = encode_fade_down(room, channel)
    return spec_from_frame(frame)


def stop_command(room: int, channel: int = 0) -> CommandSpec:
    """Stop a running fade (STOP 0x0F)."""
    return spec_from_frame(encode_stop_fade(room, channel))


# ---------------------------------------------------------------------------
# Echo verification
# ---------------------------------------------------------------------------


@dataclass
class _Waiter:
    spec: CommandSpec
    future: asyncio.Future[StatusMessage]


class EchoVerifier:
    """Routes incoming status broadcasts to the commands awaiting them.

    Attach it to a :class:`~python_rako.listener.StatusListener` once; each
    in-flight command registers a waiter for the duration of its verify window.
    """

    def __init__(self) -> None:
        self._waiters: list[_Waiter] = []
        self._listener: Any | None = None
        self._unsubscribe: Callable[[], None] | None = None

    def attach(self, listener: object) -> None:
        """Subscribe to ``listener``'s messages. Safe to call more than once.

        Subscribes with ``include_duplicates=True`` where the listener supports
        it: the listener suppresses repeat broadcasts for its other subscribers,
        but an echo we are waiting for must never be swallowed just because an
        identical frame happened to arrive moments earlier.
        """
        self.detach()
        subscribe = getattr(listener, "subscribe", None)
        if subscribe is None:  # pragma: no cover - defensive
            raise TypeError("listener does not support subscribe()")
        try:
            self._unsubscribe = subscribe(self.handle_message, include_duplicates=True)
        except TypeError:  # pragma: no cover - a listener without the option
            self._unsubscribe = subscribe(self.handle_message)
        self._listener = listener

    def detach(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._listener = None

    @property
    def pending(self) -> int:
        return len(self._waiters)

    @property
    def is_ready(self) -> bool:
        """Whether the attached listener is actually receiving right now.

        A listener that was never started, or that is between restarts, cannot
        deliver an echo.  Waiting the full verify window and then blaming the
        bridge would be a lie, so callers check this first.
        """
        if self._listener is None:
            return False
        return bool(getattr(self._listener, "is_running", True))

    def handle_message(self, message: StatusMessage) -> None:
        """Give ``message`` to the best-matching pending command, if any.

        ``_waiters`` is kept in registration order, so scanning it forwards and
        keeping only a *strictly* better match resolves ties oldest-first (FIFO)
        without needing a sequence number.
        """
        best: _Waiter | None = None
        best_quality = MatchQuality.NONE
        for waiter in self._waiters:
            if waiter.future.done():
                continue
            quality = waiter.spec.match(message)
            if quality > best_quality:
                best, best_quality = waiter, quality
        if best is not None:
            best.future.set_result(message)

    @contextlib.contextmanager
    def expect(self, spec: CommandSpec) -> Iterator[asyncio.Future[StatusMessage]]:
        """Register a waiter for ``spec`` for the duration of the block."""
        loop = asyncio.get_running_loop()
        waiter = _Waiter(spec, loop.create_future())
        self._waiters.append(waiter)
        try:
            yield waiter.future
        finally:
            with contextlib.suppress(ValueError):
                self._waiters.remove(waiter)
            if not waiter.future.done():
                waiter.future.cancel()


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class CommandSender:
    """Transport that puts a :class:`CommandSpec` on the wire."""

    #: Set once the "sending unverified" warning has been issued, so a
    #: misconfiguration is reported without one warning per light command.
    warned_unverified: bool = False

    async def send(self, spec: CommandSpec) -> None:
        raise NotImplementedError

    def on_verified(self) -> None:
        """Called when an echo has confirmed the most recent command.

        The default does nothing; transports use it to abandon diagnostics that
        can no longer tell us anything.
        """

    async def close(self) -> None:
        """Release any resources held by this transport."""


class UdpCommandSender(CommandSender):
    """Send commands as UDP requests to the bridge over one reused socket.

    A single connected datagram socket is kept for the lifetime of the sender
    and recreated if it errors, rather than opening one per command.

    The bridge's ``"AOK"`` reply is read by at most one short-lived background
    task, purely for diagnostics: the ack arrives long after the echo, so
    blocking on it would make every command three times slower for no extra
    truth.  Once an echo has confirmed the command the ack can no longer tell
    us anything, so :meth:`on_verified` abandons the read.

    Call :meth:`close` when done -- :meth:`python_rako.Bridge.close` does it for
    you, and ``async with Bridge(...)`` does it automatically.
    """

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.ack_count = 0
        self.error_ack_count = 0
        self.last_ack: AckPacket | None = None
        self._client: Any | None = None
        self._ack_task: asyncio.Task[None] | None = None
        self._closed = False

    async def _connect(self) -> Any:
        if self._client is None:
            try:
                self._client = await asyncio_dgram.connect((self.host, self.port))
            except OSError as err:
                raise RakoConnectionError(
                    f"cannot reach Rako bridge at {self.host}:{self.port}: {err}"
                ) from err
        return self._client

    def _drop_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    async def send(self, spec: CommandSpec) -> None:
        if self._closed:
            raise RakoConnectionError("command sender is closed")
        byte_list = spec.to_byte_list()
        _LOGGER.debug("Sending Rako command %s as %s", spec, byte_list)
        payload = bytes(byte_list)

        for attempt in (1, 2):
            client = await self._connect()
            try:
                await client.send(payload)
            except _SOCKET_ERRORS as err:
                # A connected UDP socket can go stale (interface change, route
                # loss). Drop it and let the second attempt build a fresh one.
                self._drop_client()
                if attempt == 2:
                    raise RakoConnectionError(f"failed to send Rako command: {err}") from err
                _LOGGER.debug("Rebuilding Rako command socket after %s", err)
                continue
            break

        self._start_ack_read()

    def _start_ack_read(self) -> None:
        if self._ack_task is not None and not self._ack_task.done():
            return  # one reader is enough; it will pick up this reply too
        self._ack_task = asyncio.ensure_future(self._drain_ack())

    async def _drain_ack(self) -> None:
        """Read one reply, bounded by ACK_READ_TIMEOUT, then stop.

        Diagnostics only: never raises, never blocks a command, and never
        outlives its timeout.
        """
        client = self._client
        if client is None:
            return
        try:
            data, _ = await asyncio.wait_for(client.recv(), timeout=ACK_READ_TIMEOUT)
        except TimeoutError:
            return
        except asyncio.CancelledError:
            # Propagate: swallowing it would leave the task "finished" and hide
            # the cancellation from whoever asked for it.
            raise
        except Exception:  # diagnostics must never break a command
            _LOGGER.debug("Failed reading Rako ack", exc_info=True)
            return
        packet = decode_packet(data)
        if isinstance(packet, AckPacket):
            self.last_ack = packet
            self.ack_count += 1
            if not packet.ok:
                self.error_ack_count += 1
                _LOGGER.warning("Rako bridge replied AERROR")
        else:
            _LOGGER.debug("Unexpected reply to a command: %s", packet)

    def on_verified(self) -> None:
        """The echo already proved the command worked; stop reading the ack."""
        if self._ack_task is not None and not self._ack_task.done():
            self._ack_task.cancel()

    async def close(self) -> None:
        self._closed = True
        task, self._ack_task = self._ack_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._drop_client()


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def execute_command(
    sender: CommandSender,
    spec: CommandSpec,
    *,
    verifier: EchoVerifier | None = None,
    verify: bool = True,
    verify_timeout: float = DEFAULT_VERIFY_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> StatusMessage | None:
    """Send ``spec`` and, when verifying, return the bridge's echo.

    Returns ``None`` when verification was not performed -- ``verify=False``, no
    verifier attached, a listener that is not currently receiving, or a command
    the bridge does not echo.  It never returns a truthy "probably fine": an
    unverified command is reported as unverified.

    :raises RakoCommandError: the command was sent ``retries + 1`` times and the
        bridge never broadcast a matching change.
    :raises RakoUnsupportedCommandError: the transport cannot express this
        command.  Propagated immediately and never retried, because resending
        it can only fail the same way.
    """
    if not verify or verifier is None:
        if verify and verifier is None:
            _warn_unverified(
                sender,
                "Sending %s without echo verification: no status listener is "
                "attached, so a command that silently fails will not be noticed",
                spec,
            )
        await sender.send(spec)
        return None

    if not verifier.is_ready:
        # The listener exists but is not receiving -- never started, or between
        # restarts. Waiting the full window and then reporting "the bridge did
        # not confirm" would blame the wrong component.
        _warn_unverified(
            sender,
            "Sending %s without echo verification: the status listener is not "
            "running, so no echo can arrive",
            spec,
        )
        await sender.send(spec)
        return None

    if not spec.expects_echo:
        _LOGGER.debug("%s is not echoed by the bridge; sending unverified", spec)
        await sender.send(spec)
        return None

    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        with verifier.expect(spec) as echo:
            # RakoUnsupportedCommandError from send() deliberately escapes the
            # retry loop: no number of resends can make the transport carry it.
            await sender.send(spec)
            try:
                message = await asyncio.wait_for(echo, timeout=verify_timeout)
            except TimeoutError:
                _LOGGER.warning(
                    "No echo for %s within %.2fs (attempt %d/%d)",
                    spec,
                    verify_timeout,
                    attempt,
                    attempts,
                )
                continue
            _LOGGER.debug("Command %s verified by echo %s", spec, message)
            sender.on_verified()
            return message

    raise RakoCommandError(
        f"Rako bridge did not confirm {spec.command.name} for room {spec.room} "
        f"channel {spec.channel} after {attempts} attempts"
    )


def _warn_unverified(sender: CommandSender, message: str, spec: CommandSpec) -> None:
    """Warn once per transport, then drop to debug.

    Otherwise a consumer that never attaches a listener gets one warning per
    light command.
    """
    log = _LOGGER.debug if sender.warned_unverified else _LOGGER.warning
    sender.warned_unverified = True
    log(message, spec)


def as_status_frame(spec: CommandSpec) -> list[int]:
    """The status frame the bridge would broadcast for ``spec``.

    Used by tests and by fake bridges; the frames differ only in the leading
    type byte and the checksum domain.
    """
    return encode_command(
        spec.room,
        spec.channel,
        spec.command,
        spec.data,
        message_type=MessageType.STATUS,
    )
