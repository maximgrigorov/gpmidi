"""Hermetic contract tests for the live acceptance runner.

The runner itself talks to a deployed cluster and is deliberately excluded from
pytest collection. Its *fail-closed contract* is not — a runner that can report
PASS while an internal assertion failed is the exact defect this guards, so the
property is verified by the ordinary root gate.

Nothing here performs any I/O.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "live_acceptance", Path(__file__).resolve().parent / "e2e" / "live_acceptance.py"
)
live = importlib.util.module_from_spec(_SPEC)
sys.modules["live_acceptance"] = live
_SPEC.loader.exec_module(live)


def _result():
    return live.ScenarioResult(id=1, key="k", title="t")


class TestFailClosedContract:
    def test_a_scenario_with_only_passing_checks_passes(self):
        r = _result()
        live.Check(r).require(True, "holds")
        assert r.status == "pass"

    def test_a_failed_check_raises_and_records(self):
        r = _result()
        check = live.Check(r)
        check.require(True, "first holds")
        with pytest.raises(live.AcceptanceFailure):
            check.require(False, "second does not hold", "detail")
        assert r.status == "fail"
        assert [o.ok for o in r.observations] == [True, False]

    def test_a_scenario_cannot_pass_with_a_failed_observation(self):
        """Even if nothing raised, a recorded failure forces `fail`.

        This is the property the previous runner lacked: its `fail()` helper
        recorded a failure without raising, so pytest reported PASS.
        """
        r = _result()
        r.observations.append(live.Observation("silently failed", False, ""))
        assert r.status == "fail"

    def test_a_scenario_with_no_observations_fails(self):
        """A scenario that asserted nothing has proven nothing."""
        assert _result().status == "fail"

    def test_an_unexpected_exception_is_a_failure(self):
        runner = live.Runner()

        def boom(check, shared):
            check.require(True, "got this far")
            raise KeyError("missing")

        result = runner.run(1, "boom", "explodes", boom)
        assert result.status == "fail"
        assert "KeyError" in result.error
        assert result.traceback
        assert runner.failed == [result]

    def test_equal_records_both_sides_on_mismatch(self):
        r = _result()
        with pytest.raises(live.AcceptanceFailure):
            live.Check(r).equal(1, 2, "one equals two")
        assert "expected 2" in r.observations[0].detail

    def test_runner_keeps_going_after_a_failure_and_reports_every_scenario(self):
        runner = live.Runner()
        runner.run(1, "a", "fails", lambda c, s: c.require(False, "nope"))
        runner.run(2, "b", "passes", lambda c, s: c.require(True, "yep"))
        assert [r.status for r in runner.results] == ["fail", "pass"]
        assert len(runner.failed) == 1


class TestEvidenceDocument:
    def _evidence(self, *scenarios):
        runner = live.Runner()
        for idx, fn in enumerate(scenarios, start=1):
            runner.run(idx, f"s{idx}", f"scenario {idx}", fn)
        return runner.evidence({"base_url": "http://example.invalid"})

    def test_evidence_is_generated_from_recorded_checks(self):
        ev = self._evidence(
            lambda c, s: (c.require(True, "alpha"), c.fact("k", 7)),
        )
        assert ev["overall_status"] == "pass"
        assert ev["scenarios"][0]["observations"] == [
            {"description": "alpha", "ok": True, "detail": ""}
        ]
        assert ev["scenarios"][0]["facts"] == {"k": 7}

    def test_overall_status_is_fail_when_any_scenario_fails(self):
        ev = self._evidence(
            lambda c, s: c.require(True, "alpha"),
            lambda c, s: c.require(False, "beta"),
        )
        assert ev["overall_status"] == "fail"
        assert ev["failed"] == 1
        assert ev["passed"] == 1

    def test_evidence_is_json_serializable_and_stable(self):
        ev = self._evidence(lambda c, s: c.require(True, "alpha"))
        first = json.dumps(ev, sort_keys=True)
        assert json.loads(first)["scenario_count"] == 1

    def test_a_scenario_cannot_report_pass_with_a_failed_observation_in_evidence(self):
        runner = live.Runner()
        result = runner.run(1, "s", "t", lambda c, s: c.require(True, "alpha"))
        # Simulate a check that was recorded as failed without raising.
        result.observations.append(live.Observation("hidden failure", False, ""))
        ev = runner.evidence({})
        assert ev["scenarios"][0]["status"] == "fail"
        assert ev["overall_status"] == "fail"


class TestScenarioCoverage:
    def test_all_fourteen_required_scenarios_are_registered(self):
        ids = [s[0] for s in live.SCENARIOS]
        assert ids == list(range(1, 15))

    def test_every_scenario_has_a_distinct_key_and_callable(self):
        keys = [s[1] for s in live.SCENARIOS]
        assert len(set(keys)) == 14
        assert all(callable(s[3]) for s in live.SCENARIOS)

    def test_no_scenario_uses_skip(self):
        source = (
            Path(__file__).resolve().parent / "e2e" / "live_acceptance.py"
        ).read_text()
        assert "pytest.skip" not in source
        assert "SkipTest" not in source


class TestGeneratedFixtures:
    def test_fixtures_are_unique_per_run(self):
        """A cold-cache scenario needs an identity that was never computed."""
        assert live.make_midi(tag="x") != live.make_midi(tag="y")
        assert live.RUN_ID in live.make_midi(tag="x").decode("latin-1")

    def test_gp_fixture_carries_marker_and_repeat_metadata(self):
        import io

        import guitarpro

        song = guitarpro.parse(io.BytesIO(live.make_gp(n_measures=8)))
        assert len(song.measureHeaders) == 8
        markers = [
            (i, h.marker.title)
            for i, h in enumerate(song.measureHeaders)
            if h.marker and h.marker.title
        ]
        assert markers == [(4, "Chorus")]
        assert [i for i, h in enumerate(song.measureHeaders) if h.isRepeatOpen] == [2]
        assert [
            i for i, h in enumerate(song.measureHeaders) if h.repeatClose > 0
        ] == [5]

    def test_redundant_tempo_fixture_differs_only_in_event_count(self):
        """Consensus must not require equal raw tempo-event counts."""
        import io

        import mido

        plain = mido.MidiFile(file=io.BytesIO(live.make_midi(n_measures=8)))
        redundant = mido.MidiFile(file=io.BytesIO(live.make_redundant_tempo_midi(8)))

        def tempos(mid):
            return [m.tempo for t in mid.tracks for m in t if m.type == "set_tempo"]

        assert len(tempos(plain)) < len(tempos(redundant))
        assert set(tempos(plain)) == set(tempos(redundant))

    def test_click_fixtures_have_the_intended_phase_relationship(self):
        in_phase = live.make_wav_clicks(duration_sec=8.0, click_period=2.0, phase_offset=0.0)
        off_phase = live.make_wav_clicks(duration_sec=8.0, click_period=2.0, phase_offset=1.0)
        assert in_phase != off_phase
        assert in_phase[:4] == b"RIFF"
        assert off_phase[8:12] == b"WAVE"

    def test_structure_fixture_is_the_supported_version(self):
        doc = json.loads(live.make_structure(anchors=[{"source_measure": 1, "gp_measure": 5}]))
        assert doc["version"] == "1.0"
        assert doc["anchors"][0]["gp_measure"] == 5
