"""Invariants of the simulated world.

The simulator is the measuring instrument for the whole project. If it drifts,
every number in the demo video is wrong and nobody would be able to tell. These
tests exist to keep the instrument honest.
"""

from __future__ import annotations

import pytest

from fieldpilot.agents import rules_triage
from fieldpilot.planning import solver
from fieldpilot.sim import report as report_mod
from fieldpilot.sim import scenario as scenario_mod
from fieldpilot.sim.engine import DAY_END_MIN, Outcome, Simulator
from fieldpilot.sim.events import EventKind

# Solving under a wall-clock limit is not reproducible: the same inputs on a
# busy machine explore fewer nodes and return a different plan. That made this
# file flaky. Counting improving solutions instead is machine-independent, and
# it also runs the suite roughly thirty times faster.
REPRODUCIBLE = 30


SEEDS = [42, 7, 2026]


def _sim_with_plan(seed: int) -> Simulator:
    scn = scenario_mod.build(seed=seed)
    rules_triage.apply(scn.work_orders, scn.accounts)
    sim = Simulator(scn, seed=seed)
    sim.load_plan(solver.solve(scn.work_orders, scn.resources, time_limit_s=10, solution_limit=REPRODUCIBLE))
    return sim


@pytest.mark.parametrize("seed", SEEDS)
def test_day_is_deterministic(seed: int) -> None:
    """A demo recorded in one take needs the day to be identical every rehearsal."""
    a = _sim_with_plan(seed)
    b = _sim_with_plan(seed)
    ev_a = [(e.at_min, e.kind, e.work_order_id) for e in a.advance(DAY_END_MIN)]
    ev_b = [(e.at_min, e.kind, e.work_order_id) for e in b.advance(DAY_END_MIN)]
    assert ev_a == ev_b


@pytest.mark.parametrize("seed", SEEDS)
def test_technician_never_in_two_places_during_execution(seed: int) -> None:
    sim = _sim_with_plan(seed)
    sim.advance(DAY_END_MIN)

    for resource in sim.scenario.resources:
        visits = sorted(
            (v for v in sim.executed if v.resource_id == resource.resource_id),
            key=lambda v: v.arrived_min,
        )
        for earlier, later in zip(visits, visits[1:]):
            assert later.arrived_min >= earlier.left_min


@pytest.mark.parametrize("seed", SEEDS)
def test_replanning_never_rewrites_what_already_happened(seed: int) -> None:
    """The core property the disruption loop depends on.

    A technician who is already inside a customer's home cannot be reassigned,
    and a completed visit cannot be un-completed. Swapping the plan at midday
    must leave both untouched.
    """
    sim = _sim_with_plan(seed)
    sim.advance(12 * 60)

    before = [(v.work_order_id, v.arrived_min, v.left_min, v.outcome) for v in sim.executed]
    in_flight = {
        s.current_order_id for s in sim.state.values() if s.current_order_id
    }

    fresh = solver.solve(sim.known_orders(), sim.available_resources(), time_limit_s=10, solution_limit=REPRODUCIBLE)
    sim.load_plan(fresh)

    after = [(v.work_order_id, v.arrived_min, v.left_min, v.outcome) for v in sim.executed]
    assert before == after

    # Nothing already under way may appear in anyone's new queue.
    queued = {oid for q in sim._queue.values() for oid in q}
    assert not (queued & in_flight)


@pytest.mark.parametrize("seed", SEEDS)
def test_a_visit_is_never_executed_twice(seed: int) -> None:
    sim = _sim_with_plan(seed)
    sim.advance(DAY_END_MIN)
    ids = [v.work_order_id for v in sim.executed]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("seed", SEEDS)
def test_emergencies_are_invisible_before_they_are_called_in(seed: int) -> None:
    """You cannot plan for a call that has not happened. If the scenario leaked
    future emergencies into the 8am plan, the agent would look prescient rather
    than responsive."""
    sim = _sim_with_plan(seed)
    early = {o.work_order_id for o in sim.known_orders()}
    assert not any(oid.startswith("wo-urg-") for oid in early)

    sim.advance(DAY_END_MIN)
    late = {o.work_order_id for o in sim.known_orders()} | {
        v.work_order_id for v in sim.executed
    }
    assert any(oid.startswith("wo-urg-") for oid in late)


@pytest.mark.parametrize("seed", SEEDS)
def test_a_lost_technician_stops_receiving_work(seed: int) -> None:
    sim = _sim_with_plan(seed)
    sim.advance(DAY_END_MIN)

    lost = [
        e.resource_id
        for e in sim.events
        if e.kind == EventKind.RESOURCE_UNAVAILABLE
    ]
    for resource_id in lost:
        moment = next(
            e.at_min
            for e in sim.events
            if e.kind == EventKind.RESOURCE_UNAVAILABLE and e.resource_id == resource_id
        )
        started_after = [
            v for v in sim.executed if v.resource_id == resource_id and v.arrived_min > moment
        ]
        assert not started_after, f"{resource_id} kept working after breaking down"


@pytest.mark.parametrize("seed", SEEDS)
def test_executed_visits_respect_certifications(seed: int) -> None:
    sim = _sim_with_plan(seed)
    sim.advance(DAY_END_MIN)
    by_res = {r.resource_id: r for r in sim.scenario.resources}
    by_order = {o.work_order_id: o for o in sim.all_orders}

    for visit in sim.executed:
        resource = by_res[visit.resource_id]
        order = by_order[visit.work_order_id]
        assert set(order.required_characteristics) <= set(resource.characteristics)


@pytest.mark.parametrize("seed", SEEDS)
def test_static_plan_degrades_under_disruption(seed: int) -> None:
    """The premise of the whole project, asserted rather than assumed.

    A plan that is optimal at 8am does not survive the day. If this test ever
    fails, the scenario has become too gentle to prove anything and the demo is
    no longer honest.
    """
    scn = scenario_mod.build(seed=seed)
    rules_triage.apply(scn.work_orders, scn.accounts)

    plan = solver.solve(scn.work_orders, scn.resources, time_limit_s=10, solution_limit=REPRODUCIBLE)
    planned_served = len(plan.bookings)

    sim = Simulator(scn, seed=seed)
    sim.load_plan(plan)
    sim.advance(DAY_END_MIN)

    actually_completed = sum(1 for v in sim.executed if v.outcome == Outcome.COMPLETED)
    assert actually_completed < planned_served

    day = report_mod.build(sim, "static")
    assert day.actionable_events > 0


def test_report_counts_add_up() -> None:
    sim = _sim_with_plan(42)
    sim.advance(DAY_END_MIN)
    day = report_mod.build(sim, "static")

    assert day.jobs_completed >= 0
    assert day.jobs_failed >= 0
    assert day.windows_missed >= 0
    assert 0.0 <= day.weighted_completion_pct <= 100.0
    assert day.safety_completed <= day.safety_total
    assert day.worst_lateness_min <= day.total_lateness_min
