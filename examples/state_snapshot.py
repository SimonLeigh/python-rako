"""Track bridge state with push updates and a reconciliation poll.

This is the pattern a Home Assistant coordinator (or any UI) wants:

* build a snapshot from the bridge's caches at start-up,
* apply every status broadcast to it as it arrives,
* poll ``scenes.htm`` occasionally to catch anything the push path missed.

The snapshot records *where each level came from*, and reconciliation uses that
to avoid overwriting a level the bridge actually reported with the approximate
one derived from a scene.

    python examples/state_snapshot.py 192.0.2.10
"""

import asyncio
import contextlib
import logging
import sys

import aiohttp

from python_rako import Bridge, StateSource, StatusListener

DEFAULT_HOST = "192.0.2.10"  # TEST-NET-1 placeholder
DEFAULT_PORT = 9761
POLL_INTERVAL = 300  # seconds


def print_snapshot(snapshot) -> None:
    for room in sorted(snapshot.rooms):
        scene = snapshot.room_scene(room)
        scene_text = "unknown" if scene is None else f"scene {scene}"
        print(f"room {room}: {scene_text}")
        for room_channel in sorted(snapshot.room_channels(room), key=lambda rc: rc.channel_id):
            state = snapshot.channels[room_channel]
            level = "unknown" if state.level is None else state.level
            estimated = " (estimated)" if state.is_estimated else ""
            print(f"    ch {room_channel.channel_id}: {level}{estimated}")


async def main(host: str) -> None:
    async with aiohttp.ClientSession() as session, StatusListener(host) as listener:
        bridge = Bridge(host, DEFAULT_PORT, "bridge", "", listener=listener)

        snapshot = await bridge.get_state_snapshot(session)
        print("=== initial snapshot ===")
        print_snapshot(snapshot)

        # The push path: every broadcast refines the snapshot.
        def on_message(message) -> None:
            nonlocal snapshot
            snapshot = snapshot.apply(message)
            print(f"applied {message}")
            if snapshot.level_table_stale:
                print("a scene was re-stored; the level table needs refreshing")

        listener.subscribe(on_message)

        # The poll path: bounded staleness for anything the push path missed.
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                fresh = await bridge.get_state_snapshot(
                    session, refresh_level_table=snapshot.level_table_stale
                )
                before = dict(snapshot.channels)
                snapshot = snapshot.reconcile(fresh)
                changed = [
                    rc
                    for rc, state in snapshot.channels.items()
                    if before.get(rc) != state and state.source is StateSource.SCENE_DERIVED
                ]
                print(f"=== reconciled; {len(changed)} channels corrected ===")
        finally:
            await bridge.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    host = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HOST
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(host))
