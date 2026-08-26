"""StatusListener tests.

These drive a *real* UDP socket pair on loopback -- nothing in asyncio is
mocked -- so the bind flags, the receive loop and the supervision all get
exercised as they will be against a bridge.
"""

import asyncio
import contextlib
import socket

import pytest

from python_rako.const import MessageOrigin
from python_rako.listener import ListenerHealth, StatusListener
from python_rako.model import ChannelStatusMessage, SceneStatusMessage
from python_rako.protocol import FadeMessage

LOOPBACK = "127.0.0.1"
# TEST-NET-1 (RFC 5737): guaranteed never to be a real host on the LAN.
NOT_THE_BRIDGE = "192.0.2.10"

SET_LEVEL_R7_C2_255 = bytes([83, 7, 0, 7, 2, 52, 1, 255, 195])
SET_LEVEL_R7_C2_0 = bytes([83, 7, 0, 7, 2, 52, 1, 0, 194])
SC2_R6 = bytes([83, 5, 0, 6, 0, 4, 246])
FADE_R9 = bytes([83, 10, 0, 9, 0, 50, 128, 0, 0, 0, 0, 69])
SENSOR_SCENE_R145 = bytes([83, 10, 0, 145, 0, 49, 9, 1, 0, 0, 0, 52])
DISCOVERY_PING = b"D"


@pytest.fixture
def sender():
    """A UDP socket bound to loopback, standing in for the bridge."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((LOOPBACK, 0))
    yield sock
    sock.close()


@pytest.fixture
async def listener():
    """A started listener on an ephemeral loopback port."""
    listener = StatusListener(
        LOOPBACK,
        port=0,
        listen_host=LOOPBACK,
        backoff_initial=0.01,
        backoff_max=0.05,
    )
    await listener.start()
    try:
        yield listener
    finally:
        await listener.stop()


async def _collect(listener: StatusListener, count: int, timeout: float = 2.0):
    """Wait for ``count`` messages to be delivered to a subscriber."""
    received: list = []
    done = asyncio.Event()

    def handler(message):
        received.append(message)
        if len(received) >= count:
            done.set()

    unsubscribe = listener.subscribe(handler)
    try:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(done.wait(), timeout=timeout)
    finally:
        unsubscribe()
    return received


async def _settle(seconds: float = 0.15) -> None:
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# Receiving
# ---------------------------------------------------------------------------


async def test_receives_and_decodes_a_broadcast(listener, sender):
    received: list = []
    listener.subscribe(received.append)

    sender.sendto(SET_LEVEL_R7_C2_255, (LOOPBACK, listener.local_port))
    await _settle()

    assert received == [ChannelStatusMessage(7, 2, 255)]
    assert listener.messages_received == 1
    assert listener.last_message_at is not None


async def test_binds_with_reuse_flags(listener):
    """Two listeners must be able to share port 9761 on one host."""
    port = listener.local_port
    second = StatusListener(LOOPBACK, port=port, listen_host=LOOPBACK)
    await second.start()
    try:
        assert second.is_running
        assert second.local_port == port
    finally:
        await second.stop()


async def test_async_subscribers_are_awaited(listener, sender):
    received: list = []

    async def handler(message):
        await asyncio.sleep(0)
        received.append(message)

    listener.subscribe(handler)
    sender.sendto(SC2_R6, (LOOPBACK, listener.local_port))
    await _settle()

    assert received == [SceneStatusMessage(6, 0, 2)]


async def test_messages_async_iterator(listener, sender):
    received = []

    async def consume():
        async for message in listener.messages():
            received.append(message)

    task = asyncio.create_task(consume())
    await _settle(0.05)
    sender.sendto(SC2_R6, (LOOPBACK, listener.local_port))
    sender.sendto(FADE_R9, (LOOPBACK, listener.local_port))
    await _settle()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert received[0] == SceneStatusMessage(6, 0, 2)
    assert isinstance(received[1], FadeMessage)


async def test_origin_is_preserved_through_the_listener(listener, sender):
    sender.sendto(SENSOR_SCENE_R145, (LOOPBACK, listener.local_port))
    received = await _collect(listener, 1)
    assert received[0].origin is MessageOrigin.SENSOR


# ---------------------------------------------------------------------------
# Source filtering
# ---------------------------------------------------------------------------


async def test_packets_from_other_hosts_are_ignored_but_counted(sender):
    listener = StatusListener(NOT_THE_BRIDGE, port=0, listen_host=LOOPBACK)
    await listener.start()
    try:
        received: list = []
        listener.subscribe(received.append)
        sender.sendto(SET_LEVEL_R7_C2_255, (LOOPBACK, listener.local_port))
        await _settle()

        assert received == []
        assert listener.ignored_packets == 1
        assert listener.messages_received == 0
    finally:
        await listener.stop()


async def test_non_status_packets_from_the_bridge_are_counted_not_delivered(
    listener, sender
):
    """Phones on the LAN broadcast discovery pings; they are not errors."""
    received: list = []
    listener.subscribe(received.append)

    sender.sendto(DISCOVERY_PING, (LOOPBACK, listener.local_port))
    await _settle()

    assert received == []
    assert listener.non_status_packets == 1
    assert listener.is_running


async def test_a_source_port_other_than_9761_is_accepted(listener, sender):
    """The bridge broadcasts from an ephemeral port (observed: 2861)."""
    assert sender.getsockname()[1] != 9761
    sender.sendto(SET_LEVEL_R7_C2_255, (LOOPBACK, listener.local_port))
    received = await _collect(listener, 1)
    assert received


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------


async def test_identical_messages_inside_the_window_are_suppressed(listener, sender):
    """The bridge re-broadcasts some keypad events ~200 ms apart."""
    received: list = []
    listener.subscribe(received.append)

    sender.sendto(FADE_R9, (LOOPBACK, listener.local_port))
    await _settle(0.05)
    sender.sendto(FADE_R9, (LOOPBACK, listener.local_port))
    await _settle()

    assert len(received) == 1
    assert listener.suppressed_duplicates == 1


async def test_different_messages_are_never_suppressed(listener, sender):
    received: list = []
    listener.subscribe(received.append)

    sender.sendto(SET_LEVEL_R7_C2_255, (LOOPBACK, listener.local_port))
    sender.sendto(SET_LEVEL_R7_C2_0, (LOOPBACK, listener.local_port))
    await _settle()

    assert len(received) == 2
    assert listener.suppressed_duplicates == 0


async def test_repeats_outside_the_window_are_delivered(sender):
    listener = StatusListener(
        LOOPBACK, port=0, listen_host=LOOPBACK, dedupe_window=0.05
    )
    await listener.start()
    try:
        received: list = []
        listener.subscribe(received.append)
        sender.sendto(FADE_R9, (LOOPBACK, listener.local_port))
        await _settle(0.12)
        sender.sendto(FADE_R9, (LOOPBACK, listener.local_port))
        await _settle()
        assert len(received) == 2
        assert listener.suppressed_duplicates == 0
    finally:
        await listener.stop()


async def test_dedupe_can_be_disabled(sender):
    listener = StatusListener(LOOPBACK, port=0, listen_host=LOOPBACK, dedupe_window=0)
    await listener.start()
    try:
        received: list = []
        listener.subscribe(received.append)
        sender.sendto(FADE_R9, (LOOPBACK, listener.local_port))
        sender.sendto(FADE_R9, (LOOPBACK, listener.local_port))
        await _settle()
        assert len(received) == 2
    finally:
        await listener.stop()


# ---------------------------------------------------------------------------
# Subscriber isolation
# ---------------------------------------------------------------------------


async def test_a_failing_subscriber_does_not_affect_the_others(listener, sender):
    good_before: list = []
    good_after: list = []

    def boom(_message):
        raise RuntimeError("subscriber exploded")

    async def async_boom(_message):
        raise RuntimeError("async subscriber exploded")

    listener.subscribe(good_before.append)
    listener.subscribe(boom)
    listener.subscribe(async_boom)
    listener.subscribe(good_after.append)

    sender.sendto(SET_LEVEL_R7_C2_255, (LOOPBACK, listener.local_port))
    sender.sendto(SET_LEVEL_R7_C2_0, (LOOPBACK, listener.local_port))
    await _settle()

    assert len(good_before) == 2
    assert len(good_after) == 2
    assert listener.is_running
    assert listener.restart_count == 0


async def test_unsubscribe_stops_delivery(listener, sender):
    received: list = []
    unsubscribe = listener.subscribe(received.append)

    sender.sendto(SET_LEVEL_R7_C2_255, (LOOPBACK, listener.local_port))
    await _settle()
    unsubscribe()
    sender.sendto(SET_LEVEL_R7_C2_0, (LOOPBACK, listener.local_port))
    await _settle()

    assert len(received) == 1
    unsubscribe()  # idempotent


# ---------------------------------------------------------------------------
# Supervision
# ---------------------------------------------------------------------------


class _FlakyEndpointListener(StatusListener):
    """Injects a receive-loop failure after the first datagram."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.explode_after = 1
        self._seen = 0

    async def _receive_forever(self):
        assert self._endpoint is not None
        while not self._stopping.is_set():
            data, addr = await self._endpoint.recv()
            self._seen += 1
            if self._seen == self.explode_after:
                raise OSError("simulated socket failure")
            self._handle_datagram(bytes(data), addr)


async def test_the_loop_restarts_after_an_exception(sender):
    listener = _FlakyEndpointListener(
        LOOPBACK, port=0, listen_host=LOOPBACK, backoff_initial=0.01, backoff_max=0.05
    )
    await listener.start()
    try:
        port = listener.local_port
        sender.sendto(SET_LEVEL_R7_C2_255, (LOOPBACK, port))  # triggers the failure
        await _settle(0.3)

        assert listener.restart_count >= 1
        assert listener.is_running

        # ... and it is genuinely receiving again on a fresh socket.
        received: list = []
        listener.subscribe(received.append)
        sender.sendto(SET_LEVEL_R7_C2_0, (LOOPBACK, listener.local_port))
        await _settle()
        assert received == [ChannelStatusMessage(7, 2, 0)]
    finally:
        await listener.stop()


class _UnbindableListener(StatusListener):
    """Fails the first N bind attempts."""

    def __init__(self, *args, fail_times=2, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_times = fail_times
        self.attempts = 0

    async def _open_endpoint(self):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise OSError("simulated bind failure")
        return await super()._open_endpoint()


async def test_bind_failures_back_off_and_eventually_succeed(sender):
    listener = _UnbindableListener(
        LOOPBACK, port=0, listen_host=LOOPBACK, backoff_initial=0.01, backoff_max=0.05
    )
    await listener.start()
    try:
        assert listener.is_running is False  # first bind failed
        for _ in range(50):
            if listener.is_running:
                break
            await asyncio.sleep(0.02)
        assert listener.is_running
        assert listener.restart_count >= 2
        assert listener.attempts >= 3

        received: list = []
        listener.subscribe(received.append)
        sender.sendto(SC2_R6, (LOOPBACK, listener.local_port))
        await _settle()
        assert received == [SceneStatusMessage(6, 0, 2)]
    finally:
        await listener.stop()


async def test_health_callback_reports_transitions(sender):
    seen: list[ListenerHealth] = []
    listener = _FlakyEndpointListener(
        LOOPBACK,
        port=0,
        listen_host=LOOPBACK,
        backoff_initial=0.01,
        backoff_max=0.05,
        on_health_change=seen.append,
    )
    await listener.start()
    try:
        assert seen and seen[0].is_running is True
        sender.sendto(SET_LEVEL_R7_C2_255, (LOOPBACK, listener.local_port))
        await _settle(0.3)
        assert any(h.is_running is False for h in seen)
        assert any(h.last_error and "simulated" in h.last_error for h in seen)
        assert listener.health().restart_count >= 1
    finally:
        await listener.stop()


async def test_a_raising_health_callback_does_not_break_the_listener(sender):
    def boom(_health):
        raise RuntimeError("health callback exploded")

    listener = StatusListener(
        LOOPBACK, port=0, listen_host=LOOPBACK, on_health_change=boom
    )
    await listener.start()
    try:
        assert listener.is_running
        received: list = []
        listener.subscribe(received.append)
        sender.sendto(SC2_R6, (LOOPBACK, listener.local_port))
        await _settle()
        assert received
    finally:
        await listener.stop()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_async_context_manager(sender):
    async with StatusListener(LOOPBACK, port=0, listen_host=LOOPBACK) as listener:
        assert listener.is_running
        received: list = []
        listener.subscribe(received.append)
        sender.sendto(SC2_R6, (LOOPBACK, listener.local_port))
        await _settle()
        assert received
    assert listener.is_running is False


async def test_start_is_idempotent_and_stop_is_safe_twice(listener):
    await listener.start()
    assert listener.is_running
    await listener.stop()
    await listener.stop()
    assert listener.is_running is False


async def test_health_snapshot_fields(listener, sender):
    sender.sendto(SET_LEVEL_R7_C2_255, (LOOPBACK, listener.local_port))
    await _settle()
    health = listener.health()
    assert health.is_running
    assert health.messages_received == 1
    assert health.restart_count == 0
    assert health.seconds_since_last_message is not None
    assert health.seconds_since_last_message < 5

    idle = ListenerHealth(is_running=False, last_message_at=None, restart_count=0)
    assert idle.seconds_since_last_message is None
