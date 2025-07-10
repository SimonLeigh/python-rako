# Testing with Your Bridge Configuration

## Async Lock Tests

The `test_async_lock.py` file tests the critical async locking mechanism that prevents multiple concurrent HTTP requests to your Rako bridge. These tests use a sample XML file to avoid exposing sensitive bridge configuration data in the repository.

### Using Your Actual Bridge Configuration

To test with your actual bridge configuration:

1. **Get your bridge XML response:**
   ```bash
   curl http://YOUR_BRIDGE_IP/rako.xml > tests/sample_rako.xml
   ```

2. **Update test parameters:**
   Edit `tests/test_async_lock.py` and update the bridge configuration in each test:
   ```python
   bridge = Bridge(
       host="YOUR_BRIDGE_IP",        # e.g., "192.168.10.3"
       port=9761,
       name="YOUR_BRIDGE_NAME",      # e.g., "RAKOBRIDGE"
       mac="YOUR_BRIDGE_MAC"         # e.g., "00:11:22:33:44:55"
   )
   ```

3. **Update assertions:**
   Update the assertions in the tests to match your bridge's actual IP and MAC:
   ```python
   assert "YOUR_BRIDGE_IP" in result
   assert "YOUR_BRIDGE_MAC" in result
   ```

### What These Tests Verify

The async lock tests verify that:

1. **Concurrent Protection**: Multiple concurrent calls to `get_rako_xml()` on the same bridge instance result in only one actual HTTP request
2. **XML Content Integrity**: The returned XML matches the expected content without parsing exceptions
3. **Cache Behavior**: Subsequent calls use cached results appropriately
4. **Force Refresh**: The `force_refresh` parameter works correctly
5. **Instance Isolation**: Different bridge instances don't interfere with each other

### Security Note

Never commit your actual bridge configuration (IP addresses, MAC addresses, or device layout) to version control. The sample XML file contains generic placeholder data for testing purposes.
