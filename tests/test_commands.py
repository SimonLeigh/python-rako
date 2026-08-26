"""Echo-verified command tests, driven by a fake bridge on loopback.

The fake bridge behaves like the real one: it accepts a UDP request, broadcasts
a status message back a short time later, and only then sends its ``"AOK"``.
Timings mirror the measurements in ``BRIDGE_BEHAVIOUR.md`` (echo 144-306 ms,
ack 677-770 ms), scaled down so the suite stays fast.
"""

import asyncio
import contextlib
import socket
import time

import pytest

from python_rako.bridge import Bridge, BridgeCommanderHTTP
from python_rako.commands import (
    CommandSpec,
    EchoVerifier,
    MatchQuality,
    UdpCommandSender,
    as_status_frame,
    execute_command,
    level_command,
    scene_command,
)
from python_rako.const import CommandType, MessageOrigin, MessageType
from python_rako.exceptions import RakoCommandError
from python_rako.listener import StatusListener
from python_rako.model import ChannelStatusMessage, SceneStatusMessage
from python_rako.protocol import (
    FadeMessage,
    StopFadeMessage,
    calc_crc,
    decode_status_message,
    encode_command,
)

LOOPBACK = "127.0.0.1"


def status_frame_for(request: list[int]) -> list[int]:
    """The status frame the bridge broadcasts when it acts on ``request``."""
    body = request[1:-1]
    return [MessageType.STATUS.value, *body, calc_crc(body[1:])]


class FakeBridge:
    """A loopback UDP endpoint that echoes status broadcasts like a bridge."""

    def __init__(
        self,
        *,
        echo_delay: float = 0.02,
        ack_delay: float = 0.4,
        silent: bool = False,
        silent_for: int = 0,
        echo_builder=None,
        send_ack: bool = True,
    ) -> None:
        self.echo_delay = echo_delay
        self.ack_delay = ack_delay
        self.silent = silent
        self.silent_for = silent_for
        self.echo_builder = echo_builder
        self.send_ack = send_ack
        self.requests: list[list[int]] = []
        self.broadcast_to: tuple[str, int] | None = None
        self.port = 0
        self._sock: socket.socket | None = None
        self._task: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((LOOPBACK, 0))
        self._sock.setblocking(False)
        self.port = self._sock.getsockname()[1]
        self._task = asyncio.create_task(self._serve())

    async def stop(self) -> None:
        for task in [self._task, *self._pending]:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._sock is not None:
            self._sock.close()

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        assert self._sock is not None
        while True:
            data, addr = await loop.sock_recvfrom(self._sock, 512)
            request = list(data)
            self.requests.append(request)
            self._spawn(self._respond(request, addr))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _respond(self, request: list[int], addr) -> None:
        index = len(self.requests)
        should_echo = not self.silent and index > self.silent_for
        if should_echo and self.broadcast_to is not None:
            await asyncio.sleep(self.echo_delay)
            frames = (
                self.echo_builder(request)
                if self.echo_builder
                else [status_frame_for(request)]
            )
            assert self._sock is not None
            for frame in frames:
                self._sock.sendto(bytes(frame), self.broadcast_to)
        if self.send_ack:
            await asyncio.sleep(self.ack_delay)
            with contextlib.suppress(OSError):
                assert self._sock is not None
                self._sock.sendto(b"AOK", addr)


@pytest.fixture
async def fake_bridge():
    bridge = FakeBridge()
    await bridge.start()
    yield bridge
    await bridge.stop()


@pytest.fixture
async def listener():
    listener = StatusListener(LOOPBACK, port=0, listen_host=LOOPBACK)
    await listener.start()
    yield listener
    await listener.stop()


@pytest.fixture
async def bridge(fake_bridge, listener):
    """A Bridge wired to the fake bridge with echo verification enabled."""
    fake_bridge.broadcast_to = (LOOPBACK, listener.local_port)
    bridge = Bridge(LOOPBACK, fake_bridge.port, "fake", "00:00:00:00:00:00")
    bridge.verify_timeout = 0.3
    bridge.attach_listener(listener)
    yield bridge
    await bridge.close()


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_set_channel_level_returns_the_echo(bridge, fake_bridge):
    message = await bridge.set_channel_level(7, 2, 255)
    assert message == ChannelStatusMessage(7, 2, 255)
    assert len(fake_bridge.requests) == 1
    assert fake_bridge.requests[0][5] == CommandType.SET_LEVEL.value


async def test_set_room_scene_returns_the_echo(bridge):
    message = await bridge.set_room_scene(6, 2)
    assert message == SceneStatusMessage(6, 0, 2)


async def test_set_room_level_targets_channel_zero(bridge, fake_bridge):
    message = await bridge.set_room_level(9, 128)
    assert message == ChannelStatusMessage(9, 0, 128)
    assert fake_bridge.requests[0][4] == 0


async def test_fade_and_stop_are_verified(bridge):
    fade = await bridge.fade_up(9)
    stop = await bridge.stop_fade(9)
    assert isinstance(fade, FadeMessage)
    assert isinstance(stop, StopFadeMessage)


async def test_verification_returns_well_before_the_ack(bridge, fake_bridge):
    """The AOK trails the echo; waiting for it would triple command latency."""
    fake_bridge.echo_delay = 0.02
    fake_bridge.ack_delay = 0.5
    started = time.monotonic()
    await bridge.set_channel_level(7, 2, 64)
    elapsed = time.monotonic() - started
    assert elapsed < 0.3


async def test_the_ack_is_still_collected_for_diagnostics(bridge, fake_bridge):
    fake_bridge.ack_delay = 0.02
    await bridge.set_channel_level(7, 2, 64)
    for _ in range(50):
        sender = bridge._sender
        if isinstance(sender, UdpCommandSender) and sender.ack_count:
            break
        await asyncio.sleep(0.02)
    assert isinstance(bridge._sender, UdpCommandSender)
    assert bridge._sender.ack_count == 1
    assert bridge._sender.last_ack is not None
    assert bridge._sender.last_ack.ok is True


# ---------------------------------------------------------------------------
# Echo-form variants
# ---------------------------------------------------------------------------


async def test_a_scene_set_echoed_as_a_legacy_sc_command_still_verifies(
    bridge, fake_bridge
):
    """The app and some keypads echo scene selections as SC1-SC4, not SET_SCENE."""

    def legacy_echo(request):
        scene = request[7]
        legacy = {1: 3, 2: 4, 3: 5, 4: 6}[scene]
        return [
            encode_command(
                request[3], request[4], legacy, message_type=MessageType.STATUS
            )
        ]

    fake_bridge.echo_builder = legacy_echo
    message = await bridge.set_room_scene(6, 2)
    assert message == SceneStatusMessage(6, 0, 2)
    assert message.command is CommandType.SC2_LEGACY


async def test_a_clamped_level_still_verifies_as_a_loose_match(bridge, fake_bridge):
    """Some circuits have a configured minimum, so the echo may differ."""

    def clamped(request):
        frame = status_frame_for(request)
        frame[7] = 20  # the bridge drove it to its minimum instead
        frame[-1] = calc_crc(frame[2:-1])
        return [frame]

    fake_bridge.echo_builder = clamped
    message = await bridge.set_channel_level(7, 2, 5)
    assert isinstance(message, ChannelStatusMessage)
    assert message.brightness == 20


# ---------------------------------------------------------------------------
# Failure and retry
# ---------------------------------------------------------------------------


async def test_silence_retries_once_then_raises(bridge, fake_bridge):
    fake_bridge.silent = True
    with pytest.raises(RakoCommandError, match="did not confirm"):
        await bridge.set_channel_level(7, 2, 255)
    assert len(fake_bridge.requests) == 2


async def test_a_command_lost_on_the_first_attempt_succeeds_on_the_retry(
    bridge, fake_bridge
):
    """The observed failure mode: the command never reaches the circuit."""
    fake_bridge.silent_for = 1
    message = await bridge.set_channel_level(7, 2, 255)
    assert message == ChannelStatusMessage(7, 2, 255)
    assert len(fake_bridge.requests) == 2


async def test_retries_can_be_disabled(bridge, fake_bridge):
    fake_bridge.silent = True
    with pytest.raises(RakoCommandError):
        await bridge.send_command(level_command(7, 2, 255), retries=0)
    assert len(fake_bridge.requests) == 1


# ---------------------------------------------------------------------------
# Unverified paths
# ---------------------------------------------------------------------------


async def test_verify_false_sends_and_returns_none(bridge, fake_bridge):
    result = await bridge.set_channel_level(7, 2, 255, verify=False)
    assert result is None
    await asyncio.sleep(0.1)
    assert len(fake_bridge.requests) == 1


async def test_without_a_listener_the_command_is_sent_unverified(
    fake_bridge, caplog
):
    """Legacy behaviour, minus the lie: silence is reported, not called success."""
    fake_bridge.silent = True
    bridge = Bridge(LOOPBACK, fake_bridge.port, "fake", "00:00:00:00:00:00")
    try:
        result = await bridge.set_channel_level(7, 2, 255)
        assert result is None
        await asyncio.sleep(0.1)
        assert len(fake_bridge.requests) == 1
        assert "without echo verification" in caplog.text
    finally:
        await bridge.close()


async def test_detaching_the_listener_returns_to_the_unverified_path(
    bridge, fake_bridge
):
    fake_bridge.silent = True
    bridge.detach_listener()
    assert await bridge.set_channel_level(7, 2, 255) is None


async def test_a_command_the_bridge_never_echoes_is_not_waited_for(bridge, fake_bridge):
    fake_bridge.silent = True
    spec = CommandSpec(7, 0, CommandType.IDENT)
    assert await bridge.send_command(spec) is None
    await asyncio.sleep(0.05)
    assert len(fake_bridge.requests) == 1


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


async def test_commands_to_different_targets_verify_independently(
    bridge, fake_bridge
):
    fake_bridge.echo_delay = 0.05
    results = await asyncio.gather(
        bridge.set_channel_level(7, 2, 255),
        bridge.set_channel_level(9, 1, 64),
        bridge.set_room_scene(6, 3),
    )
    assert results[0] == ChannelStatusMessage(7, 2, 255)
    assert results[1] == ChannelStatusMessage(9, 1, 64)
    assert results[2] == SceneStatusMessage(6, 0, 3)


async def test_two_commands_to_the_same_channel_do_not_steal_each_others_echoes(
    bridge, fake_bridge
):
    fake_bridge.echo_delay = 0.05
    first, second = await asyncio.gather(
        bridge.set_channel_level(7, 2, 10),
        bridge.set_channel_level(7, 2, 200),
    )
    assert first == ChannelStatusMessage(7, 2, 10)
    assert second == ChannelStatusMessage(7, 2, 200)


# ---------------------------------------------------------------------------
# EchoVerifier matching, without any sockets
# ---------------------------------------------------------------------------


async def test_exact_matches_beat_loose_ones():
    verifier = EchoVerifier()
    loose_spec = level_command(7, 2, 10)
    exact_spec = level_command(7, 2, 200)
    with verifier.expect(loose_spec) as loose, verifier.expect(exact_spec) as exact:
        verifier.handle_message(ChannelStatusMessage(7, 2, 200))
        assert exact.done()
        assert not loose.done()


async def test_identical_specs_are_resolved_first_in_first_out():
    verifier = EchoVerifier()
    spec = level_command(7, 2, 200)
    with verifier.expect(spec) as first, verifier.expect(spec) as second:
        verifier.handle_message(ChannelStatusMessage(7, 2, 200))
        assert first.done()
        assert not second.done()
        verifier.handle_message(ChannelStatusMessage(7, 2, 200))
        assert second.done()


def test_a_sensor_broadcast_cannot_satisfy_a_loose_match():
    """A PIR firing in the same room must not be mistaken for our echo."""
    spec = scene_command(145, 3)
    sensor = SceneStatusMessage(145, 0, 1, origin=MessageOrigin.SENSOR)
    control = SceneStatusMessage(145, 0, 1, origin=MessageOrigin.CONTROL)
    assert spec.match(sensor) is MatchQuality.NONE
    assert spec.match(control) is MatchQuality.LOOSE
    assert spec.match(SceneStatusMessage(145, 0, 3)) is MatchQuality.EXACT


def test_messages_for_other_targets_never_match():
    spec = level_command(7, 2, 200)
    assert spec.match(ChannelStatusMessage(7, 3, 200)) is MatchQuality.NONE
    assert spec.match(ChannelStatusMessage(8, 2, 200)) is MatchQuality.NONE
    assert spec.match(SceneStatusMessage(7, 2, 1)) is MatchQuality.NONE


def test_off_matches_both_a_scene_zero_and_a_zero_level():
    spec = CommandSpec(7, 0, CommandType.OFF)
    assert spec.match(SceneStatusMessage(7, 0, 0)) is MatchQuality.EXACT
    assert spec.match(ChannelStatusMessage(7, 0, 0)) is MatchQuality.EXACT


async def test_waiters_are_removed_when_the_block_exits():
    verifier = EchoVerifier()
    spec = level_command(7, 2, 200)
    with verifier.expect(spec):
        assert verifier.pending == 1
    assert verifier.pending == 0


def test_as_status_frame_round_trips():
    spec = level_command(7, 2, 129)
    assert decode_status_message(as_status_frame(spec)) == ChannelStatusMessage(
        7, 2, 129
    )


def test_level_command_rejects_a_bad_level():
    with pytest.raises(ValueError):
        level_command(7, 2, 300)


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


class _RecordingHttpCommander(BridgeCommanderHTTP):
    def __init__(self):
        self.scenes: list[tuple[int, int]] = []
        self.levels: list[tuple[int, int, int]] = []

    async def set_room_scene(self, room_id: int, scene: int) -> None:
        self.scenes.append((room_id, scene))

    async def set_channel_brightness(
        self, room_id: int, channel_id: int, brightness: int
    ) -> None:
        self.levels.append((room_id, channel_id, brightness))


async def test_http_transport_sends_scene_and_level_commands():
    commander = _RecordingHttpCommander()
    bridge = Bridge(LOOPBACK, 9761, "fake", "mac", commander)
    await bridge.set_room_scene(6, 2, verify=False)
    await bridge.set_channel_level(7, 2, 129, verify=False)
    assert commander.scenes == [(6, 2)]
    assert commander.levels == [(7, 2, 129)]


async def test_http_transport_rejects_commands_it_cannot_express():
    commander = _RecordingHttpCommander()
    bridge = Bridge(LOOPBACK, 9761, "fake", "mac", commander)
    with pytest.raises(RakoCommandError, match="HTTP transport"):
        await bridge.fade_up(9, verify=False)


async def test_execute_command_without_a_verifier_warns_once(fake_bridge, caplog):
    sender = UdpCommandSender(LOOPBACK, fake_bridge.port)
    try:
        result = await execute_command(sender, level_command(7, 2, 1))
        assert result is None
        assert "without echo verification" in caplog.text
    finally:
        await sender.close()
