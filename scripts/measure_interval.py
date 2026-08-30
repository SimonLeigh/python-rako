#!/usr/bin/env python3
"""Measure how fast a real Rako bridge can actually accept commands.

``DEFAULT_MIN_COMMAND_INTERVAL`` is currently 1.5 s *by assumption* -- it is the
echo-verify window, known to be safe, not a measured floor.  This script finds
the floor by asking the bridge to do the thing it is known to fail at: two
commands to the same channel in quick succession.

For each candidate interval it runs three trials.  A trial is::

    send OFF (echo-verified, no retries)
    wait <interval>
    send ON  (echo-verified, no retries)

A trial *fails* when either command produces no echo within the verify window.
That is exactly the Phase 0 failure -- the bridge accepts the frame and never
acts on it -- so a missing echo is the signal we are looking for.  Retries are
disabled on purpose: a retry would paper over precisely what is being measured.

Intervals are tried from slow to fast and the run stops at the first interval
with any loss, because everything below it is worse and there is no point
flashing the lights for nothing.  The recommendation is the smallest interval
that scored 3/3, plus 25% margin.

Both commands go over the **direct** (unpaced) path: pacing the pacing
measurement would measure the pacing.

    RAKO_BRIDGE_HOST=192.0.2.10 python scripts/measure_interval.py \\
        --room 7 --channel 2 --i-know-this-changes-lights

Then record the result in ``BRIDGE_BEHAVIOUR.md`` and, if it is below 1.5 s,
lower ``DEFAULT_MIN_COMMAND_INTERVAL``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys

from python_rako import (
    Bridge,
    RakoBridgeError,
    RakoCommandError,
    StatusListener,
    discover_bridge,
)
from python_rako.commands import level_command
from python_rako.const import RAKO_BRIDGE_DEFAULT_PORT

#: Slow to fast. The run stops at the first interval that loses anything.
DEFAULT_INTERVALS = (2.0, 1.5, 1.0, 0.75, 0.5, 0.35, 0.25)
DEFAULT_TRIALS = 3
#: Applied to the fastest interval that scored a clean sweep.
SAFETY_MARGIN = 1.25
#: Between trials, so one trial's tail never contaminates the next.
SETTLE = 3.0

_LOGGER = logging.getLogger("measure_interval")


class Trial:
    """One off/on pair at a candidate interval."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.lost: list[str] = []
        self.error: str | None = None

    @property
    def ok(self) -> bool:
        return not self.lost and self.error is None

    def describe(self) -> str:
        if self.error is not None:
            return f"error: {self.error}"
        if self.lost:
            return "lost " + "+".join(self.lost)
        return "ok"


async def run_trial(bridge: Bridge, room: int, channel: int, interval: float) -> Trial:
    """Send OFF then ON, ``interval`` apart, and report which echoes were lost."""
    trial = Trial(interval)
    for label, level in (("off", 0), ("on", 255)):
        if label == "on":
            await asyncio.sleep(interval)
        try:
            echo = await bridge.send_command(
                level_command(room, channel, level),
                paced=False,  # the whole point: this run sets its own timing
                retries=0,  # a retry would hide the loss being measured
            )
        except RakoCommandError:
            trial.lost.append(label)
        except RakoBridgeError as err:  # transport trouble, not command loss
            trial.error = str(err)
            return trial
        else:
            if echo is None:
                trial.error = "no echo verification (is the listener running?)"
                return trial
    return trial


async def read_level(bridge: Bridge, room: int, channel: int) -> int | None:
    """The channel's current level, so it can be put back afterwards."""
    try:
        snapshot = await bridge.get_state_snapshot()
    except RakoBridgeError as err:
        _LOGGER.warning("Could not read the current state (%s); will not restore", err)
        return None
    level = snapshot.channel_level(room, channel)
    if level is None:
        _LOGGER.warning(
            "The bridge does not know room %d channel %d's level "
            "(fade-controlled, most likely); will not restore it",
            room,
            channel,
        )
    return level


async def restore(bridge: Bridge, room: int, channel: int, level: int | None) -> None:
    if level is None:
        return
    _LOGGER.info("Restoring room %d channel %d to level %d", room, channel, level)
    with contextlib.suppress(RakoBridgeError):
        await bridge.send_command(level_command(room, channel, level), paced=False, retries=1)


def print_table(results: dict[float, list[Trial]], trials: int) -> None:
    print()
    print(f"{'interval (s)':>12}  {'result':>7}  detail")
    print(f"{'-' * 12}  {'-' * 7}  {'-' * 40}")
    for interval, runs in results.items():
        passed = sum(1 for trial in runs if trial.ok)
        detail = ", ".join(trial.describe() for trial in runs)
        print(f"{interval:>12.2f}  {passed:>4}/{trials}  {detail}")
    print()


def recommend(results: dict[float, list[Trial]], trials: int) -> float | None:
    """The fastest clean interval, plus margin."""
    clean = [
        interval
        for interval, runs in results.items()
        if len(runs) == trials and all(trial.ok for trial in runs)
    ]
    if not clean:
        return None
    return round(min(clean) * SAFETY_MARGIN, 2)


async def measure(
    host: str,
    port: int,
    room: int,
    channel: int,
    intervals: tuple[float, ...],
    trials: int,
) -> int:
    results: dict[float, list[Trial]] = {}
    async with (
        StatusListener(host) as listener,
        Bridge(host, port, "bridge", "", listener=listener) as bridge,
    ):
        original = await read_level(bridge, room, channel)
        try:
            for interval in intervals:
                runs: list[Trial] = []
                results[interval] = runs
                for index in range(1, trials + 1):
                    trial = await run_trial(bridge, room, channel, interval)
                    runs.append(trial)
                    _LOGGER.info(
                        "interval %.2fs trial %d/%d: %s", interval, index, trials, trial.describe()
                    )
                    if trial.error is not None:
                        print_table(results, trials)
                        _LOGGER.error("Stopping: %s", trial.error)
                        return 2
                    await asyncio.sleep(SETTLE)
                if not all(trial.ok for trial in runs):
                    _LOGGER.info("Loss at %.2fs; nothing faster can be safe. Stopping.", interval)
                    break
        finally:
            await restore(bridge, room, channel, original)

    print_table(results, trials)
    suggestion = recommend(results, trials)
    if suggestion is None:
        print(
            "No interval was clean -- even the slowest one lost a command.\n"
            "Keep the current default and investigate the bridge or the network."
        )
        return 1
    print(
        f"Recommended min_interval: {suggestion:.2f}s "
        f"(fastest clean interval x {SAFETY_MARGIN:g} margin).\n"
        "Record this in BRIDGE_BEHAVIOUR.md before changing "
        "DEFAULT_MIN_COMMAND_INTERVAL."
    )
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find the minimum safe interval between commands to a Rako bridge.",
        epilog="This turns a real light off and on many times. Pick a channel nobody is using.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("RAKO_BRIDGE_HOST"),
        help="bridge address (default: $RAKO_BRIDGE_HOST, else discovery)",
    )
    parser.add_argument("--port", type=int, default=RAKO_BRIDGE_DEFAULT_PORT)
    parser.add_argument("--room", type=int, required=True, help="room id to test")
    parser.add_argument("--channel", type=int, required=True, help="channel id to test")
    parser.add_argument(
        "--trials", type=int, default=DEFAULT_TRIALS, help="trials per interval (default: 3)"
    )
    parser.add_argument(
        "--intervals",
        type=float,
        nargs="+",
        default=list(DEFAULT_INTERVALS),
        help="candidate intervals in seconds, slowest first",
    )
    parser.add_argument(
        "--i-know-this-changes-lights",
        dest="confirmed",
        action="store_true",
        help="required: confirms you accept that this switches a real circuit",
    )
    return parser.parse_args(argv)


async def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.confirmed:
        print(
            "Refusing to run: this script switches a real light off and on "
            "dozens of times.\nRe-run with --i-know-this-changes-lights once "
            "you have picked a channel nobody minds."
        )
        return 2

    host = args.host
    port = args.port
    if not host:
        _LOGGER.info("No --host or RAKO_BRIDGE_HOST; discovering...")
        description = await discover_bridge()
        host, port = description["host"], description["port"]
    _LOGGER.info(
        "Measuring room %d channel %d on %s, %d trials per interval",
        args.room,
        args.channel,
        host,
        args.trials,
    )
    intervals = tuple(sorted(args.intervals, reverse=True))
    return await measure(host, port, args.room, args.channel, intervals, args.trials)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(asyncio.run(main(sys.argv[1:])))
