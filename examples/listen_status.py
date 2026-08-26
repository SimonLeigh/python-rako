"""Listen to every status broadcast the bridge sends.

The listener is supervised: if the socket dies it rebinds with exponential
backoff rather than leaving you with silently stale state.  Run it and press
buttons on a keypad -- fades, scene selections, levels and even instructions
the library does not model all arrive here.

    python examples/listen_status.py 192.0.2.10
"""

import asyncio
import contextlib
import logging
import sys

from python_rako import (
    ChannelStatusMessage,
    FadeMessage,
    ListenerHealth,
    SceneStatusMessage,
    StatusListener,
    StopFadeMessage,
    UnknownStatusMessage,
)

_LOGGER = logging.getLogger("example")

# Replace with your bridge's address, or use discover_bridge().
DEFAULT_HOST = "192.0.2.10"  # TEST-NET-1 placeholder


def describe(message) -> str:
    if isinstance(message, SceneStatusMessage):
        return f"room {message.room} -> scene {message.scene}"
    if isinstance(message, ChannelStatusMessage):
        return f"room {message.room} ch {message.channel} -> level {message.brightness}"
    if isinstance(message, FadeMessage):
        return f"room {message.room} fading {message.direction.value}"
    if isinstance(message, StopFadeMessage):
        return f"room {message.room} fade stopped (level now unknown)"
    if isinstance(message, UnknownStatusMessage):
        return (
            f"room {message.room} unmodelled command "
            f"0x{message.command_value:02X} data={list(message.data)}"
        )
    return repr(message)


def on_health_change(health: ListenerHealth) -> None:
    if health.is_running:
        print(f"listener up (restarts so far: {health.restart_count})")
    else:
        print(f"listener DOWN: {health.last_error}")


async def main(host: str) -> None:
    async with StatusListener(host, on_health_change=on_health_change) as listener:
        # Callbacks and the async iterator can be used together.
        listener.subscribe(lambda m: print(f"  [{m.origin.value}] {describe(m)}"))

        print(f"Listening for broadcasts from {host}. Ctrl-C to stop.")
        while True:
            await asyncio.sleep(30)
            health = listener.health
            print(
                f"-- {health.messages_received} messages, "
                f"{health.suppressed_duplicates} duplicates suppressed, "
                f"{health.ignored_packets} packets from other hosts"
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    host = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HOST
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(host))
