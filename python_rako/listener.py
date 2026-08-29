"""A supervised UDP status listener for the Rako bridge.

The bridge broadcasts a status message for every state change it performs, and
those broadcasts are the *only* live source of true dimmer levels: there is no
way to read a circuit's current level back (``Accessing the Rako Bridge``
v2.2.2, p.8).  A listener that dies silently therefore leaves a consumer
showing stale state forever -- the primary divergence mechanism identified in
Phase 0.

:class:`StatusListener` is built around that: the receive loop is supervised,
any exception restarts it with jittered exponential backoff, and health is
observable through :attr:`StatusListener.health` so a consumer can mark
entities unavailable rather than lie about them.

Two behaviours are dictated by observation rather than the specification:

* The socket sets ``SO_REUSEADDR`` **and** ``SO_REUSEPORT`` where available, so
  a development and a production instance can listen on the same host at the
  same time.  A listener that hogs port 9761 blocks every other consumer.
* The bridge itself re-broadcasts some keypad events roughly 200 ms apart, so
  identical messages inside :attr:`dedupe_window` are suppressed.  Without this
  a single button press fans out twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

import asyncio_dgram

from python_rako.const import RAKO_BRIDGE_DEFAULT_PORT
from python_rako.model import StatusMessage
from python_rako.protocol import NonStatusPacket, decode_packet

_LOGGER = logging.getLogger(__name__)

__all__ = ["ListenerHealth", "StatusListener"]

#: Identical messages closer together than this are treated as one event.
DEFAULT_DEDUPE_WINDOW = 0.3
DEFAULT_BACKOFF_INITIAL = 0.5
DEFAULT_BACKOFF_MAX = 30.0

MessageCallback = Callable[[StatusMessage], Any | Awaitable[Any]]


@dataclass
class ListenerHealth:
    """The listener's health and diagnostic counters.

    A listener keeps exactly one of these and mutates it in place; reading
    :attr:`StatusListener.health` hands back a copy, so a value you stored will
    not change under you.
    """

    is_running: bool = False
    last_message_at: float | None = None
    restart_count: int = 0
    last_error: str | None = None
    #: Broadcasts dropped because the bridge re-sent the same event.
    suppressed_duplicates: int = 0
    #: Datagrams discarded because they came from some other host.
    ignored_packets: int = 0
    #: Datagrams from the bridge that were not status broadcasts.
    non_status_packets: int = 0
    #: Status messages accepted, after de-duplication.
    messages_received: int = 0

    @property
    def seconds_since_last_message(self) -> float | None:
        if self.last_message_at is None:
            return None
        return time.monotonic() - self.last_message_at


@dataclass
class _Subscription:
    callback: MessageCallback
    is_async: bool = field(default=False)
    #: Echo verification subscribes this way: it must see a message even when
    #: an identical one arrived moments earlier, or a command's confirmation
    #: can be swallowed by de-duplication.
    include_duplicates: bool = False


class StatusListener:
    """Receive, decode, de-duplicate and fan out Rako status broadcasts.

    ::

        async with StatusListener("192.0.2.10") as listener:
            listener.subscribe(handle)
            async for message in listener.messages():
                ...

    :param bridge_host: only datagrams from this address are accepted.  The
        bridge sends status broadcasts from an *ephemeral* source port, not
        9761, so the port is deliberately not part of the filter.
    :param port: local port to bind; 0 picks an ephemeral port, which is what
        the tests use.
    :param dedupe_window: seconds within which an identical message is treated
        as a re-broadcast of the same event.  Set to 0 to disable.
    """

    def __init__(
        self,
        bridge_host: str,
        port: int = RAKO_BRIDGE_DEFAULT_PORT,
        *,
        listen_host: str = "0.0.0.0",  # must accept broadcasts
        dedupe_window: float = DEFAULT_DEDUPE_WINDOW,
        backoff_initial: float = DEFAULT_BACKOFF_INITIAL,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        on_health_change: Callable[[ListenerHealth], Any] | None = None,
        strict_crc: bool = False,
        queue_maxsize: int = 1000,
    ) -> None:
        self.bridge_host = bridge_host
        self.port = port
        self.listen_host = listen_host
        self.dedupe_window = dedupe_window
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.strict_crc = strict_crc
        self.queue_maxsize = queue_maxsize
        self._on_health_change = on_health_change

        self._task: asyncio.Task[None] | None = None
        self._endpoint: Any | None = None
        self._stopping = asyncio.Event()
        self._subscriptions: list[_Subscription] = []
        self._queues: list[asyncio.Queue[StatusMessage]] = []
        self._callback_tasks: set[asyncio.Task[Any]] = set()
        self._recent: dict[tuple[Any, ...], float] = {}
        self._local_port: int | None = None
        self._health = ListenerHealth()

    # -- health ------------------------------------------------------------

    @property
    def health(self) -> ListenerHealth:
        """A copy of the listener's health and counters.

        Everything diagnostic lives here -- ``restart_count``,
        ``suppressed_duplicates``, ``ignored_packets``, ``non_status_packets``,
        ``messages_received``, ``last_error`` -- rather than being mirrored
        across a dozen properties.
        """
        return replace(self._health)

    @property
    def is_running(self) -> bool:
        """Whether the socket is currently bound and receiving."""
        return self._health.is_running

    @property
    def last_message_at(self) -> float | None:
        """``time.monotonic()`` of the last accepted status message."""
        return self._health.last_message_at

    @property
    def restart_count(self) -> int:
        """How many times the receive loop has been rebuilt after an error."""
        return self._health.restart_count

    @property
    def local_port(self) -> int | None:
        """The port actually bound; useful when constructed with ``port=0``."""
        return self._local_port

    def _publish_health(self) -> None:
        if self._on_health_change is None:
            return
        try:
            self._on_health_change(self.health)
        except Exception:  # pragma: no cover - defensive
            _LOGGER.exception("Rako listener health callback failed")

    def _set_running(self, running: bool, error: str | None = None) -> None:
        if running == self._health.is_running and error == self._health.last_error:
            return
        self._health.is_running = running
        self._health.last_error = error
        self._publish_health()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the supervised receive loop.

        Returns once the first bind attempt has completed, so a caller that
        immediately sends a command will not race the socket into existence.
        """
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        bound = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(
            self._supervise(bound), name=f"rako-listener-{self.bridge_host}"
        )
        with contextlib.suppress(asyncio.CancelledError):
            await bound

    async def stop(self) -> None:
        """Stop the receive loop and release the socket."""
        self._stopping.set()
        task, self._task = self._task, None
        self._close_endpoint()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for pending in list(self._callback_tasks):
            pending.cancel()
        self._callback_tasks.clear()
        self._set_running(False)

    async def __aenter__(self) -> StatusListener:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    # -- subscription ------------------------------------------------------

    def subscribe(
        self, callback: MessageCallback, *, include_duplicates: bool = False
    ) -> Callable[[], None]:
        """Register ``callback`` for every accepted message.

        The callback may be sync or async.  A callback that raises is logged
        and skipped: it can never take down the loop or the other subscribers.
        Returns a handle that unsubscribes when called.

        Pass ``include_duplicates=True`` to receive messages the de-duplication
        window would otherwise suppress.  Echo verification needs this: two
        identical broadcasts inside the window may be one keypad event repeated
        *or* the confirmation of a command we just sent, and dropping the second
        would make a command that worked look like one that failed.
        """
        subscription = _Subscription(
            callback,
            is_async=asyncio.iscoroutinefunction(callback),
            include_duplicates=include_duplicates,
        )
        self._subscriptions.append(subscription)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subscriptions.remove(subscription)

        return unsubscribe

    async def messages(self) -> AsyncIterator[StatusMessage]:
        """Iterate messages as they arrive.

        The queue is bounded; if a consumer falls behind, the *oldest* messages
        are dropped in favour of the newest, because stale state is worse than
        missing state here.
        """
        queue: asyncio.Queue[StatusMessage] = asyncio.Queue(maxsize=self.queue_maxsize)
        self._queues.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            with contextlib.suppress(ValueError):
                self._queues.remove(queue)

    # -- socket ------------------------------------------------------------

    def _create_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # SO_REUSEPORT lets several processes on one host bind 9761 together;
        # without it a second Home Assistant instance cannot listen at all.
        reuse_port = getattr(socket, "SO_REUSEPORT", None)
        if reuse_port is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, reuse_port, 1)
            except OSError:  # pragma: no cover - platform dependent
                _LOGGER.debug("SO_REUSEPORT unavailable on this platform")
        sock.bind((self.listen_host, self.port))
        return sock

    async def _open_endpoint(self) -> Any:
        """Bind the socket and wrap it for asyncio. Overridable for testing."""
        sock = self._create_socket()
        self._local_port = sock.getsockname()[1]
        return await asyncio_dgram.from_socket(sock)

    def _close_endpoint(self) -> None:
        endpoint, self._endpoint = self._endpoint, None
        if endpoint is not None:
            with contextlib.suppress(Exception):
                endpoint.close()

    # -- supervision -------------------------------------------------------

    async def _supervise(self, bound: asyncio.Future[None]) -> None:
        backoff = self.backoff_initial
        first_attempt = True
        while not self._stopping.is_set():
            try:
                self._endpoint = await self._open_endpoint()
                _LOGGER.debug(
                    "Rako listener bound on %s:%s, accepting from %s",
                    self.listen_host,
                    self._local_port,
                    self.bridge_host,
                )
                self._set_running(True)
                backoff = self.backoff_initial
                if first_attempt and not bound.done():
                    bound.set_result(None)
                    first_attempt = False
                await self._receive_forever()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # supervision is the whole point
                self._health.last_error = f"{type(err).__name__}: {err}"
                _LOGGER.exception("Rako listener failed; restarting")
            finally:
                self._close_endpoint()
                self._set_running(False, self._health.last_error)

            if first_attempt and not bound.done():
                bound.set_result(None)
                first_attempt = False
            if self._stopping.is_set():
                break

            self._health.restart_count += 1
            # Jitter so several listeners recovering from the same network
            # event do not all retry in lockstep.
            delay = min(backoff, self.backoff_max) * (0.5 + random.random())  # noqa: S311
            _LOGGER.warning(
                "Rako listener restarting in %.2fs (restart #%d)",
                delay,
                self._health.restart_count,
            )
            self._publish_health()
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                break  # stop() was called while we were backing off
            except TimeoutError:
                pass
            backoff = min(backoff * 2, self.backoff_max)

    async def _receive_forever(self) -> None:
        assert self._endpoint is not None
        while not self._stopping.is_set():
            data, addr = await self._endpoint.recv()
            self._handle_datagram(bytes(data), addr)

    # -- datagram handling -------------------------------------------------

    def _handle_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        remote_host = addr[0] if addr else None
        if remote_host != self.bridge_host:
            # Phones running the Rako app broadcast discovery pings here; count
            # them so "nothing is arriving" can be told from "the wrong things
            # are arriving".
            self._health.ignored_packets += 1
            _LOGGER.debug("Ignoring datagram from %s", remote_host)
            return

        try:
            packet = decode_packet(data, strict=self.strict_crc)
        except Exception:  # a bad packet must never kill the receive loop
            _LOGGER.exception("Failed to decode Rako datagram %s", list(data))
            return

        if isinstance(packet, NonStatusPacket):
            self._health.non_status_packets += 1
            _LOGGER.debug("Non-status packet from bridge: %s", packet)
            return

        now = time.monotonic()
        self._health.last_message_at = now
        if self._is_duplicate(packet, now):
            self._health.suppressed_duplicates += 1
            _LOGGER.debug("Suppressed duplicate broadcast: %s", packet)
            # Still offered to subscribers that asked for duplicates, so a
            # pending echo waiter can be satisfied by it.
            self._dispatch(packet, duplicate=True)
            return

        self._health.messages_received += 1
        self._dispatch(packet)

    def _is_duplicate(self, message: StatusMessage, now: float) -> bool:
        if self.dedupe_window <= 0:
            return False
        key = message.dedupe_key
        previous = self._recent.get(key)
        self._recent[key] = now
        if len(self._recent) > 512:
            cutoff = now - self.dedupe_window
            self._recent = {k: t for k, t in self._recent.items() if t >= cutoff}
            self._recent[key] = now
        return previous is not None and (now - previous) < self.dedupe_window

    def _dispatch(self, message: StatusMessage, *, duplicate: bool = False) -> None:
        if not duplicate:
            for queue in self._queues:
                if queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(message)

        for subscription in list(self._subscriptions):
            if duplicate and not subscription.include_duplicates:
                continue
            if subscription.is_async:
                self._spawn(subscription.callback(message))  # type: ignore[arg-type]
                continue
            try:
                subscription.callback(message)
            except Exception:
                # One bad subscriber must not break the others or the loop.
                _LOGGER.exception("Rako status subscriber raised; continuing")

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._callback_tasks.add(task)
        task.add_done_callback(self._callback_tasks.discard)
        task.add_done_callback(_log_task_exception)


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    if task.exception() is not None:
        _LOGGER.error(
            "Rako status subscriber raised; continuing",
            exc_info=task.exception(),
        )
