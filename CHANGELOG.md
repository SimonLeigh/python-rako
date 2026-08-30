# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Full UDP status decoder (`python_rako.protocol`): every documented instruction
  (OFF, FADE_UP/DOWN, SC1–SC4, IDENT, LEVEL_SET, STORE, STOP, CUSTOM_232,
  HOLIDAY, SET_SCENE, FADE, SET_LEVEL), the empirically observed undocumented
  0x33 level-toggle, and a typed `UnknownStatusMessage` for anything else —
  nothing is silently dropped any more. Messages carry a `MessageOrigin`
  (sensor vs control, derived from flags bit 3) and 10-bit room ids.
- `StatusListener`: supervised broadcast listener with automatic restart and
  backoff, health reporting, a 300 ms duplicate window (the bridge re-sends
  some keypad events ~200 ms apart), and subscriber fan-out.
- Echo-verified commands: `Bridge.send_command`, `set_room_scene`,
  `set_room_level`, `set_channel_level`, `fade_up`, `fade_down`, `stop_fade`
  wait for the bridge's own status broadcast to confirm the command took
  effect (typically 150–300 ms), retry once on silence, then raise
  `RakoCommandError`. The echoed message is returned so callers update state
  from what the bridge actually did.
- `BridgeStateSnapshot`: room/channel state with per-channel provenance
  (`StateSource`) and a `reconcile()` rule that never overwrites a fresher
  level broadcast with a stale scene-derived level, nor resurrects a scene for
  a room whose last event was a fade.
- `Bridge.get_scene_cache_http()` reads the scene cache over HTTP
  (`scenes.htm`) so polling never contends with the UDP listener socket.
- **Command pacing** (`python_rako.pacing.CommandQueue`): every command now goes
  through a per-bridge FIFO queue that sends no faster than
  `min_command_interval` (default `DEFAULT_MIN_COMMAND_INTERVAL` = 1.5 s,
  assumed pending live measurement) and never overlaps a command still waiting
  for its echo. Requests that arrive too fast are queued, never dropped — the
  bridge silently ignores commands sent too close together. Commands for the
  same `(room, channel)` coalesce: a newer one replaces a waiting one in place,
  keeping its queue position, so a slider drag becomes a single send and the
  superseded callers get the echo of the command that actually ran. Diagnostics
  via `bridge.command_queue.stats` (depth, oldest age, sent/coalesced/failed),
  `drain()` for orderly shutdown, `paced=False` on `send_command`/`set_*` as a
  direct escape hatch, and a runtime-settable `bridge.min_command_interval`.
  A fade and its stop are treated as one gesture: the stop for the target of
  the fade just sent skips the queue and the interval (so a short tap stays
  short), neither half can coalesce the other away, and pacing resumes from the
  stop. New exception `RakoQueueClosedError` (a `RakoCommandError`) for
  commands a closed queue will never send.
- `scripts/measure_interval.py`: live tool that finds the bridge's real minimum
  safe interval by sending echo-verified off/on pairs at decreasing intervals,
  and recommends a `min_interval` from the result.
- `Bridge` is now an async context manager; `Bridge.close()` releases sockets.
- `discover_bridge(timeout=5.0)`; new exceptions `RakoDiscoveryError`,
  `RakoUnsupportedCommandError`, `RakoProtocolError`.
- `tests_integration/` rewritten on the new state/listener/echo-verify API,
  gated behind `RAKO_LIVE=1` (mutating tests additionally behind
  `RAKO_LIVE_MUTATE=1` and an operator-chosen `RAKO_TEST_ROOM`/
  `RAKO_TEST_CHANNEL`) and the `live` pytest marker, so a plain `pytest` run
  never collects them; `tests/test_state.py` gained a full-pipeline regression
  test replaying every Phase-0 capture through `decode_packet` and
  `BridgeStateSnapshot.apply`. New characterisation tools under `scripts/`
  (`listen.py`, `snapshot.py`, `latency.py`) mirror the Phase 0 tooling
  against the new API for exploring a live bridge by hand.

### Changed

- **Behaviour change:** `set_*` methods no longer report success on a UDP
  timeout. With a listener attached they raise `RakoCommandError` when the
  bridge does not confirm; without one they return `None` and warn.
  `BridgeCommanderUDP`/`BridgeCommanderHTTP` are deprecated delegates.
- `rako.xml` parsing runs off the event loop (`asyncio.to_thread`); the
  unnecessary parse lock was removed.
- Scene-cache parsing now handles extended (10-bit) room ids correctly.
- Tooling refresh: ruff/mypy/pre-commit pins updated, Python 3.12/3.13 CI matrix.

### Removed

- Dead code: `UDPMessageRateLimit`, `get_predicted_channel_brightness`.

### Fixed

- Release pipeline: the release workflow no longer overwrites
  `python_rako/__version__.py` with the raw git tag (which produced an
  invalid Python file). It now verifies the release tag matches
  `__version__.py` and fails loudly on mismatch instead of publishing a
  broken build.
- Release workflow now publishes to PyPI using **Trusted Publishing**
  (OIDC) instead of long-lived API tokens, with build attestations, a
  pre-publish smoke test (build the wheel, install it into a clean venv,
  import it), and a `workflow_dispatch`-triggered TestPyPI dry run.
- CI now also builds the sdist/wheel and runs `twine check` on every pull
  request, so packaging breakage is caught before release day, not during
  it.

<!--
When cutting a release, move the entries above into a new section here,
e.g.:

## [0.5.0] - 2026-09-01

### Fixed

- ...
-->

[Unreleased]: https://github.com/SimonLeigh/python-rako/commits/master

<!--
Note for the first tagged release under this process: no git tags exist in
this repository yet (0.4.1 was released without ever tagging the commit).
Once v0.5.0 (or whichever version comes first) is tagged, switch the link
above to a compare link, e.g.:
  [Unreleased]: https://github.com/SimonLeigh/python-rako/compare/v0.5.0...HEAD
  [0.5.0]: https://github.com/SimonLeigh/python-rako/releases/tag/v0.5.0
-->
