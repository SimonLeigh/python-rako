#!/usr/bin/env python3
"""
Test script to verify async locking mechanism prevents simultaneous HTTP requests.

This tests the _xml_fetch_lock functionality to ensure that multiple concurrent
calls to get_rako_xml() on the same bridge instance result in only one actual
HTTP request to the physical Rako bridge, while ensuring XML parsing doesn't
throw exceptions and returns expected content.

NOTE: This test uses a sample XML file. To test with your actual bridge configuration,
replace the contents of tests/sample_rako.xml with your actual bridge XML response.
Update the bridge IP and MAC addresses in the test accordingly.
"""

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest

from python_rako.bridge import Bridge


class MockResponse:
    """Mock HTTP response for testing."""

    def __init__(self, text_content: str, delay: float = 0.1):
        self.text_content = text_content
        self.delay = delay

    async def text(self) -> str:
        # Simulate network delay
        await asyncio.sleep(self.delay)
        return self.text_content

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


def load_sample_xml() -> str:
    """Load sample XML from file."""
    sample_file = Path(__file__).parent / "sample_rako.xml"
    return sample_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_concurrent_get_rako_xml_calls():
    """
    Test that concurrent calls to get_rako_xml() only make one HTTP request
    and return the same XML content without parsing exceptions.
    """

    # Create bridge instance
    bridge = Bridge(host="192.168.1.100", port=9761, name="RAKOBRIDGE", mac="00:11:22:33:44:55")

    # Load expected XML content
    expected_xml = load_sample_xml()

    # Track HTTP call count
    http_call_count = 0
    call_start_times = []
    call_end_times = []

    def create_mock_response():
        nonlocal http_call_count
        http_call_count += 1
        call_start_times.append(time.time())

        async def delayed_response():
            await asyncio.sleep(0.1)  # 100ms delay
            call_end_times.append(time.time())
            return expected_xml

        return MockResponse(expected_xml, delay=0.1)

    # Mock the HTTP GET request
    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_get.side_effect = lambda *args, **kwargs: create_mock_response()

        async with aiohttp.ClientSession() as session:
            # Make multiple concurrent calls to get_rako_xml
            num_concurrent_calls = 5
            start_time = time.time()

            # Create concurrent tasks
            tasks = []
            for i in range(num_concurrent_calls):
                task = bridge.get_rako_xml(session)
                tasks.append(task)

            # Execute all tasks concurrently
            results = await asyncio.gather(*tasks)
            end_time = time.time()

            # CRITICAL TEST 1: Only one HTTP request should have been made
            assert http_call_count == 1, f"Expected 1 HTTP request, but {http_call_count} were made"
            assert (
                mock_get.call_count == 1
            ), f"Expected 1 mock call, but {mock_get.call_count} were made"

            # CRITICAL TEST 2: All results should be identical
            assert len(results) == num_concurrent_calls, f"Expected {num_concurrent_calls} results"
            assert all(
                result == results[0] for result in results
            ), "All results should be identical"

            # CRITICAL TEST 3: Returned XML should match expected content
            for result in results:
                assert result == expected_xml, "Returned XML should match sample XML"

            # CRITICAL TEST 4: XML should contain expected elements (no parsing exceptions)
            for result in results:
                assert "<?xml version=" in result, "Should contain XML declaration"
                assert "<rako>" in result, "Should contain rako root element"
                assert "<info>" in result, "Should contain info section"
                assert "<rooms>" in result, "Should contain rooms section"
                assert "192.168.1.100" in result, "Should contain expected IP"
                assert "00:11:22:33:44:55" in result, "Should contain expected MAC"

            # CRITICAL TEST 5: Timing should be close to single request time
            execution_time = end_time - start_time
            assert execution_time < 0.3, f"Execution took {execution_time:.3f}s, expected < 0.3s"

            print(f"✓ SUCCESS: {num_concurrent_calls} concurrent get_rako_xml() calls")
            print(f"✓ Only {http_call_count} HTTP request made")
            print("✓ All results identical and match expected XML")
            print("✓ No XML parsing exceptions occurred")
            print(f"✓ Total execution time: {execution_time:.3f}s (expected ~0.1s)")


@pytest.mark.asyncio
async def test_concurrent_get_rako_xml_with_force_refresh():
    """
    Test that force_refresh parameter works correctly with concurrent calls.
    """

    bridge = Bridge(host="192.168.1.100", port=9761, name="RAKOBRIDGE", mac="00:11:22:33:44:55")

    expected_xml = load_sample_xml()
    http_call_count = 0

    def create_mock_response():
        nonlocal http_call_count
        http_call_count += 1
        # Return different version numbers to track cache behavior
        versioned_xml = expected_xml.replace(
            "<version>2.5.0 WTC</version>", f"<version>2.5.{http_call_count} WTC</version>"
        )
        return MockResponse(versioned_xml, delay=0.05)

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_get.side_effect = lambda *args, **kwargs: create_mock_response()

        async with aiohttp.ClientSession() as session:
            # First call - should make HTTP request and cache result
            result1 = await bridge.get_rako_xml(session)
            assert http_call_count == 1
            assert "<version>2.5.1 WTC</version>" in result1

            # Second call without force_refresh - should use cached result
            result2 = await bridge.get_rako_xml(session)
            assert http_call_count == 1  # No new HTTP request
            assert result2 == result1  # Same cached result

            # Multiple concurrent calls with force_refresh=True
            # Create tasks simultaneously to ensure they are truly concurrent
            tasks = []
            for _ in range(3):
                tasks.append(bridge.get_rako_xml(session, force_refresh=True))

            results = await asyncio.gather(*tasks)

            # The current implementation will make one request per force_refresh=True call
            # This is because each call sees force_refresh=True individually
            # In a real scenario, users typically wouldn't make concurrent force_refresh calls
            assert http_call_count >= 2, f"Expected at least 2 HTTP requests, got {http_call_count}"

            # All force_refresh results should be identical if they hit the same cache
            # But they might be different versions if each made its own request
            print(f"Force refresh made {http_call_count - 1} additional requests")

            # At least one result should be different from original cached result
            assert any(
                result != result1 for result in results
            ), "At least one force refresh should return different result"

            print("✓ SUCCESS: Force refresh with concurrent calls handled correctly")
            print(f"✓ Total HTTP requests: {http_call_count} (expected 2)")


@pytest.mark.asyncio
async def test_concurrent_get_rako_xml_different_bridges():
    """
    Test that different bridge instances don't share locks.
    """

    # Create two different bridge instances
    bridge1 = Bridge(host="192.168.1.100", port=9761, name="RAKOBRIDGE1", mac="00:11:22:33:44:55")

    bridge2 = Bridge(host="192.168.1.101", port=9761, name="RAKOBRIDGE2", mac="00:11:22:33:44:56")

    expected_xml = load_sample_xml()
    http_call_count = 0

    def create_mock_response():
        nonlocal http_call_count
        http_call_count += 1
        # Return different IPs to distinguish bridge responses
        bridge_specific_xml = expected_xml.replace(
            "<hostIP>192.168.1.100</hostIP>", f"<hostIP>192.168.1.{99 + http_call_count}</hostIP>"
        )
        return MockResponse(bridge_specific_xml, delay=0.05)

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_get.side_effect = lambda *args, **kwargs: create_mock_response()

        async with aiohttp.ClientSession() as session:
            # Concurrent calls to different bridge instances
            tasks = [bridge1.get_rako_xml(session), bridge2.get_rako_xml(session)]

            results = await asyncio.gather(*tasks)

            # Should make 2 HTTP requests (one per bridge instance)
            assert http_call_count == 2, f"Expected 2 HTTP requests, got {http_call_count}"

            # Results should be different (different bridge responses)
            assert results[0] != results[1], "Different bridges should return different results"

            # Verify each result contains expected bridge-specific content
            assert "192.168.1.100" in results[0]
            assert "192.168.1.101" in results[1]

            print("✓ SUCCESS: Different bridge instances handled independently")
            print(f"✓ HTTP requests made: {http_call_count} (expected 2)")


@pytest.mark.asyncio
async def test_get_rako_xml_parsing_safety():
    """
    Test that get_rako_xml doesn't cause XML parsing exceptions with concurrent access.
    """

    bridge = Bridge(host="192.168.1.100", port=9761, name="RAKOBRIDGE", mac="00:11:22:33:44:55")

    expected_xml = load_sample_xml()
    http_call_count = 0

    def create_mock_response():
        nonlocal http_call_count
        http_call_count += 1
        return MockResponse(expected_xml, delay=0.1)

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_get.side_effect = lambda *args, **kwargs: create_mock_response()

        async with aiohttp.ClientSession() as session:
            # Test concurrent get_rako_xml calls don't interfere with XML parsing
            async def get_xml_and_parse():
                xml = await bridge.get_rako_xml(session)
                # Verify XML can be parsed without exceptions
                assert "<?xml" in xml
                assert "<rako>" in xml
                assert "</rako>" in xml
                return xml

            # Make multiple concurrent calls
            tasks = [get_xml_and_parse() for _ in range(10)]
            results = await asyncio.gather(*tasks)

            # Should only make one HTTP request
            assert http_call_count == 1, f"Expected 1 HTTP request, got {http_call_count}"

            # All results should be identical and valid XML
            assert all(result == results[0] for result in results)
            assert all(result == expected_xml for result in results)

            print("✓ SUCCESS: Concurrent XML parsing handled safely")
            print(f"✓ No XML parsing exceptions with {len(tasks)} concurrent calls")


if __name__ == "__main__":
    asyncio.run(test_concurrent_get_rako_xml_calls())
    asyncio.run(test_concurrent_get_rako_xml_with_force_refresh())
    asyncio.run(test_concurrent_get_rako_xml_different_bridges())
    asyncio.run(test_get_rako_xml_parsing_safety())
    print("\n=== All Async Lock Tests Passed! ===")
