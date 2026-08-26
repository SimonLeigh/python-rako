from __future__ import annotations

import asyncio
from typing import Any

import asyncio_dgram
import pytest

from python_rako import discover_bridge
from python_rako.exceptions import RakoDiscoveryError


class _FakeDgServer:
    """Stand-in for asyncio_dgram's DatagramServer.

    Avoids touching a real socket (and its broadcast permissions) in tests
    while exercising discover_bridge's own send/recv/close/timeout logic.
    """

    def __init__(
        self,
        recv_result: tuple[bytes, tuple[str, int]] | None = None,
        recv_delay: float = 0.0,
    ):
        self.sent: list[tuple[bytes, Any]] = []
        self.closed = False
        self._recv_result = recv_result
        self._recv_delay = recv_delay

    async def send(self, data: bytes, addr: Any = None) -> None:
        self.sent.append((data, addr))

    async def recv(self) -> tuple[bytes, tuple[str, int]]:
        if self._recv_delay:
            await asyncio.sleep(self._recv_delay)
        assert self._recv_result is not None
        return self._recv_result

    def close(self) -> None:
        self.closed = True


def _patch_from_socket(monkeypatch: pytest.MonkeyPatch, fake_server: _FakeDgServer) -> None:
    async def _from_socket(sock: Any) -> _FakeDgServer:
        return fake_server

    monkeypatch.setattr(asyncio_dgram, "from_socket", _from_socket)


@pytest.mark.asyncio
async def test_discover_bridge_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDgServer(recv_result=(b"RAKOBRIDGE 00:11:22:33:44:55", ("203.0.113.50", 9761)))
    _patch_from_socket(monkeypatch, fake)

    result = await discover_bridge()

    assert result == {
        "host": "203.0.113.50",
        "port": 9761,
        "name": "RAKOBRIDGE",
        "mac": "00:11:22:33:44:55",
    }
    assert fake.sent == [(b"D", ("255.255.255.255", 9761))]
    assert fake.closed is True


@pytest.mark.asyncio
async def test_discover_bridge_timeout_raises_rako_discovery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDgServer(recv_delay=10)
    _patch_from_socket(monkeypatch, fake)

    with pytest.raises(RakoDiscoveryError):
        await discover_bridge(timeout=0.05)

    # Socket must be closed even when discovery times out.
    assert fake.closed is True


@pytest.mark.asyncio
async def test_discover_bridge_garbled_reply_raises_rako_discovery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDgServer(recv_result=(b"not-a-valid-discovery-reply", ("203.0.113.50", 9761)))
    _patch_from_socket(monkeypatch, fake)

    with pytest.raises(RakoDiscoveryError):
        await discover_bridge()

    assert fake.closed is True


@pytest.mark.asyncio
async def test_discover_bridge_closes_socket_on_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDgServer()  # recv() will raise AssertionError (no recv_result configured)
    _patch_from_socket(monkeypatch, fake)

    with pytest.raises(AssertionError):
        await discover_bridge()

    assert fake.closed is True
