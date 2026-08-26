#!/usr/bin/env python3
"""Log every decoded Rako status broadcast, with origin and timestamps.

The characterisation tool behind ``docs/BRIDGE_BEHAVIOUR.md`` (Phase 0),
rewritten against the supervised :class:`~python_rako.StatusListener`. Run it
and press buttons on a keypad, drag a slider in the app, or send commands from
another client -- everything the bridge broadcasts is logged here, including
instructions the library does not model (as ``UnknownStatusMessage``).

The bridge address is never hard-coded: give ``--host`` or set
``RAKO_BRIDGE_HOST``, or omit both to fall back to ``discover_bridge()``.

    RAKO_BRIDGE_HOST=192.0.2.10 python scripts/listen.py
    python scripts/listen.py --host 192.0.2.10 --json-lines captures.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from python_rako import (
    RAKO_BRIDGE_DEFAULT_PORT,
    BridgeDescription,
    ListenerHealth,
    StatusListener,
    discover_bridge,
)

if TYPE_CHECKING:
    from python_rako.model import StatusMessage

_LOGGER = logging.getLogger("rako.scripts.listen")

_IDENTITY_FIELDS = {"room", "channel", "command", "data", "flags", "origin", "raw"}


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
    parser.add_argument(
        "--json-lines", metavar="FILE", help="also append one JSON object per message to FILE"
    )
    parser.add_argument(
        "--dedupe-window",
        type=float,
        default=0.3,
        help="seconds within which an identical broadcast is suppressed (default: %(default)s)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args()


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


def describe(message: StatusMessage) -> str:
    command = message.command
    command_text = command.name if hasattr(command, "name") else f"0x{message.command_value:02X}"
    extras = {
        key: value
        for key, value in vars(message).items()
        if key not in _IDENTITY_FIELDS and not key.startswith("_")
    }
    extra_text = " ".join(f"{key}={value}" for key, value in extras.items())
    base = f"room={message.room} ch={message.channel} cmd={command_text} data={list(message.data)}"
    return f"{base} {extra_text}".rstrip()


def as_json(message: StatusMessage, received_at: float) -> dict[str, object]:
    command = message.command
    return {
        "received_at": received_at,
        "room": message.room,
        "channel": message.channel,
        "command": message.command_value,
        "command_name": command.name if hasattr(command, "name") else None,
        "data": list(message.data),
        "origin": message.origin.value,
        "type": type(message).__name__,
        "raw": list(message.raw),
    }


async def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    bridge_description = await resolve_bridge(args)

    json_file: TextIO | None = None
    if args.json_lines:
        json_file = Path(args.json_lines).open("a")  # noqa: SIM115 - closed in finally

    def on_health_change(health: ListenerHealth) -> None:
        state = "UP" if health.is_running else f"DOWN ({health.last_error})"
        print(f"[listener] {state} restarts={health.restart_count}", file=sys.stderr)

    def on_message(message: StatusMessage) -> None:
        now = time.time()
        stamp = time.strftime("%H:%M:%S", time.localtime(now))
        millis = int(now % 1 * 1000)
        print(f"{stamp}.{millis:03d} [{message.origin.value}] {describe(message)}")
        if json_file is not None:
            json_file.write(json.dumps(as_json(message, now)) + "\n")
            json_file.flush()

    try:
        async with StatusListener(
            bridge_description["host"],
            args.port,
            dedupe_window=args.dedupe_window,
            on_health_change=on_health_change,
        ) as started:
            started.subscribe(on_message)

            print(
                f"Listening for broadcasts from {bridge_description['host']}. Ctrl-C to stop.",
                file=sys.stderr,
            )
            while True:
                await asyncio.sleep(30)
                health = started.health
                print(
                    f"-- {health.messages_received} messages, "
                    f"{health.suppressed_duplicates} duplicates suppressed, "
                    f"{health.ignored_packets} packets from other hosts, "
                    f"{health.non_status_packets} non-status packets",
                    file=sys.stderr,
                )
    finally:
        if json_file is not None:
            json_file.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
