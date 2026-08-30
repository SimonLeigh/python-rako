# Python: Rako Controls API Client

[![GitHub Release][releases-shield]][releases]
![Project Stage][project-stage-shield]
![Project Maintenance][maintenance-shield]
[![License][license-shield]](LICENSE)

[![Build Status][build-shield]][build]
[![Code Coverage][codecov-shield]][codecov]
[![Code Quality][code-quality-shield]][code-quality]

[![Buy me a coffee][buymeacoffee-shield]][buymeacoffee]

Asynchronous Python client for Rako Controls.

## About

This package allows you to control and monitor Rako Controls devices
programmatically. It is mainly created to allow third-party programs to automate
their behavior.

## Installation

```bash
pip install python-rako
```

## Usage

Runnable versions of everything below live in [`examples/`](examples/).

### Listening for status

The bridge broadcasts a status message for every change it makes, and those
broadcasts are the only live source of true dimmer levels — there is no way to
read a circuit's current level back. `StatusListener` owns that socket and
keeps it alive: any error rebinds it with exponential backoff instead of
leaving you with state that is silently frozen.

```python
from python_rako import StatusListener

async with StatusListener("192.0.2.10") as listener:
    listener.subscribe(lambda message: print(message))

    async for message in listener.messages():
        print(message.room, message.channel, message.origin)
```

It binds with `SO_REUSEADDR` and `SO_REUSEPORT`, so several processes on one
host can listen at once; it accepts datagrams only from the bridge's address
(any source port — the bridge broadcasts from an ephemeral one); and it
suppresses the duplicate broadcasts the bridge itself emits ~200 ms apart.
Health and counters live on `listener.health` — `is_running`,
`last_message_at`, `restart_count`, `messages_received`, `ignored_packets`,
`suppressed_duplicates`, `last_error` — which returns a copy, or arrive via an
`on_health_change` callback. Subscribe with `include_duplicates=True` to see
messages the dedupe window would otherwise hide.

Every instruction is decoded, including fades, stop, store, ident and the
undocumented `0x33`. Anything unrecognised arrives as an `UnknownStatusMessage`
carrying room, channel, command and data rather than being dropped.
See [`examples/listen_status.py`](examples/listen_status.py).

### Sending verified commands

A Rako bridge's `"AOK"` only means it parsed the request, not that the circuit
moved — and commands really do get lost. Attach a listener and the `set_*`
methods wait for the status broadcast that proves the change happened, retry
once on silence, then raise.

```python
from python_rako import Bridge, RakoCommandError, StatusListener

async with (
    StatusListener(host) as listener,
    Bridge(host, 9761, name, mac, listener=listener) as bridge,
):
    try:
        echo = await bridge.set_channel_level(room_id=7, channel_id=2, level=255)
        print("confirmed by the bridge:", echo)
    except RakoCommandError:
        print("the bridge never confirmed the change")
```

Update your state from the returned echo, never from the value you asked for.
`set_room_scene`, `set_room_level`, `set_channel_level`, `fade_up`, `fade_down`
and `stop_fade` all work this way; pass `verify=False` to opt out.

A command returns `None` whenever it could not be verified — no listener
attached, the listener not currently receiving, `verify=False`, or a command
the bridge does not echo — rather than claiming a success it cannot
demonstrate. `RakoCommandError` means the bridge stayed silent after a send and
a resend; `RakoUnsupportedCommandError` means the transport cannot express the
command at all (the HTTP commander has no fade), and is never retried.

Use `async with Bridge(...)` or call `await bridge.close()` when you are done:
the UDP command transport keeps one socket open for the bridge's lifetime.
See [`examples/verified_commands.py`](examples/verified_commands.py).

### Command pacing

A Rako bridge silently drops commands that arrive too close together — it
accepts the frame, never acts on it, and says nothing. Home Assistant will
happily issue twenty level changes while a slider is dragged, so every command
goes through a per-bridge queue that sends no faster than
`min_command_interval` (default `1.25` s, measured on a live bridge: 1.0 s spacing was loss-free, 0.75 s dropped a command) and never more than one verified
command at a time.

```python
bridge = Bridge(host, 9761, name, mac, listener=listener, min_command_interval=1.25)

# Twenty rapid levels for one channel; the queue sends the first, then one
# more carrying the final level. Nothing is dropped, nothing is sent too fast.
await asyncio.gather(*(bridge.set_channel_level(7, 2, level) for level in levels))
```

Requests that arrive too soon are **queued, never dropped**, and sent in order.
While a command for a `(room, channel)` is still waiting, a newer command for
the same target **replaces it in place**: it keeps the original queue position
and takes the new payload, so a slider drag costs one send instead of twenty.
Different kinds of command coalesce too (a scene selection replaces a pending
level for the same target), because the bridge only honours whichever arrives
last anyway.

A superseded request does not fail and does not return `None`: it resolves with
the result of the command that replaced it — the echo the bridge sent for the
value it actually applied. `await bridge.set_channel_level(...)` therefore
always describes where the channel really ended up.

The next send waits for `max(previous send + min_command_interval, previous
echo-or-failure)`, so a slow verification is never overlapped with the next
command. A failing command's exception goes to its own caller and the queue
carries on.

**Fades are the exception.** A fade is a gesture, not a value: `fade_up()` is
the press and `stop_fade()` is the release, and the release decides how far the
circuit got. Pacing it would turn a 200 ms tap into a 1.5 s sweep, so a stop
for the target of the fade just sent is dispatched as soon as the queue is
free, ahead of anything else waiting and without waiting out the interval. The
press itself is paced as usual, neither half of the pair can be coalesced away
by the other, and the next command is paced from the stop.

```python
stats = bridge.command_queue.stats
# depth, oldest_age, in_flight, sent, coalesced, failed, min_interval

bridge.min_command_interval = 1.0        # adjustable at runtime
await bridge.command_queue.drain()       # let everything queued land
await bridge.send_command(spec, paced=False)  # tooling escape hatch
```

`bridge.close()` closes the queue; anything still queued raises
`RakoQueueClosedError` (a `RakoCommandError`), so nothing waits forever on a
queue that will never run again. Call `drain()` first if you want queued
commands to land.

The 1.25 s default is **measured** (2026-08-30, on a live WTC bridge): 1.0 s
spacing was loss-free across all trials, 0.75 s silently dropped a command, and
the default is the fastest clean interval with a 1.25x margin.
[`scripts/measure_interval.py`](scripts/measure_interval.py) re-measures
against any bridge by sending off/on pairs at decreasing intervals until an
echo goes missing:

```console
$ RAKO_BRIDGE_HOST=192.0.2.10 python scripts/measure_interval.py \
    --room 7 --channel 2 --i-know-this-changes-lights
```

It switches a real circuit, so it refuses to run without that flag, restores
the channel's original level afterwards, and stops at the first interval that
loses anything.

### State snapshot

`BridgeStateSnapshot` records a level *and where it came from*, which is what
makes it safe to combine live broadcasts with periodic cache reads.

```python
snapshot = await bridge.get_state_snapshot(session)

listener.subscribe(lambda message: apply(message))   # push path
...
fresh = await bridge.get_state_snapshot(session)     # poll path
snapshot = snapshot.reconcile(fresh)
```

`apply()` is pure and returns a new snapshot. `reconcile()` applies
cache-derived values to a room only when the cached scene differs from the one
being tracked, so a level the bridge actually broadcast is never overwritten by
the approximate level a scene implies. Rooms missing from the scene cache are
reported as unknown, never as off — the bridge deletes fade-controlled rooms
from that cache by design. A room that has just faded keeps its
`UNKNOWN_AFTER_FADE` channels even if a cache read still reports the scene it
faded out of: that cache entry has simply not caught up.

When a STORE broadcast sets `snapshot.level_table_stale`, a keypad has
rewritten a scene definition, so every scene-derived level is suspect. Clear it
by actually re-reading the table — `await bridge.refresh_level_table()`, or
`get_state_snapshot(session, refresh_level_table=True)`; reconciling against
the table you already had leaves the flag set.
See [`examples/state_snapshot.py`](examples/state_snapshot.py).

## Changelog & Releases

This repository keeps a change log using [GitHub's releases][releases]
functionality. The format of the log is based on
[Keep a Changelog][keepchangelog].

Releases are based on [Semantic Versioning][semver], and use the format
of ``MAJOR.MINOR.PATCH``. In a nutshell, the version will be incremented
based on the following:

- ``MAJOR``: Incompatible or major changes.
- ``MINOR``: Backwards-compatible new features and enhancements.
- ``PATCH``: Backwards-compatible bugfixes and package updates.

## Contributing

This is an active open-source project. We are always open to people who want to
use the code or contribute to it.

We've set up a separate document for our
[contribution guidelines](CONTRIBUTING.md).

Thank you for being involved! :heart_eyes:

## Setting up development environment

In case you'd like to contribute, a `Makefile` has been included to ensure a
quick start.

```bash
make venv
source ./venv/bin/activate
make dev
```

Now you can start developing, run `make` without arguments to get an overview
of all make goals that are available (including description):

```bash
$ make
Asynchronous Python client for Rako Controls Lighting.

Usage:
  make help                            Shows this message.
  make dev                             Set up a development environment.
  make lint                            Run all linters.
  make lint-black                      Run linting using black & blacken-docs.
  make lint-flake8                     Run linting using flake8 (pycodestyle/pydocstyle).
  make lint-pylint                     Run linting using PyLint.
  make lint-mypy                       Run linting using MyPy.
  make test                            Run tests quickly with the default Python.
  make coverage                        Check code coverage quickly with the default Python.
  make install                         Install the package to the active Python's site-packages.
  make clean                           Removes build, test, coverage and Python artifacts.
  make clean-all                       Removes all venv, build, test, coverage and Python artifacts.
  make clean-build                     Removes build artifacts.
  make clean-pyc                       Removes Python file artifacts.
  make clean-test                      Removes test and coverage artifacts.
  make clean-venv                      Removes Python virtual environment artifacts.
  make dist                            Builds source and wheel package.
  make release                         Release build on PyP
  make venv                            Create Python venv environment.
```

## Authors & contributors

The original setup of this repository is by [Ben Marengo][marengaz].

For a full list of all authors and contributors,
check [the contributor's page][contributors].

## License

[License](LICENSE)

[build-shield]: https://github.com/marengaz/python-rako/workflows/Continuous%20Integration/badge.svg
[build]: https://github.com/marengaz/python-rako/actions
[code-quality-shield]: https://img.shields.io/lgtm/grade/python/g/marengaz/python-rako.svg?logo=lgtm&logoWidth=18
[code-quality]: https://lgtm.com/projects/g/marengaz/python-rako/context:python
[codecov-shield]: https://codecov.io/gh/marengaz/python-rako/branch/master/graph/badge.svg
[codecov]: https://codecov.io/gh/marengaz/python-rako
[contributors]: https://github.com/marengaz/python-rako/graphs/contributors
[marengaz]: https://github.com/marengaz
[keepchangelog]: http://keepachangelog.com/en/1.0.0/
[license-shield]: https://img.shields.io/github/license/marengaz/python-rako.svg
[maintenance-shield]: https://img.shields.io/maintenance/yes/2021.svg
[project-stage-shield]: https://img.shields.io/badge/project%20stage-experimental-yellow.svg
[releases-shield]: https://img.shields.io/github/release/marengaz/python-rako.svg
[releases]: https://github.com/marengaz/python-rako/releases
[semver]: http://semver.org/spec/v2.0.0.html

[buymeacoffee-shield]: https://www.buymeacoffee.com/assets/img/guidelines/download-assets-sm-2.svg
[buymeacoffee]: https://www.buymeacoffee.com/marengaz
[github-actions-shield]: https://github.com/marengaz/rakomqtt/workflows/Test%20RakoMQTT/badge.svg?branch=master
[github-actions]: https://github.com/marengaz/rakomqtt/actions?query=workflow%3A%22Test+RakoMQTT%22+branch%3Amaster
