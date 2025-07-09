"""Example script demonstrating async file operations and Rako bridge updates.

This script shows how to:
- Monitor Rako bridge updates in real-time
- Write updates to a file asynchronously
- Read and process the update data
"""

import asyncio
import logging

import aiofiles

from python_rako import Bridge, BridgeDescription, discover_bridge
from python_rako.helpers import get_dg_listener

_LOGGER = logging.getLogger(__name__)


# Define an async function to write data to a file
async def write_to_file(filename, data):
    # Open the file for writing using async with, which ensures the file is closed
    # when we're done with it
    async with aiofiles.open(filename, "w") as f:
        # Write the data to the file using the await keyword
        await f.write(data)


# Define an async function to read data from a file
async def read_from_file(filename):
    # Open the file for reading using async with, which ensures the file is closed
    # when we're done with it
    async with aiofiles.open(filename, "r") as f:
        # Read the contents of the file using the await keyword
        data = await f.read()
        # Return the data as a string
        return data


# Define the main coroutine, which will run when we execute the script
async def async_file_example():
    # Set up a filename and some data to write to the file
    filename = "example.txt"
    data = "Hello, world!"

    # Create tasks to write and read the file concurrently
    write_task = asyncio.create_task(write_to_file(filename, data))
    read_task = asyncio.create_task(read_from_file(filename))

    # Wait for both tasks to complete
    await asyncio.gather(write_task, read_task)

    # Print the contents of the file to the console
    print(read_task.result())


# Run the async file example using asyncio.run, which creates and manages the event loop
# if __name__ == '__main__':
#     asyncio.run(async_file_example())


async def listen_for_state_updates(bridge):
    """Listen for state updates worker method."""
    async with get_dg_listener(bridge.port) as listener:
        while True:
            message = await bridge.next_pushed_message(listener)
            if message:
                # Do stuff with the message
                _LOGGER.debug(message)


def main():
    logging.basicConfig(level=logging.DEBUG)
    loop = asyncio.get_event_loop()

    # Find the bridge
    bridge_desc: BridgeDescription = loop.run_until_complete(
        asyncio.gather(discover_bridge())
    )[0]
    print(bridge_desc)

    # Listen for state updates in the lights
    bridge = Bridge(**bridge_desc)
    # Start listening task (using _ to indicate we don't use the task variable)
    _ = loop.create_task(listen_for_state_updates(bridge))

    loop.run_forever()
    # Stop listening
    # task.cancel()


if __name__ == "__main__":
    main()
