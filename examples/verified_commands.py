"""Send commands and wait for the bridge to confirm it acted.

A Rako bridge answers every command with a weak acknowledgement that only says
it received the request.  What proves anything is the status broadcast it makes
when the circuit actually changes -- and that broadcast arrives *sooner* than
the acknowledgement does.  Attach a listener and the ``set_*`` methods wait for
it, retry once on silence, and raise if the bridge never confirms.

    python examples/verified_commands.py 192.0.2.10 7 2
"""

import asyncio
import logging
import sys

from python_rako import Bridge, RakoCommandError, StatusListener

DEFAULT_HOST = "192.0.2.10"  # TEST-NET-1 placeholder
DEFAULT_PORT = 9761


async def main(host: str, room: int, channel: int) -> None:
    async with StatusListener(host) as listener:
        bridge = Bridge(host, DEFAULT_PORT, "bridge", "", listener=listener)
        try:
            # Each call returns the bridge's own echo. Update your state from
            # that, never from the value you asked for.
            echo = await bridge.set_channel_level(room, channel, 255)
            print(f"on  -> confirmed: {echo}")

            await asyncio.sleep(1)

            echo = await bridge.set_channel_level(room, channel, 64)
            print(f"25% -> confirmed: {echo}")

            await asyncio.sleep(1)

            echo = await bridge.set_room_scene(room, 0)
            print(f"off -> confirmed: {echo}")

            # Fades work like a keypad button: start one, then stop it. No
            # level is broadcast when a fade stops, so the resulting level is
            # genuinely unknown afterwards.
            await bridge.fade_up(room, channel)
            await asyncio.sleep(1.5)
            await bridge.stop_fade(room, channel)
            print("faded up for 1.5s; level is now unknown")

        except RakoCommandError as err:
            # The command was sent twice and the bridge never reported a
            # change. This is the failure the acknowledgement used to hide.
            print(f"command not confirmed: {err}")
        finally:
            await bridge.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    args = sys.argv[1:]
    host = args[0] if args else DEFAULT_HOST
    room = int(args[1]) if len(args) > 1 else 1
    channel = int(args[2]) if len(args) > 2 else 1
    asyncio.run(main(host, room, channel))
