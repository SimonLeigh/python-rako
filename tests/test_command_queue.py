"""Pacing-queue tests.

The queue is timing behaviour, so these tests are written to be *directionally*
deterministic: ``asyncio`` never wakes a sleeper early, so "at least the
interval elapsed between two sends" can be asserted exactly.  Upper bounds are
only asserted where they are generous enough not to depend on machine load.
Intervals are scaled down (``INTERVAL``) rather than faked, so the real
``asyncio`` scheduling path is under test.
"""

import asyncio
import contextlib

import pytest

from python_rako.bridge import Bridge
from python_rako.commands import CommandSpec, level_command, scene_command, stop_command
from python_rako.const import CommandType
from python_rako.exceptions import RakoCommandError, RakoQueueClosedError
from python_rako.model import ChannelStatusMessage, SceneStatusMessage, StatusMessage
from python_rako.pacing import DEFAULT_MIN_COMMAND_INTERVAL, CommandQueue

#: Short enough to keep the suite fast, long enough that a send that ignored
#: pacing would land inside the same event-loop tick and be caught.
INTERVAL = 0.05


class RecordingRunner:
    """Stands in for ``Bridge._execute``: records, optionally stalls, fails."""

    def __init__(
        self,
        *,
        duration: float = 0.0,
        fail_on: CommandType | None = None,
    ) -> None:
        self.calls: list[CommandSpec] = []
        self.options: list[dict] = []
        self.started_at: list[float] = []
        self.finished_at: list[float] = []
        self.duration = duration
        self.fail_on = fail_on
        self.gate: asyncio.Event | None = None

    async def __call__(self, spec: CommandSpec, **options: object) -> StatusMessage | None:
        loop = asyncio.get_running_loop()
        self.calls.append(spec)
        self.options.append(dict(options))
        self.started_at.append(loop.time())
        if self.gate is not None:
            await self.gate.wait()
        if self.duration:
            await asyncio.sleep(self.duration)
        self.finished_at.append(loop.time())
        if self.fail_on is not None and spec.command is self.fail_on:
            raise RakoCommandError(f"bridge did not confirm {spec.command.name}")
        return _echo_for(spec)

    @property
    def gaps(self) -> list[float]:
        """Seconds between consecutive sends."""
        return [b - a for a, b in zip(self.started_at, self.started_at[1:], strict=False)]

    @property
    def levels(self) -> list[int | None]:
        return [spec.expected_level for spec in self.calls]


def _echo_for(spec: CommandSpec) -> StatusMessage:
    if spec.expected_level is not None:
        return ChannelStatusMessage(spec.room, spec.channel, spec.expected_level)
    return SceneStatusMessage(spec.room, spec.channel, spec.expected_scene or 0)


@pytest.fixture
async def runner():
    return RecordingRunner()


@pytest.fixture
async def queue(runner):
    queue = CommandQueue(runner, min_interval=INTERVAL, name="test")
    yield queue
    await queue.close()


# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------


async def test_the_first_command_is_not_delayed(queue, runner):
    """Pacing protects the bridge from bursts; it must not add idle latency."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    await queue.submit(level_command(7, 2, 100))
    assert loop.time() - started < INTERVAL
    assert len(runner.calls) == 1


async def test_rapid_requests_are_spaced_by_the_interval(queue, runner):
    """Four requests submitted in one tick reach the bridge one interval apart."""
    results = await asyncio.gather(
        *(queue.submit(level_command(7, channel, 50)) for channel in range(1, 5))
    )
    assert len(runner.calls) == 4
    assert all(gap >= INTERVAL * 0.95 for gap in runner.gaps), runner.gaps
    assert [message.channel for message in results] == [1, 2, 3, 4]


async def test_nothing_is_dropped_under_a_burst(queue, runner):
    """Twenty distinct targets: all twenty arrive, none is lost."""
    await asyncio.gather(*(queue.submit(level_command(7, channel, 10)) for channel in range(20)))
    assert len(runner.calls) == 20
    assert queue.sent == 20
    assert queue.coalesced == 0


async def test_fifo_order_across_targets(queue, runner):
    """Different targets never overtake one another."""
    submitted = [(1, 2), (3, 0), (7, 4), (1, 5)]
    await asyncio.gather(*(queue.submit(level_command(r, c, 20)) for r, c in submitted))
    assert [(spec.room, spec.channel) for spec in runner.calls] == submitted


# ---------------------------------------------------------------------------
# Coalescing
# ---------------------------------------------------------------------------


async def test_a_slider_drag_becomes_one_send(queue, runner):
    """Twenty levels for one channel while the queue is busy: one send, the last level."""
    blocker = queue.enqueue(level_command(9, 9, 1))  # occupies the first slot
    drag = [queue.enqueue(level_command(7, 2, level)) for level in range(20)]

    results = await asyncio.gather(blocker, *drag)

    assert runner.levels == [1, 19]
    # Every requester is answered, and with the level that was actually applied.
    assert results[1:] == [ChannelStatusMessage(7, 2, 19)] * 20
    assert queue.coalesced == 19
    assert queue.sent == 2


async def test_a_superseded_request_resolves_with_the_replacing_echo(queue, runner):
    """The contract callers see: the answer describes what the bridge really did."""
    blocker = queue.enqueue(level_command(9, 9, 1))
    superseded = queue.enqueue(level_command(7, 2, 10))
    replacing = queue.enqueue(level_command(7, 2, 200))

    await blocker
    assert await superseded == ChannelStatusMessage(7, 2, 200)
    assert await replacing == ChannelStatusMessage(7, 2, 200)
    assert 10 not in runner.levels


async def test_coalescing_keeps_the_original_queue_position(queue, runner):
    """A newer payload must not jump the queue -- only replace what is there."""
    first = queue.enqueue(level_command(9, 9, 1))
    second = queue.enqueue(level_command(7, 2, 10))
    third = queue.enqueue(level_command(3, 1, 30))
    fourth = queue.enqueue(level_command(7, 2, 99))  # coalesces into `second`

    await asyncio.gather(first, second, third, fourth)
    assert [(spec.room, spec.channel) for spec in runner.calls] == [(9, 9), (7, 2), (3, 1)]
    assert runner.levels == [1, 99, 30]


async def test_different_kinds_of_command_coalesce_on_the_same_target(queue, runner):
    """The bridge honours only the last one anyway, so sending both is waste."""
    blocker = queue.enqueue(level_command(9, 9, 1))
    level = queue.enqueue(level_command(6, 0, 128))
    scene = queue.enqueue(scene_command(6, 3))

    await blocker
    assert await level == SceneStatusMessage(6, 0, 3)
    assert await scene == SceneStatusMessage(6, 0, 3)
    assert [spec.command for spec in runner.calls] == [
        CommandType.SET_LEVEL,
        CommandType.SET_SCENE,
    ]


async def test_a_stop_coalesces_over_a_pending_fade(queue, runner):
    """Latest wins even when the two commands mean opposite things."""
    blocker = queue.enqueue(level_command(9, 9, 1))
    queue.enqueue(level_command(4, 1, 200))
    stop = queue.enqueue(stop_command(4, 1))

    await blocker
    await stop
    assert [spec.command for spec in runner.calls] == [
        CommandType.SET_LEVEL,
        CommandType.STOP_FADING,
    ]


async def test_an_in_flight_command_is_not_coalesced_into(queue, runner):
    """Once it is on the wire it cannot be recalled, so it must not be edited."""
    runner.gate = asyncio.Event()
    first = queue.enqueue(level_command(7, 2, 10))
    await asyncio.sleep(0)  # let the worker pick it up
    await asyncio.sleep(0)
    assert queue.in_flight
    second = queue.enqueue(level_command(7, 2, 20))
    runner.gate.set()

    assert await first == ChannelStatusMessage(7, 2, 10)
    assert await second == ChannelStatusMessage(7, 2, 20)
    assert runner.levels == [10, 20]


# ---------------------------------------------------------------------------
# Interaction with echo verification
# ---------------------------------------------------------------------------


async def test_the_next_send_waits_for_a_slow_verification(queue, runner):
    """Echo slower than the interval: no second command while one is in flight."""
    runner.duration = INTERVAL * 4
    await asyncio.gather(
        queue.submit(level_command(7, 1, 10)),
        queue.submit(level_command(7, 2, 20)),
    )
    # The second send started after the first *finished*, not merely one
    # interval after it started.
    assert runner.started_at[1] >= runner.finished_at[0]


async def test_the_next_send_waits_for_the_interval_when_verification_is_fast(queue, runner):
    """Echo faster than the interval: pacing, not the echo, sets the rhythm."""
    runner.duration = INTERVAL / 10
    await asyncio.gather(
        queue.submit(level_command(7, 1, 10)),
        queue.submit(level_command(7, 2, 20)),
    )
    assert runner.started_at[1] - runner.started_at[0] >= INTERVAL * 0.95
    assert runner.started_at[1] > runner.finished_at[0]


async def test_only_one_command_is_ever_in_flight(queue, runner):
    """Overlapping sends are the pattern that lost a command in Phase 0."""
    concurrent = 0
    peak = 0
    original = runner.__call__

    async def counting(spec, **options):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        try:
            return await original(spec, **options)
        finally:
            concurrent -= 1

    queue._runner = counting
    runner.duration = INTERVAL / 2
    await asyncio.gather(*(queue.submit(level_command(7, channel, 5)) for channel in range(4)))
    assert peak == 1


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


async def test_a_failing_command_does_not_stall_the_queue(queue, runner):
    """The exception reaches its own requester; everyone else is unaffected."""
    runner.fail_on = CommandType.SET_SCENE
    failing = queue.enqueue(scene_command(6, 3))
    following = queue.enqueue(level_command(7, 2, 40))

    with pytest.raises(RakoCommandError):
        await failing
    assert await following == ChannelStatusMessage(7, 2, 40)
    assert queue.failed == 1
    assert queue.sent == 1


async def test_a_failure_is_shared_with_everyone_who_coalesced_into_it(queue, runner):
    runner.fail_on = CommandType.SET_LEVEL
    blocker = queue.enqueue(scene_command(6, 3))
    first = queue.enqueue(level_command(7, 2, 10))
    second = queue.enqueue(level_command(7, 2, 20))

    await blocker
    with pytest.raises(RakoCommandError):
        await first
    with pytest.raises(RakoCommandError):
        await second


async def test_cancelling_one_request_skips_only_that_one(queue, runner):
    blocker = queue.enqueue(level_command(9, 9, 1))
    doomed = queue.enqueue(level_command(7, 2, 10))
    survivor = queue.enqueue(level_command(3, 1, 30))

    doomed.cancel()
    await asyncio.sleep(0)  # the done callback runs on the next loop pass
    # The cancelled position is dropped rather than sent as a no-op.
    assert all(entry.key != (7, 2) for entry in queue._pending)

    await asyncio.gather(blocker, survivor)
    assert [(spec.room, spec.channel) for spec in runner.calls] == [(9, 9), (3, 1)]


async def test_a_position_survives_while_any_requester_still_wants_it(queue, runner):
    """Coalesced requests share a position; one giving up must not cancel it."""
    blocker = queue.enqueue(level_command(9, 9, 1))
    first = queue.enqueue(level_command(7, 2, 10))
    second = queue.enqueue(level_command(7, 2, 20))

    first.cancel()
    await blocker
    assert await second == ChannelStatusMessage(7, 2, 20)
    assert runner.levels == [1, 20]


async def test_cancelling_the_awaiting_task_withdraws_the_request(queue, runner):
    blocker = queue.enqueue(level_command(9, 9, 1))
    task = asyncio.create_task(queue.submit(level_command(7, 2, 10)))
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    await blocker
    await queue.drain()
    assert [(spec.room, spec.channel) for spec in runner.calls] == [(9, 9)]


# ---------------------------------------------------------------------------
# Diagnostics and lifecycle
# ---------------------------------------------------------------------------


async def test_depth_and_oldest_age_describe_the_backlog(queue, runner):
    queue.enqueue(level_command(9, 9, 1))
    queue.enqueue(level_command(7, 2, 10))
    queue.enqueue(level_command(3, 1, 30))
    assert queue.depth == 3
    assert queue.oldest_age is not None
    assert queue.oldest_age >= 0

    await queue.drain()
    assert queue.depth == 0
    assert queue.oldest_age is None
    assert queue.in_flight is False


async def test_stats_report_the_counters(queue, runner):
    blocker = queue.enqueue(level_command(9, 9, 1))
    queue.enqueue(level_command(7, 2, 10))
    queue.enqueue(level_command(7, 2, 20))
    await blocker
    await queue.drain()

    stats = queue.stats
    assert (stats.sent, stats.coalesced, stats.failed) == (2, 1, 0)
    assert stats.min_interval == INTERVAL
    assert stats.depth == 0
    assert stats.in_flight is False


async def test_drain_waits_for_everything_queued(queue, runner):
    for channel in range(3):
        queue.enqueue(level_command(7, channel, 10))
    await queue.drain()
    assert len(runner.calls) == 3


async def test_drain_returns_immediately_when_idle(queue):
    await queue.drain()


async def test_close_fails_the_pending_requests(queue, runner):
    runner.gate = asyncio.Event()
    in_flight = queue.enqueue(level_command(9, 9, 1))
    queued = queue.enqueue(level_command(7, 2, 10))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await queue.close()

    with pytest.raises(RakoQueueClosedError, match="closed"):
        await queued
    with pytest.raises(RakoQueueClosedError, match="in flight"):
        await in_flight
    # A closed-queue failure is still a command failure to anyone catching the
    # general case.
    assert issubclass(RakoQueueClosedError, RakoCommandError)


async def test_a_cancelled_worker_cancels_the_command_it_was_running(queue, runner):
    """Not a close: whoever cancelled the worker gets a cancellation, not a lie."""
    runner.gate = asyncio.Event()
    in_flight = queue.enqueue(level_command(9, 9, 1))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert queue.in_flight

    queue._worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await queue._worker
    assert in_flight.cancelled()


async def test_close_is_idempotent_and_refuses_new_work(queue):
    await queue.close()
    await queue.close()
    assert queue.closed
    with pytest.raises(RakoQueueClosedError):
        queue.enqueue(level_command(7, 2, 10))


async def test_lowering_the_interval_at_runtime_releases_a_waiting_request(runner):
    """A request already waiting picks up the new interval, not the old one."""
    queue = CommandQueue(runner, min_interval=10.0, name="test")
    try:
        first = queue.enqueue(level_command(7, 1, 10))
        second = queue.enqueue(level_command(7, 2, 20))
        await first
        queue.min_interval = 0.0
        await asyncio.wait_for(second, timeout=2.0)
        assert len(runner.calls) == 2
    finally:
        await queue.close()


async def test_raising_the_interval_at_runtime_delays_the_next_send(runner):
    queue = CommandQueue(runner, min_interval=0.0, name="test")
    try:
        await queue.submit(level_command(7, 1, 10))
        queue.min_interval = INTERVAL * 2
        await queue.submit(level_command(7, 2, 20))
        assert runner.gaps[0] >= INTERVAL * 2 * 0.95
    finally:
        await queue.close()


async def test_a_negative_interval_is_rejected(runner):
    with pytest.raises(ValueError, match="negative"):
        CommandQueue(runner, min_interval=-1)
    queue = CommandQueue(runner)
    with pytest.raises(ValueError, match="negative"):
        queue.min_interval = -0.5


async def test_options_are_passed_through_to_the_runner(queue, runner):
    await queue.submit(level_command(7, 2, 10), verify=False, retries=3)
    assert runner.options[0] == {"verify": False, "retries": 3}


# ---------------------------------------------------------------------------
# Bridge wiring
# ---------------------------------------------------------------------------


async def test_bridge_defaults_to_the_documented_interval():
    bridge = Bridge("192.0.2.10", 9761, "bridge", "mac")
    try:
        assert bridge.min_command_interval == DEFAULT_MIN_COMMAND_INTERVAL
        assert DEFAULT_MIN_COMMAND_INTERVAL == 1.5
    finally:
        await bridge.close()


async def test_bridge_paces_and_coalesces_its_set_calls(monkeypatch):
    bridge = Bridge("192.0.2.10", 9761, "bridge", "mac", min_command_interval=INTERVAL)
    runner = RecordingRunner()
    monkeypatch.setattr(bridge, "_execute", runner)
    monkeypatch.setattr(bridge._command_queue, "_runner", runner)
    try:
        # The drag starts: nothing is queued, so this one goes out at once.
        assert await bridge.set_channel_level(7, 2, 1) == ChannelStatusMessage(7, 2, 1)
        # The rest of the drag arrives while the queue is holding the interval.
        results = await asyncio.gather(
            *(bridge.set_channel_level(7, 2, level) for level in range(2, 12))
        )
        # One send carrying the final level, and every caller told about it.
        assert runner.levels == [1, 11]
        assert results == [ChannelStatusMessage(7, 2, 11)] * 10
        assert bridge.command_queue.stats.coalesced == 9
    finally:
        await bridge.close()


async def test_bridge_direct_path_skips_the_queue(monkeypatch):
    bridge = Bridge("192.0.2.10", 9761, "bridge", "mac", min_command_interval=10.0)
    runner = RecordingRunner()
    monkeypatch.setattr(bridge, "_execute", runner)
    monkeypatch.setattr(bridge._command_queue, "_runner", runner)
    try:
        await asyncio.gather(
            *(bridge.send_command(level_command(7, 2, level), paced=False) for level in (1, 2, 3))
        )
        assert runner.levels == [1, 2, 3]
        assert bridge.command_queue.stats.sent == 0
    finally:
        await bridge.close()


async def test_bridge_interval_is_settable_at_runtime():
    bridge = Bridge("192.0.2.10", 9761, "bridge", "mac")
    try:
        bridge.min_command_interval = 0.25
        assert bridge.command_queue.min_interval == 0.25
    finally:
        await bridge.close()


async def test_bridge_close_closes_the_queue(monkeypatch):
    bridge = Bridge("192.0.2.10", 9761, "bridge", "mac", min_command_interval=10.0)
    runner = RecordingRunner()
    runner.gate = asyncio.Event()
    monkeypatch.setattr(bridge, "_execute", runner)
    monkeypatch.setattr(bridge._command_queue, "_runner", runner)
    queued = bridge.command_queue.enqueue(level_command(7, 2, 10))
    pending = asyncio.create_task(bridge.set_channel_level(3, 1, 20))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await bridge.close()

    assert bridge.command_queue.closed
    with pytest.raises(RakoQueueClosedError):
        await queued
    with pytest.raises(RakoQueueClosedError):
        await pending
