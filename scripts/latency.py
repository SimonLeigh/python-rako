#!/usr/bin/env python3
"""Measure send->echo and send->ack latency for verified commands.

Reproduces the BRIDGE_BEHAVIOUR.md fact-14 measurement (echo 144-306ms, AOK
677-770ms) against any bridge, on demand. **Changes light state** on the
given channel repeatedly, so it refuses to run without
``--i-know-this-changes-lights``.

The bridge's own acknowledgement is diagnostics-only and is not timestamped
by the library once an echo has verified a command (`CommandSender.on_verified`
abandons the read -- see python_rako/commands.py), so this script talks to
`UdpCommandSender` directly and polls its `ack_count`/`error_ack_count`
counters to infer when the AOK arrived, rather than going through
`Bridge.set_channel_level`.

    RAKO_BRIDGE_HOST=192.0.2.10 python scripts/latency.py \\
        --room 7 --channel 2 --count 10 --i-know-this-changes-lights
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import statistics
import sys
import time

import aiohttp

from python_rako import (
    RAKO_BRIDGE_DEFAULT_PORT,
    Bridge,
    BridgeDescription,
    StatusListener,
    discover_bridge,
)
from python_rako.commands import CommandSpec, UdpCommandSender, level_command

_LOGGER = logging.getLogger("rako.scripts.latency")

#: Bound on how long we poll for an AOK before giving up on this sample.
_ACK_POLL_TIMEOUT = 3.0
_ACK_POLL_INTERVAL = 0.01
_ECHO_TIMEOUT = 3.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--host", default=os.environ.get("RAKO_BRIDGE_HOST"), help="bridge IP/hostname"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("RAKO_BRIDGE_PORT", RAKO_BRIDGE_DEFAULT_PORT)),
        help="bridge UDP port (default: %(default)s)",
    )
    parser.add_argument("--room", type=int, default=_int_env("RAKO_TEST_ROOM"), help="target room")
    parser.add_argument(
        "--channel", type=int, default=_int_env("RAKO_TEST_CHANNEL"), help="target channel"
    )
    parser.add_argument(
        "--count", type=int, default=10, help="number of commands to send (default: %(default)s)"
    )
    parser.add_argument(
        "--i-know-this-changes-lights",
        action="store_true",
        dest="confirmed",
        help="required: this script repeatedly toggles the chosen channel",
    )
    return parser.parse_args()


def _int_env(name: str) -> int | None:
    value = os.environ.get(name)
    return int(value) if value is not None else None


async def resolve_bridge(args: argparse.Namespace) -> BridgeDescription:
    if args.host:
        return BridgeDescription(host=args.host, port=args.port, name="", mac="")
    print("No --host/RAKO_BRIDGE_HOST given; broadcasting discovery...", file=sys.stderr)
    description = await discover_bridge()
    print(
        f"Discovered {description['name']} at {description['host']}:{description['port']} "
        f"({description['mac']})",
        file=sys.stderr,
    )
    return description


async def measure_once(
    sender: UdpCommandSender, listener: StatusListener, spec: CommandSpec
) -> tuple[float | None, float | None]:
    """Send ``spec`` once; return (echo_latency, ack_latency) in seconds."""
    loop = asyncio.get_running_loop()
    echo_future: asyncio.Future = loop.create_future()

    def on_message(message: object) -> None:
        if not echo_future.done() and spec.match(message):  # type: ignore[arg-type]
            echo_future.set_result(message)

    unsubscribe = listener.subscribe(on_message, include_duplicates=True)
    try:
        acks_before = sender.ack_count + sender.error_ack_count
        start = time.monotonic()
        await sender.send(spec)

        echo_latency: float | None
        try:
            await asyncio.wait_for(echo_future, timeout=_ECHO_TIMEOUT)
            echo_latency = time.monotonic() - start
        except TimeoutError:
            echo_latency = None

        ack_latency: float | None = None
        deadline = time.monotonic() + _ACK_POLL_TIMEOUT
        while time.monotonic() < deadline:
            if sender.ack_count + sender.error_ack_count > acks_before:
                ack_latency = time.monotonic() - start
                break
            await asyncio.sleep(_ACK_POLL_INTERVAL)

        return echo_latency, ack_latency
    finally:
        unsubscribe()


def _summarise(label: str, samples: list[float]) -> None:
    if not samples:
        print(f"{label}: no samples arrived")
        return
    print(
        f"{label} ({len(samples)} samples): "
        f"min={min(samples) * 1000:.0f}ms "
        f"median={statistics.median(samples) * 1000:.0f}ms "
        f"max={max(samples) * 1000:.0f}ms"
    )


async def main() -> None:
    args = parse_args()
    if not args.confirmed:
        print(
            "Refusing to run: pass --i-know-this-changes-lights (this script "
            "toggles a real light repeatedly).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if args.room is None or args.channel is None:
        print(
            "Give --room/--channel, or set RAKO_TEST_ROOM/RAKO_TEST_CHANNEL.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    bridge_description = await resolve_bridge(args)
    host, port = bridge_description["host"], bridge_description["port"]

    async with aiohttp.ClientSession() as session, Bridge(**bridge_description) as read_bridge:
        snapshot = await read_bridge.get_state_snapshot(session)
        previous_level = snapshot.channel_level(args.room, args.channel)
    if previous_level is None:
        previous_level = 255  # unknown (e.g. fade-controlled); a safe default to restore to

    sender = UdpCommandSender(host, port)
    echo_latencies: list[float] = []
    ack_latencies: list[float] = []
    try:
        async with StatusListener(host, port) as listener:
            levels = [255, 128]
            for i in range(args.count):
                level = levels[i % 2]
                spec = level_command(args.room, args.channel, level)
                echo_latency, ack_latency = await measure_once(sender, listener, spec)
                echo_text = "no echo" if echo_latency is None else f"{echo_latency * 1000:.0f}ms"
                ack_text = "no ack" if ack_latency is None else f"{ack_latency * 1000:.0f}ms"
                print(f"  [{i + 1}/{args.count}] level={level} echo={echo_text} ack={ack_text}")
                if echo_latency is not None:
                    echo_latencies.append(echo_latency)
                if ack_latency is not None:
                    ack_latencies.append(ack_latency)

            print(f"Restoring room {args.room} channel {args.channel} to level {previous_level}...")
            restore_spec = level_command(args.room, args.channel, previous_level)
            await measure_once(sender, listener, restore_spec)
    finally:
        await sender.close()

    print()
    _summarise("send->echo latency", echo_latencies)
    _summarise("send->AOK latency", ack_latencies)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
