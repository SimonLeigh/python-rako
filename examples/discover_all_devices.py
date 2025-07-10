#!/usr/bin/env python3
"""
Example demonstrating discovery of all device types with the Rako python library.

This script shows how to:
1. Discover all devices (lights and ventilation) at once
2. Discover specific device types using lists
3. Use the new flexible discovery API
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

    async with aiohttp.ClientSession() as session:
        print("\n=== Discovering ALL devices ===")
        # Discover lights and ventilation separately
        async for device in bridge.discover_lights(session):
            device_type = type(device).__name__
            print(f"Found {device_type}: {device}")

        async for device in bridge.discover_ventilation(session):
            device_type = type(device).__name__
            print(f"Found {device_type}: {device}")

        print("\n=== Static method examples ===")
        rako_xml = await bridge.get_rako_xml(session)

        # Get all devices using static method
        all_devices = list(Bridge.get_devices_from_discovery_xml(rako_xml))
        print(f"Total devices found: {len(all_devices)}")

        # Get specific device types using list parameter
        lights = list(Bridge.get_devices_from_discovery_xml(rako_xml, ["Lights"]))
        ventilation = list(Bridge.get_devices_from_discovery_xml(rako_xml, ["Ventilation"]))

        print(f"Lights found: {len(lights)}")
        print(f"Ventilation found: {len(ventilation)}")

        # Get both types explicitly
        both_types = list(
            Bridge.get_devices_from_discovery_xml(rako_xml, ["Lights", "Ventilation"])
        )
        print(f"Both types found: {len(both_types)}")

        # Different ways to get all devices
        all_none = list(Bridge.get_devices_from_discovery_xml(rako_xml, None))
        all_string = list(Bridge.get_devices_from_discovery_xml(rako_xml, "All"))

        print(f"All devices (None): {len(all_none)}")
        print(f"All devices ('All'): {len(all_string)}")
        print(f"All same? {len(all_none) == len(all_string) == len(all_devices)}")


if __name__ == "__main__":
    asyncio.run(main())
