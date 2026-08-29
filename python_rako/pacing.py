"""A paced, coalescing command queue -- one per bridge.

Home Assistant will happily ask for twenty level changes in the time it takes
to drag a slider, and a Rako bridge will not survive that.  Phase 0 observed
the failure directly: a command sent moments after another to the same channel
silently never took effect -- no light change, no broadcast, no error.  The
bridge simply dropped it.

So the library refuses to send faster than the bridge can accept.  Requests
that arrive too soon are *queued*, never dropped, and sent in order once the
interval has elapsed:

    submit -> queue (FIFO) -> wait until pacing allows -> send + verify

Two rules make that cheap rather than laggy:

**Same-target coalescing.**  While a command for ``(room, channel)`` is still
waiting, a newer command for the same target replaces it *in place*: it keeps
the original queue position and takes the new payload.  A slider drag of twenty
levels therefore becomes a single send of the final level, and no request ever
waits behind a value that is already obsolete.  Different kinds of command
coalesce too (a scene selection replaces a pending level for the same target
and vice versa) because the bridge only honours whichever arrives last anyway.

**One in-flight verified command.**  The next send waits for
``max(previous send + min_interval, previous echo-or-failure)``.  Echo
verification (:mod:`python_rako.commands`) takes 150-300 ms on a good day and
up to two verify windows on a bad one, and overlapping that with the next send
is exactly the pattern that lost a command in Phase 0.

The release exemption
---------------------
A fade is a *gesture*, not a value: FADE_UP/FADE_DOWN is the button press and
STOP is the release, and the release is what decides how far the circuit got.
Pacing the release would rewrite the user's intent -- a 200 ms tap on a dimmer
would become a 1.5 s sweep -- so a STOP for the target of the most recently
sent fade is dispatched as soon as the worker is free, ignoring the interval,
and ahead of anything else queued.  Everything else about it is normal: it
still waits for the fade's own verification (one command in flight at a time),
and the *next* command is paced from the moment the STOP was sent.

Two coalescing rules protect the pair, because losing either half of a gesture
leaves a fade running or never starts one:

* a pending STOP is never replaced by a later command -- the release must
  reach the bridge;
* a STOP never replaces a pending fade for the same target -- otherwise a
  press/release submitted while the queue is busy would collapse into a stop
  for a fade that never ran.

The fade itself is paced like anything else.

The superseded contract
-----------------------
Callers await a result, so a coalesced-away request has to resolve somehow.  It
resolves with the outcome of the command that *replaced* it -- the echo the
bridge sent for the value it actually applied, or the exception that value
failed with.  ``await bridge.set_channel_level(...)`` therefore always returns
a truthful description of where the channel ended up, never a level that was
overtaken before it left the queue and never a silent ``None``.

Pacing default
--------------
:data:`DEFAULT_MIN_COMMAND_INTERVAL` is 1.5 s, *assumed pending live
measurement*: it is the verify window from ``BRIDGE_BEHAVIOUR.md`` fact 14,
which is known to be safe, not a measured minimum.  ``scripts/measure_interval.py``
finds the real floor against a live bridge; lower the interval once it has.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from python_rako.const import CommandType
from python_rako.exceptions import RakoQueueClosedError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from python_rako.commands import CommandSpec
    from python_rako.model import StatusMessage

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MIN_COMMAND_INTERVAL",
    "CommandQueue",
    "CommandQueueStats",
]

#: Minimum seconds between consecutive sends to one bridge.
#:
#: **Assumed pending live measurement.**  1.5 s is the echo-verify window (see
#: ``BRIDGE_BEHAVIOUR.md`` fact 14: echo in 144-306 ms, ack in 677-770 ms), so
#: it is comfortably above anything the bridge has been seen to need -- but the
#: true minimum safe spacing has not been measured yet.  Run
#: ``scripts/measure_interval.py`` against a live bridge to find it.
DEFAULT_MIN_COMMAND_INTERVAL = 1.5

#: Runs one command and returns the bridge's echo (or ``None`` if unverified).
#: Deliberately ``...``-typed: the queue passes each request's options straight
#: through to the runner without knowing what they mean.
type CommandRunner = Callable[..., Awaitable[StatusMessage | None]]

#: A command's target. Everything queued for the same target coalesces.
type TargetKey = tuple[int, int]

#: The press half of a fade gesture. STOP is the release (see the module
#: docstring): FADE_UP/FADE_DOWN are the keypad-style press/release pair, and
#: FADE (0x32) is the parameterised form, which STOP also terminates.
_FADE_COMMANDS = frozenset({CommandType.FADE_UP, CommandType.FADE_DOWN, CommandType.FADE})


@dataclass(frozen=True)
class CommandQueueStats:
    """A point-in-time view of a queue, for diagnostics and health reporting."""

    #: Requests waiting to be sent (excludes the one in flight).
    depth: int
    #: Seconds since the oldest waiting request was first submitted, or ``None``.
    oldest_age: float | None
    #: Whether a command is on the wire or awaiting its echo right now.
    in_flight: bool
    #: Commands actually put on the wire.
    sent: int
    #: Requests absorbed into a newer command for the same target.
    coalesced: int
    #: Commands the bridge never confirmed (after their retries).
    failed: int
    #: The interval currently being enforced, in seconds.
    min_interval: float


@dataclass(eq=False)
class _QueueEntry:
    """One queue position, which several requests may end up sharing.

    ``eq=False`` so entries compare by identity: two requests with the same
    spec are still two distinct positions, and ``deque.remove`` must not delete
    the wrong one.
    """

    key: TargetKey
    spec: CommandSpec
    options: dict[str, Any]
    enqueued_at: float
    waiters: list[asyncio.Future[StatusMessage | None]] = field(default_factory=list)

    @property
    def live_waiters(self) -> list[asyncio.Future[StatusMessage | None]]:
        return [waiter for waiter in self.waiters if not waiter.done()]

    def settle_result(self, result: StatusMessage | None) -> None:
        for waiter in self.live_waiters:
            waiter.set_result(result)

    def settle_exception(self, error: BaseException) -> None:
        for waiter in self.live_waiters:
            waiter.set_exception(error)

    def cancel(self) -> None:
        for waiter in self.live_waiters:
            waiter.cancel()


class CommandQueue:
    """Paces and coalesces commands to one bridge.

    Construct it with a ``runner`` -- a coroutine function taking a
    :class:`~python_rako.commands.CommandSpec` plus whatever keyword options
    the caller passed to :meth:`submit`, and returning the bridge's echo.
    :class:`~python_rako.bridge.Bridge` builds one for you; use this directly
    only if you are driving a bridge without it.

    The worker task starts on first use and stops on :meth:`close`, so a queue
    can be constructed outside a running event loop.
    """

    def __init__(
        self,
        runner: CommandRunner,
        *,
        min_interval: float = DEFAULT_MIN_COMMAND_INTERVAL,
        name: str = "rako",
    ) -> None:
        self._runner = runner
        self._min_interval = self._validated(min_interval)
        self._name = name
        self._pending: deque[_QueueEntry] = deque()
        self._in_flight: _QueueEntry | None = None
        self._worker: asyncio.Task[None] | None = None
        self._closed = False
        # Set whenever the worker needs to re-examine the world: a new request,
        # an interval change, a close. Waiting on it (rather than sleeping a
        # fixed span) is what makes a runtime interval change take effect on
        # the request that is already waiting.
        self._wakeup = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        # -inf so the first command goes out immediately.
        self._last_send_started = float("-inf")
        self._last_completed = float("-inf")
        #: Target of the most recently sent fade, whose release skips pacing.
        self._fade_target: TargetKey | None = None
        self._sent = 0
        self._coalesced = 0
        self._failed = 0

    # -- configuration -----------------------------------------------------

    @staticmethod
    def _validated(min_interval: float) -> float:
        if min_interval < 0:
            raise ValueError("min_interval must not be negative")
        return float(min_interval)

    @property
    def min_interval(self) -> float:
        """Minimum seconds between consecutive sends. Adjustable at runtime."""
        return self._min_interval

    @min_interval.setter
    def min_interval(self, value: float) -> None:
        self._min_interval = self._validated(value)
        # A request may already be waiting on the old interval; the pacing
        # deadline is recomputed from the interval each time round the worker
        # loop, so all that is needed is to wake it up.
        self._wakeup.set()

    # -- diagnostics -------------------------------------------------------

    @property
    def depth(self) -> int:
        """Requests waiting to be sent, not counting the one in flight."""
        return len(self._pending)

    @property
    def in_flight(self) -> bool:
        """Whether a command is on the wire or awaiting its echo."""
        return self._in_flight is not None

    @property
    def oldest_age(self) -> float | None:
        """Seconds the oldest waiting request has been queued, or ``None``.

        Measured from when that queue *position* was created, so a request
        coalesced into it does not reset the clock -- the number answers "how
        far behind is this bridge", which is what a health check wants.
        """
        if not self._pending:
            return None
        return self._loop_time() - self._pending[0].enqueued_at

    @property
    def sent(self) -> int:
        """Commands actually put on the wire."""
        return self._sent

    @property
    def coalesced(self) -> int:
        """Requests absorbed into a newer command for the same target."""
        return self._coalesced

    @property
    def failed(self) -> int:
        """Commands that raised (typically an unconfirmed :class:`RakoCommandError`)."""
        return self._failed

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stats(self) -> CommandQueueStats:
        return CommandQueueStats(
            depth=self.depth,
            oldest_age=self.oldest_age,
            in_flight=self.in_flight,
            sent=self._sent,
            coalesced=self._coalesced,
            failed=self._failed,
            min_interval=self._min_interval,
        )

    # -- submission --------------------------------------------------------

    async def submit(self, spec: CommandSpec, **options: Any) -> StatusMessage | None:
        """Queue ``spec`` and wait for the result of the command that runs.

        Returns the bridge's echo -- possibly the echo of a *later* command for
        the same target that superseded this one (see the module docstring).

        :raises RakoCommandError: the command that ran was never confirmed.
        :raises RakoQueueClosedError: the queue was closed before it ran.
        """
        return await self.enqueue(spec, **options)

    def enqueue(self, spec: CommandSpec, **options: Any) -> asyncio.Future[StatusMessage | None]:
        """Queue ``spec`` without waiting, returning the future to await.

        Cancelling the returned future withdraws the request; once every
        request sharing a queue position has been cancelled, that position is
        dropped and never sent.
        """
        if self._closed:
            raise RakoQueueClosedError(f"command queue for {self._name} is closed")
        loop = asyncio.get_running_loop()
        self._ensure_worker(loop)
        future: asyncio.Future[StatusMessage | None] = loop.create_future()
        key: TargetKey = (spec.room, spec.channel)

        existing = self._find_coalescible(key, spec)
        if existing is not None:
            # Keep the position, take the new payload: the bridge only honours
            # the last command to a target, so sending the earlier one would
            # spend a pacing slot on a value nobody wants any more.
            _LOGGER.debug(
                "Coalescing %s into queued %s for room %d channel %d",
                spec.command.name,
                existing.spec.command.name,
                key[0],
                key[1],
            )
            existing.spec = spec
            existing.options = options
            existing.waiters.append(future)
            self._coalesced += 1
        else:
            entry = _QueueEntry(
                key=key,
                spec=spec,
                options=options,
                enqueued_at=loop.time(),
                waiters=[future],
            )
            self._pending.append(entry)
            self._idle.clear()
            existing = entry

        entry_ref = existing
        future.add_done_callback(lambda _f: self._discard_if_abandoned(entry_ref))
        self._wakeup.set()
        return future

    def _find_coalescible(self, key: TargetKey, spec: CommandSpec) -> _QueueEntry | None:
        """The waiting entry ``spec`` should replace, if any.

        Everything for one target coalesces except the two halves of a fade
        gesture: a queued release is never replaced, and a release never
        replaces a queued press.  Either would drop half of a press/release
        pair, leaving a fade running or never starting one.
        """
        for entry in self._pending:
            if entry.key != key:
                continue
            if entry.spec.command is CommandType.STOP_FADING:
                return None
            if spec.command is CommandType.STOP_FADING and entry.spec.command in _FADE_COMMANDS:
                return None
            return entry
        return None

    def _discard_if_abandoned(self, entry: _QueueEntry) -> None:
        """Drop a queue position whose every requester has cancelled."""
        if entry.live_waiters or not any(waiter.cancelled() for waiter in entry.waiters):
            return
        with contextlib.suppress(ValueError):
            self._pending.remove(entry)
        if not self._pending and self._in_flight is None:
            self._idle.set()

    # -- lifecycle ---------------------------------------------------------

    def _ensure_worker(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._work(), name=f"rako-command-queue-{self._name}")

    async def drain(self) -> None:
        """Wait until nothing is queued and nothing is in flight.

        For tests and for an orderly shutdown that lets queued commands land.
        """
        while self._pending or self._in_flight is not None:
            await self._idle.wait()

    async def close(self) -> None:
        """Stop the queue; every unsent request fails with a clear exception.

        Idempotent.  Requests still queued -- and one in flight -- raise
        :class:`~python_rako.exceptions.RakoQueueClosedError` rather than
        hanging forever on a queue that will never run again.
        """
        if self._closed:
            return
        self._closed = True
        self._wakeup.set()

        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

        while self._pending:
            entry = self._pending.popleft()
            entry.settle_exception(
                RakoQueueClosedError(
                    f"command queue for {self._name} closed before "
                    f"{entry.spec.command.name} was sent"
                )
            )
        self._in_flight = None
        self._idle.set()

    # -- the worker --------------------------------------------------------

    def _loop_time(self) -> float:
        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:  # pragma: no cover - diagnostics off the loop
            return 0.0

    @property
    def _next_allowed_at(self) -> float:
        """When the next send may happen.

        ``max(previous send + interval, previous completion)``: never faster
        than the interval, and never overlapping a command still waiting for
        its echo.  Computed rather than stored so that changing
        :attr:`min_interval` takes effect on the request already waiting.
        """
        return max(self._last_send_started + self._min_interval, self._last_completed)

    async def _work(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._closed:
            self._wakeup.clear()

            if not self._pending:
                self._idle.set()
                await self._wakeup.wait()
                continue

            release = self._pop_pending_release()
            if release is not None:
                # The release of a fade that is already running: pacing it
                # would lengthen the fade the user asked for. It still waits
                # for the press's own verification, because _run is
                # sequential, and it re-anchors pacing for what follows.
                _LOGGER.debug("Dispatching %s ahead of pacing", release.spec)
                await self._run(release, loop)
                continue

            entry = self._pending[0]
            if not entry.live_waiters:
                # Everyone who wanted this gave up while it waited.
                self._pending.popleft()
                continue

            remaining = self._next_allowed_at - loop.time()
            if remaining > 0:
                # Waiting on the event rather than sleeping flat means a newer
                # command for this target can still coalesce into the entry,
                # and a lowered interval is picked up immediately.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wakeup.wait(), remaining)
                continue

            self._pending.popleft()
            await self._run(entry, loop)

    def _pop_pending_release(self) -> _QueueEntry | None:
        """Take the release of the fade that is currently running, if queued.

        Scans the whole queue rather than just its head: a release held behind
        another target's command would be delayed by a full interval, which is
        the delay this rule exists to remove.  It is the one departure from
        FIFO, and it can happen at most once per fade because
        :attr:`_fade_target` is cleared by the next send.
        """
        if self._fade_target is None:
            return None
        for index, entry in enumerate(self._pending):
            if (
                entry.key == self._fade_target
                and entry.spec.command is CommandType.STOP_FADING
                and entry.live_waiters
            ):
                del self._pending[index]
                return entry
        return None

    async def _run(self, entry: _QueueEntry, loop: asyncio.AbstractEventLoop) -> None:
        """Send one command and hand its outcome to everyone waiting on it.

        A failure is delivered to its own requesters and nothing else: the
        queue must not stall because one command went unconfirmed.
        """
        self._in_flight = entry
        self._idle.clear()
        self._last_send_started = loop.time()
        # A fade arms the release exemption for its target; anything else
        # sent -- including the release itself -- disarms it, so exactly one
        # STOP per fade skips the queue.
        self._fade_target = entry.key if entry.spec.command in _FADE_COMMANDS else None
        try:
            result = await self._runner(entry.spec, **entry.options)
        except asyncio.CancelledError:
            if self._closed:
                entry.settle_exception(
                    RakoQueueClosedError(
                        f"command queue for {self._name} closed while "
                        f"{entry.spec.command.name} was in flight"
                    )
                )
            else:
                entry.cancel()
            raise
        except Exception as err:
            self._failed += 1
            _LOGGER.debug("Queued %s failed: %s", entry.spec, err)
            entry.settle_exception(err)
        else:
            self._sent += 1
            entry.settle_result(result)
        finally:
            self._last_completed = loop.time()
            self._in_flight = None
            if not self._pending:
                self._idle.set()
