"""What the memory bank must and must not learn."""

from __future__ import annotations

import pytest

from fieldpilot.memory.durations import (
    MAX_FACTOR,
    MIN_FACTOR,
    DurationMemory,
    Observation,
)


def _obs(ratio: float, resource_id="res-01", incident="boiler-no-heat", estimated=60):
    return Observation(
        resource_id=resource_id,
        incident_type_id=incident,
        estimated_min=estimated,
        actual_min=int(estimated * ratio),
    )


def test_empty_memory_does_not_perturb_the_plan():
    """The property everything else depends on: knowing nothing must cost
    nothing. A memory bank that nudges plans before it has evidence is worse
    than no memory bank."""
    memory = DurationMemory()
    assert memory.factor("res-01", "boiler-no-heat") == 1.0


def test_one_observation_barely_moves_the_estimate():
    """Learning, not overreacting. One slow visit is a slow visit."""
    memory = DurationMemory()
    memory.observe(_obs(2.0))
    assert 1.0 < memory.factor("res-01", "boiler-no-heat") < 1.20


def test_repeated_evidence_moves_it_much_further():
    memory = DurationMemory()
    for _ in range(30):
        memory.observe(_obs(1.5))
    assert memory.factor("res-01", "boiler-no-heat") > 1.35


def test_corrections_are_clamped():
    """A runaway correction sends a technician to jobs they cannot finish."""
    memory = DurationMemory()
    for _ in range(200):
        memory.observe(_obs(1.9))
    assert memory.factor("res-01", "boiler-no-heat") <= MAX_FACTOR

    fast = DurationMemory()
    for _ in range(200):
        fast.observe(_obs(0.35))
    assert fast.factor("res-01", "boiler-no-heat") >= MIN_FACTOR


def test_an_unseen_incident_inherits_what_is_known_about_the_person():
    """Being generally slow is a property of the technician and shows up on
    work they have never done before."""
    memory = DurationMemory()
    for _ in range(20):
        memory.observe(_obs(1.4, incident="boiler-no-heat"))
    assert memory.factor("res-01", "water-heater-install") > 1.2


def test_an_unknown_technician_gets_no_correction():
    memory = DurationMemory()
    for _ in range(20):
        memory.observe(_obs(1.4))
    assert memory.factor("res-99", "boiler-no-heat") == 1.0


@pytest.mark.parametrize("actual", [0, 1, 4])
def test_implausibly_short_visits_are_rejected(actual):
    memory = DurationMemory()
    kept = memory.observe(
        Observation("res-01", "boiler-no-heat", estimated_min=60, actual_min=actual)
    )
    assert not kept
    assert memory.factor("res-01", "boiler-no-heat") == 1.0


def test_absurd_ratios_are_rejected_rather_than_clamped():
    """Clamping a bad reading still lets it drag the mean for days."""
    memory = DurationMemory()
    assert not memory.observe(_obs(12.0))
    assert memory.observations == 0


def test_only_completed_visits_are_learned_from():
    """A visit that ended because nobody was home says nothing about how long
    the work takes. Learning from it teaches that the job is quick."""
    from fieldpilot.sim.engine import ExecutedVisit, Outcome

    class _Order:
        incident_type_id = "boiler-no-heat"
        duration_min = 60

    visits = [
        ExecutedVisit(
            work_order_id="wo-1", resource_id="res-01",
            arrived_min=500, left_min=510, outcome=Outcome.ABSENT,
        ),
        ExecutedVisit(
            work_order_id="wo-1", resource_id="res-01",
            arrived_min=500, left_min=515, outcome=Outcome.NEEDS_PARTS,
        ),
        ExecutedVisit(
            work_order_id="wo-1", resource_id="res-01",
            arrived_min=500, left_min=590, outcome=Outcome.COMPLETED,
        ),
    ]
    memory = DurationMemory()
    kept = memory.observe_visits(visits, {"wo-1": _Order()}, {"res-01": object()})
    assert kept == 1
    assert memory.observations == 1


def test_memory_survives_a_round_trip_to_disk(tmp_path):
    path = tmp_path / "memory.json"
    memory = DurationMemory(path=path)
    for _ in range(12):
        memory.observe(_obs(1.45))
    memory.save()

    reloaded = DurationMemory.load(path)
    assert reloaded.observations == 12
    assert reloaded.factor("res-01", "boiler-no-heat") == pytest.approx(
        memory.factor("res-01", "boiler-no-heat")
    )


def test_corrupt_memory_file_is_empty_memory_not_a_crash(tmp_path):
    path = tmp_path / "memory.json"
    path.write_text("{not json at all")
    memory = DurationMemory.load(path)
    assert memory.observations == 0
    assert memory.factor("res-01", "boiler-no-heat") == 1.0


def test_saving_without_a_path_is_harmless():
    DurationMemory().save()


def test_summary_shows_the_largest_corrections_first():
    memory = DurationMemory()
    for _ in range(20):
        memory.observe(_obs(1.6, incident="boiler-no-heat"))
        memory.observe(_obs(1.02, incident="thermostat"))
    lines = memory.summary_lines()
    assert "boiler-no-heat" in lines[0]


def test_relative_memory_normalises_the_common_correction_away():
    """Everyone running twenty percent long is not a fact about anyone."""
    memory = DurationMemory(relative=True)
    for _ in range(40):
        memory.observe(_obs(1.2, resource_id="res-01"))
        memory.observe(_obs(1.2, resource_id="res-02"))
    assert memory.factor("res-01", "boiler-no-heat") == pytest.approx(1.0, abs=0.02)


def test_relative_memory_keeps_the_difference_between_technicians():
    memory = DurationMemory(relative=True)
    for _ in range(40):
        memory.observe(_obs(1.4, resource_id="res-01"))
        memory.observe(_obs(1.0, resource_id="res-02"))
    assert memory.factor("res-01", "boiler-no-heat") > 1.05
    assert memory.factor("res-02", "boiler-no-heat") < 0.95


def test_fleet_mean_of_empty_memory_is_one():
    assert DurationMemory().fleet_mean() == 1.0


def test_relative_memory_still_returns_one_when_it_knows_nothing():
    assert DurationMemory(relative=True).factor("res-01", "x") == 1.0


def test_the_solver_ignores_true_technician_speed():
    """The oracle leak this module was written to close.

    The scenario gives every technician a `duration_factor`. The simulator uses
    it, because it is what actually happens. The planner must not, because no
    dispatcher is handed it — and a plan built on it flatters itself.

    Checked behaviourally rather than by reading the source: make one
    technician absurdly slow and confirm the plan does not react. A grep for
    the attribute name would pass on a comment.
    """
    from fieldpilot.agents import rules_triage
    from fieldpilot.planning import solver
    from fieldpilot.sim import scenario as scenario_mod

    def plan_with(factor: float):
        scn = scenario_mod.build(seed=42)
        rules_triage.apply(scn.work_orders, scn.accounts)
        for resource in scn.resources:
            resource.duration_factor = factor
        plan = solver.solve(
            scn.work_orders, scn.resources, time_limit_s=10, solution_limit=30
        )
        return sorted(
            (b.work_order_id, b.resource_id, b.arrival_min) for b in plan.bookings
        )

    assert plan_with(1.0) == plan_with(3.0)
