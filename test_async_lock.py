#!/usr/bin/env python3
"""
Test script to verify async locking mechanism prevents simultaneous HTTP requests
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from python_rako.bridge import Bridge


class MockResponse:
    def __init__(self, text_content):
        self.text_content = text_content

    async def text(self):
        return self.text_content

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


async def test_async_lock_prevents_simultaneous_requests():
    """Test that async lock prevents simultaneous HTTP requests"""

    print("=== Testing Async Lock Mechanism ===\n")

    # Create a bridge instance
    bridge = Bridge(
        host="192.168.1.100", port=9761, name="Test Bridge", mac="00:11:22:33:44:55"
    )

    # Track call times and order
    call_times = []
    call_order = []

    # Mock the HTTP response with a delay to simulate network latency
    async def mock_get_response(*args, **kwargs):
        call_id = len(call_times)
        call_times.append(time.time())
        call_order.append(f"Request {call_id} started")

        # Simulate network delay
        await asyncio.sleep(0.1)

        call_order.append(f"Request {call_id} completed")

        # Return mock response
        return MockResponse(
            f"""<?xml version="1.0" encoding="UTF-8"?>
<rako>
    <rooms>
        <Room id="1">
            <Type>Lights</Type>
            <Title>Test Room</Title>
        </Room>
    </rooms>
</rako>"""
        )

    # Create multiple concurrent requests
    async def make_request(session, request_id):
        print(f"Starting request {request_id}")
        result = await bridge.get_rako_xml(session)
        print(f"Completed request {request_id}")
        return result

    # Test with mocked session
    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_get.return_value = mock_get_response()

        async with aiohttp.ClientSession() as session:
            # Make 5 concurrent requests
            tasks = [make_request(session, i) for i in range(5)]

            start_time = time.time()
            results = await asyncio.gather(*tasks)
            end_time = time.time()

            print(f"\nTotal execution time: {end_time - start_time:.2f} seconds")
            print(f"Expected minimum time (5 requests * 0.1s each): 0.5 seconds")
            print(f"Mock HTTP calls made: {mock_get.call_count}")

            # Verify all results are the same (cached)
            assert all(
                result == results[0] for result in results
            ), "All results should be identical due to caching"

            # Verify only one HTTP call was made
            assert (
                mock_get.call_count == 1
            ), f"Expected 1 HTTP call, got {mock_get.call_count}"

            print(
                f"\n✓ SUCCESS: Only {mock_get.call_count} HTTP request made despite {len(tasks)} concurrent calls"
            )
            print("✓ All requests returned the same cached result")


async def test_force_refresh_with_concurrent_requests():
    """Test force_refresh behavior with concurrent requests"""

    print("\n=== Testing Force Refresh with Concurrent Requests ===\n")

    bridge = Bridge(
        host="192.168.1.100", port=9761, name="Test Bridge", mac="00:11:22:33:44:55"
    )

    call_count = 0

    async def mock_get_response(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.1)  # Simulate network delay

        return MockResponse(
            f"""<?xml version="1.0" encoding="UTF-8"?>
<rako>
    <rooms>
        <Room id="{call_count}">
            <Type>Lights</Type>
            <Title>Test Room {call_count}</Title>
        </Room>
    </rooms>
</rako>"""
        )

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_get.return_value = mock_get_response()

        async with aiohttp.ClientSession() as session:
            # First request to populate cache
            result1 = await bridge.get_rako_xml(session)

            # Multiple concurrent requests with force_refresh=True
            tasks = [bridge.get_rako_xml(session, force_refresh=True) for _ in range(3)]
            results = await asyncio.gather(*tasks)

            print(f"HTTP calls made: {mock_get.call_count}")
            print(f"Expected calls: 2 (1 initial + 1 for force_refresh)")

            # Should make 2 calls total: 1 initial + 1 for force_refresh (others wait on lock)
            assert (
                mock_get.call_count == 2
            ), f"Expected 2 HTTP calls, got {mock_get.call_count}"

            # All force_refresh results should be the same
            assert all(
                result == results[0] for result in results
            ), "All force_refresh results should be identical"

            print("✓ SUCCESS: Force refresh with concurrent requests handled correctly")


async def main():
    await test_async_lock_prevents_simultaneous_requests()
    await test_force_refresh_with_concurrent_requests()
    print("\n=== All Async Lock Tests Passed! ===")


if __name__ == "__main__":
    asyncio.run(main())
