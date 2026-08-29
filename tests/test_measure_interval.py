"""Tests for the live measurement tool's decision logic.

``scripts/measure_interval.py`` only ever runs against real hardware, which is
exactly why the parts that do not need hardware -- what counts as a lost trial,
and which interval gets recommended off the back of it -- are tested here.  Its
answer becomes a library default, so it had better be arithmetic we trust.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from python_rako.const import CommandType
from python_rako.exceptions import RakoCommandError, RakoConnectionError

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_interval.py"
_spec = importlib.util.spec_from_file_location("measure_interval", _SCRIPT)
assert _spec is not None and _spec.loader is not None
measure_interval = importlib.util.module_from_spec(_spec)
sys.modules["measure_interval"] = measure_interval
_spec.loader.exec_module(measure_interval)

Trial = measure_interval.Trial


def _runs(interval: float, *ok: bool) -> list:
    trials = []
    for succeeded in ok:
        trial = Trial(interval)
        if not succeeded:
            trial.lost.append("on")
        trials.append(trial)
    return trials


class _FakeBridge:
    """Records what the script sends, and can lose an echo on demand."""

    def __init__(self, lose_on_call: set[int] | None = None) -> None:
        self.calls: list[tuple] = []
        self.lose_on_call = lose_on_call or set()

    async def send_command(self, spec, *, paced=True, retries=1, **kwargs):
        self.calls.append((spec, paced, retries))
        if len(self.calls) in self.lose_on_call:
            raise RakoCommandError("no echo")
        return object()


async def test_a_clean_pair_is_a_passing_trial():
    bridge = _FakeBridge()
    trial = await measure_interval.run_trial(bridge, 7, 2, 0.0)
    assert trial.ok
    assert trial.describe() == "ok"
    assert [spec.expected_level for spec, _, _ in bridge.calls] == [0, 255]


async def test_the_measurement_never_paces_and_never_retries():
    """Pacing or retrying the probe would measure the library, not the bridge."""
    bridge = _FakeBridge()
    await measure_interval.run_trial(bridge, 7, 2, 0.0)
    assert all(paced is False and retries == 0 for _, paced, retries in bridge.calls)
    assert all(spec.command is CommandType.SET_LEVEL for spec, _, _ in bridge.calls)


async def test_a_missing_echo_fails_the_trial():
    bridge = _FakeBridge(lose_on_call={2})
    trial = await measure_interval.run_trial(bridge, 7, 2, 0.0)
    assert not trial.ok
    assert trial.describe() == "lost on"


async def test_a_transport_error_stops_the_trial_rather_than_scoring_it():
    """A dead socket is not evidence about the bridge's pacing."""

    class _BrokenBridge(_FakeBridge):
        async def send_command(self, spec, **kwargs):
            raise RakoConnectionError("socket gone")

    trial = await measure_interval.run_trial(_BrokenBridge(), 7, 2, 0.0)
    assert not trial.ok
    assert trial.error is not None
    assert "socket gone" in trial.describe()


async def test_an_unverified_send_is_an_error_not_a_pass():
    """Without a working listener there is nothing to measure."""

    class _UnverifiedBridge(_FakeBridge):
        async def send_command(self, spec, **kwargs):
            return None

    trial = await measure_interval.run_trial(_UnverifiedBridge(), 7, 2, 0.0)
    assert not trial.ok
    assert "listener" in str(trial.error)


def test_the_recommendation_is_the_fastest_clean_interval_plus_margin():
    results = {
        2.0: _runs(2.0, True, True, True),
        1.0: _runs(1.0, True, True, True),
        0.5: _runs(0.5, True, False, True),
    }
    assert measure_interval.recommend(results, 3) == pytest.approx(1.25)


def test_an_interval_that_was_cut_short_is_not_recommended():
    """An early stop leaves a partial row; it has not earned 3/3."""
    results = {
        2.0: _runs(2.0, True, True, True),
        1.0: _runs(1.0, True),
    }
    assert measure_interval.recommend(results, 3) == pytest.approx(2.5)


def test_no_recommendation_when_nothing_was_clean():
    results = {2.0: _runs(2.0, False, True, True)}
    assert measure_interval.recommend(results, 3) is None


async def test_the_script_refuses_to_run_without_the_confirmation_flag(capsys):
    code = await measure_interval.main(["--room", "7", "--channel", "2"])
    assert code == 2
    assert "Refusing to run" in capsys.readouterr().out
