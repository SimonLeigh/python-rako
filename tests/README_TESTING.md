# Testing python-rako

There are three tiers of test, from fastest/safest to slowest/most invasive.
None of them need you to edit a test file with your bridge's address --
everything live-bridge related is driven entirely by environment variables.

## 1. Unit tests (`tests/`) -- always run this one

Pure, fast, no network. This is what CI runs and what `pytest` runs by
default (`pyproject.toml` sets `testpaths = ["tests"]`, so `tests_integration/`
is never even collected here).

```bash
pytest
```

Decoder/state coverage worth knowing about:

- `tests/test_protocol.py` pins the decoded meaning of every packet captured
  during the Phase 0 characterisation recorded in
  `hacs_rako/docs/BRIDGE_BEHAVIOUR.md` (keypad, app, HA, and sensor traffic),
  plus the worked examples from the official protocol document.
- `tests/test_state.py` replays specific sequences of those captures through
  `BridgeStateSnapshot.apply()` (e.g. the room-6 slider-drag-vs-scene-cache
  scenario, the room-9 fade press/release), and
  `test_every_captured_packet_replays_through_decode_and_apply` replays
  *every* capture through the full `decode_packet -> apply` pipeline a real
  `StatusListener` subscriber uses, so a decoder regression on any of them is
  caught here rather than live.

## 2. Live integration tests (`tests_integration/`) -- read-only

Exercises the library against a real bridge on your network: discovery,
`get_info`, device discovery, the HTTP/UDP scene-cache agreement, state
snapshots, and the listener lifecycle (including two listeners sharing a
host). None of these change light state.

Gating, in order:

1. `pyproject.toml`'s `testpaths` already excludes `tests_integration/` from
   a plain `pytest` run -- you always have to ask for it explicitly.
2. Every test is tagged `@pytest.mark.live` and skipped unless `RAKO_LIVE=1`
   is set, so `pytest tests_integration` without it collects everything and
   skips cleanly (safe to run anywhere, e.g. by accident in CI).
3. The `--cov-fail-under=80` coverage gate in the shared `addopts` is tuned
   for the unit-test tier; pass `--no-cov` when running `tests_integration`
   on its own, or the run will report a coverage "failure" even though every
   test skipped or passed.

Bridge address, from the environment (never hard-coded, never discovered by
scanning a fixed subnet):

| Variable            | Meaning                                              |
|----------------------|-------------------------------------------------------|
| `RAKO_BRIDGE_HOST`   | Bridge IP/hostname. If unset, `discover_bridge()` is used to find one on the LAN. |
| `RAKO_BRIDGE_PORT`   | Bridge UDP port (default `9761`).                     |
| `RAKO_BRIDGE_MAC`    | Optional; cross-checked against discovery if both are set. |
| `RAKO_BRIDGE_NAME`   | Optional, cosmetic only.                              |

Run it:

```bash
RAKO_LIVE=1 pytest tests_integration -m live --no-cov
```

(`-m live` is redundant given every test already carries that marker, but
makes the intent explicit and lets you combine it with `-k` to run a subset.)

Without `RAKO_LIVE=1`:

```bash
pytest tests_integration --no-cov   # collects everything, reports "skipped"
```

## 3. Live integration tests -- state-changing

`tests_integration/test_command_live.py` sends real commands to a real
channel: it sets the level to 0 and back, measures send-to-echo latency,
starts and stops a fade, and confirms an unverified command returns `None`
rather than a false success. Every test restores the level it found before
it ran (except the fade test -- see below), so the suite is safe to re-run,
but it **will** visibly change the chosen light while it runs.

Requires everything from tier 2, plus:

| Variable              | Meaning                                                        |
|------------------------|------------------------------------------------------------------|
| `RAKO_LIVE_MUTATE=1`   | Explicit opt-in; without it these tests skip with a clear reason. |
| `RAKO_TEST_ROOM`       | Room id of the channel you are happy to see toggled/dimmed/faded. |
| `RAKO_TEST_CHANNEL`    | Channel id within that room (`0` addresses the whole room).       |

```bash
RAKO_LIVE=1 RAKO_LIVE_MUTATE=1 RAKO_TEST_ROOM=7 RAKO_TEST_CHANNEL=2 \
    pytest tests_integration -m live --no-cov
```

Pick a room/channel you don't mind flickering for a few seconds. The one
exception to "restores what it found": `test_fade_up_then_stop_produces_fade_and_stop_broadcasts`
starts and stops a fade, and the bridge broadcasts no level when a fade stops
(this is expected bridge behaviour, not a test bug -- see
`hacs_rako/docs/BRIDGE_BEHAVIOUR.md` facts 1 and 3), so there is nothing
meaningful to restore the channel to afterwards.

## Characterisation scripts (`scripts/`)

Interactive tools for exploring a live bridge by hand, mirroring what the
Phase 0 characterisation used -- useful for a first look at an installation,
debugging, or capturing new fixtures. None of them embed a bridge address;
all take one from `--host`/`RAKO_BRIDGE_HOST` or fall back to
`discover_bridge()`.

- **`scripts/listen.py`** -- logs every decoded status broadcast with origin
  and a timestamp; `--json-lines FILE` also appends one JSON object per
  message, e.g. for building new capture fixtures.
  ```bash
  RAKO_BRIDGE_HOST=192.0.2.10 python scripts/listen.py
  ```
- **`scripts/snapshot.py`** -- read-only; prints the state snapshot next to
  the HTTP (`scenes.htm`) and UDP scene caches side by side, so you can see
  at a glance whether they agree and which rooms are absent (fade-controlled)
  from each.
  ```bash
  RAKO_BRIDGE_HOST=192.0.2.10 python scripts/snapshot.py
  ```
- **`scripts/latency.py`** -- **changes light state** on a chosen channel
  repeatedly; requires `--i-know-this-changes-lights`. Sends N verified
  commands and reports min/median/max send->echo and send->AOK latency.
  ```bash
  RAKO_BRIDGE_HOST=192.0.2.10 python scripts/latency.py \
      --room 7 --channel 2 --count 10 --i-know-this-changes-lights
  ```

## Security note

Never commit a real bridge IP/MAC address, hostname, NAS path, or room/house
name to this repository -- it's public. Everything above is designed so a
live run never needs one written down anywhere except your own shell
environment.
