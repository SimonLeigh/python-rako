#!/usr/bin/env python3
"""Print a state snapshot alongside the HTTP and UDP scene caches, side by side.

Useful for confirming ``BRIDGE_BEHAVIOUR.md`` fact 13 (``scenes.htm`` decodes
identically to the UDP cache query) and fact 2 (fade-controlled rooms are
absent from both caches, and must show up as "unknown", not "off").

Read-only; makes no changes to the bridge.

    RAKO_BRIDGE_HOST=192.0.2.10 python scripts/snapshot.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import aiohttp

from python_rako import (
    RAKO_BRIDGE_DEFAULT_PORT,
    Bridge,
    BridgeDescription,
    RequestType,
    discover_bridge,
)


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


async def main() -> None:
    args = parse_args()
    bridge_description = await resolve_bridge(args)

    async with aiohttp.ClientSession() as session, Bridge(**bridge_description) as bridge:
        http_scene_cache = await bridge.get_scene_cache_http(session)
        udp_level_cache, udp_scene_cache = await bridge.get_cache_state(
            RequestType.SCENE_LEVEL_CACHE
        )
        snapshot = await bridge.get_state_snapshot(session)

        rooms = sorted(set(http_scene_cache) | set(udp_scene_cache) | set(snapshot.rooms))
        print(f"{'room':>6}  {'http scene':>10}  {'udp scene':>9}  {'snapshot scene':>14}  agree?")
        for room in rooms:
            http_value = http_scene_cache.get(room)
            udp_value = udp_scene_cache.get(room)
            snapshot_value = snapshot.room_scene(room)
            agree = "yes" if http_value == udp_value else "MISMATCH"
            print(
                f"{room:>6}  {http_value!s:>10}  {udp_value!s:>9}  {snapshot_value!s:>14}  {agree}"
            )

        print()
        print(f"level table entries: {len(udp_level_cache)}")
        print()
        print("=== channel levels (snapshot) ===")
        for room in sorted(snapshot.rooms):
            room_channels = sorted(snapshot.room_channels(room), key=lambda rc: rc.channel_id)
            for room_channel in room_channels:
                state = snapshot.channels[room_channel]
                level = "unknown" if state.level is None else state.level
                estimated = " (estimated)" if state.is_estimated else ""
                print(
                    f"  room {room_channel.room_id} ch {room_channel.channel_id}: "
                    f"{level}{estimated} [{state.source.value}]"
                )


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
