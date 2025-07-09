#!/usr/bin/env python3
"""
Example demonstrating ventilation control with the Rako python library.

This example shows how to:
1. Discover ventilation devices on the bridge
2. Control ventilation using the same commands as lighting
"""

import asyncio

import aiohttp

from python_rako import Bridge, discover_bridge


async def main():
    # Discover the bridge on the network
    bridge_desc = await discover_bridge()
    print(f"Found bridge: {bridge_desc}")

    # Create bridge instance
    bridge = Bridge(
        host=bridge_desc["host"],
        port=bridge_desc["port"],
        name=bridge_desc["name"],
        mac=bridge_desc["mac"],
    )

    # Discover ventilation devices
    async with aiohttp.ClientSession() as session:
        print("Discovering ventilation devices...")
        async for ventilation in bridge.discover_ventilation(session):
            print(f"Found ventilation: {ventilation}")

    # Example: Control ventilation (uses same commands as lighting)
    # Turn on ventilation in room 161 (scene 1)
    await bridge.set_room_scene(161, 1)
    print("Turned on ventilation in room 161")

    # Turn off ventilation in room 161 (scene 0)
    await bridge.set_room_scene(161, 0)
    print("Turned off ventilation in room 161")

    # Set ventilation to specific level (0-255)
    await bridge.set_channel_brightness(161, 1, 128)
    print("Set ventilation channel 1 to 50% speed")


if __name__ == "__main__":
    asyncio.run(main())
