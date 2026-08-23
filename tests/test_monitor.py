"""Monitor and orchestrator invariants.

The monitor decides; the solver executes. These tests guard the boundary
between the two, and the properties that make a mid-day re-plan safe to
execute at all.
"""

from __future__ import annotations

import pytest

from fieldpilot.agents import rules_triage
from fieldpilot.agents.monitor import (
    MonitorAction,
    NoMonitor,
    RulesMonitor,
    Situation,
)
from fieldpilot.planning import solver
from fieldpilot.sim import orchestrator, scenario as scenario_mod
from fieldpilot.sim.engine import DAY_END_MIN, Simulator
from fieldpilot.sim.events import EventKind, SimEvent


def _situation(**overrides) -> Situation:
    base = dict(
        now_min=11 * 60,
        events=[],
        pending_orders=18,
        available_technicians=4,
        minutes_left=390,
        minutes_since_replan=120,
        replans_so_far=1,
        accumulated_overrun_min=0,
        capacity_jobs_left=10,
    )
    base.update(overrides)
    return Situation(**base)


def _event(kind: EventKind) -> SimEvent:
    return SimEvent(at_min=11 * 60, kind=kind, description="test")


def _day(seed: int = 42, n_orders: int = 26) -> Simulator:
    scn = scenario_mod.build(seed=seed, n_orders=n_orders)
    rules_triage.apply(scn.work_orders, scn.accounts)
    sim = Simulator(scn, seed=seed)
    sim.load_plan(solver.solve(scn.work_orders, scn.resources, time_limit_s=2))
    return sim


# --------------------------------------------------------------------------
# The rules monitor's judgement
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        EventKind.URGENT_ORDER_ARRIVED,
        EventKind.RESOURCE_UNAVAILABLE,
        EventKind.ORDER_CANCELED,
    ],
)
def test_structural_change_triggers_a_replan(kind: EventKind) -> None:
    decision = RulesMonitor().decide(_situation(events=[_event(kind)]))
    assert decision.action == MonitorAction.REPLAN


def test_a_small_delay_is_absorbed() -> None:
    decision = RulesMonitor().decide(
        _situation(events=[_event(EventKind.JOB_OVERRUNNING)], accumulated_overrun_min=12)
    )
    assert decision.action == MonitorAction.ABSORB


def test_accumulated_delay_eventually_triggers_a_replan() -> None:
    decision = RulesMonitor().decide(
        _situation(events=[_event(EventKind.JOB_OVERRUNNING)], accumulated_overrun_min=90)
    )
    assert decision.action == MonitorAction.REPLAN


def test_the_crew_is_not_redirected_twice_in_a_row() -> None:
    """Thrash protection. A crew re-routed every ten minutes stops trusting it."""
    decision = RulesMonitor().decide(
        _situation(
            events=[_event(EventKind.URGENT_ORDER_ARRIVED)], minutes_since_replan=5
        )
    )
    assert decision.action == MonitorAction.ABSORB


def test_nothing_is_replanned_at_the_end_of_the_day() -> None:
    decision = RulesMonitor().decide(
        _situation(events=[_event(EventKind.URGENT_ORDER_ARRIVED)], minutes_left=20)
    )
    assert decision.action == MonitorAction.ABSORB


def test_slack_means_there_is_nothing_to_gain() -> None:
    """If everything pending still fits, re-ordering it only moves promises."""
    decision = RulesMonitor().decide(
        _situation(
            events=[_event(EventKind.URGENT_ORDER_ARRIVED)],
            pending_orders=5,
            capacity_jobs_left=20,
        )
    )
    assert decision.action == MonitorAction.ABSORB


# --------------------------------------------------------------------------
# Mid-day re-planning has to be physically possible
# --------------------------------------------------------------------------


def test_a_midday_snapshot_starts_technicians_where_they_actually_are() -> None:
    sim = _day()
    sim.advance(11 * 60)

    for resource in sim.snapshot_resources():
        assert resource.shift_start_min >= sim.now_min, (
            "a technician cannot start a job before now"
        )
        assert resource.shift_start_min < resource.shift_end_min


def test_a_technician_mid_visit_is_not_free_until_they_finish() -> None:
    sim = _day()
    sim.advance(11 * 60)

    busy = {
        rid: state.busy_until_min
        for rid, state in sim.state.items()
        if state.current_order_id
    }
    snapshot = {r.resource_id: r for r in sim.snapshot_resources()}
    for rid, until in busy.items():
        if rid in snapshot:
            assert snapshot[rid].shift_start_min >= until


def test_snapshot_windows_never_point_into_the_past() -> None:
    sim = _day()
    sim.advance(12 * 60)
    for order in sim.snapshot_orders():
        assert order.window_start_min >= sim.now_min
        assert order.window_end_min > sim.now_min


def test_snapshot_excludes_work_already_done_or_under_way() -> None:
    sim = _day()
    sim.advance(12 * 60)

    pending = {o.work_order_id for o in sim.snapshot_orders()}
    executed = {v.work_order_id for v in sim.executed}
    in_flight = {s.current_order_id for s in sim.state.values() if s.current_order_id}

    assert not (pending & executed)
    assert not (pending & in_flight)


def test_an_unavailable_technician_is_absent_from_the_snapshot() -> None:
    sim = _day(seed=7)
    sim.advance(DAY_END_MIN)
    lost = {
        e.resource_id for e in sim.events if e.kind == EventKind.RESOURCE_UNAVAILABLE
    }
    if lost:
        available = {r.resource_id for r in sim.snapshot_resources()}
        assert not (lost & available)


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def test_an_unwatched_day_never_replans() -> None:
    sim = _day()
    log = orchestrator.run_day(sim, NoMonitor(), time_limit_s=2)
    assert log.replan_count == 0
    assert log.absorbed > 0


def test_a_watched_day_replans_and_reaches_the_end() -> None:
    sim = _day()
    log = orchestrator.run_day(sim, RulesMonitor(), time_limit_s=2)
    assert log.replan_count > 0
    assert sim.now_min == DAY_END_MIN


def test_replanning_never_rewrites_completed_work() -> None:
    """The property everything else rests on, asserted through the full loop
    rather than in isolation."""
    sim = _day()
    log = orchestrator.run_day(sim, RulesMonitor(), time_limit_s=2)

    ids = [v.work_order_id for v in sim.executed]
    assert len(ids) == len(set(ids)), "a visit was executed twice across re-plans"

    for resource in sim.scenario.resources:
        visits = sorted(
            (v for v in sim.executed if v.resource_id == resource.resource_id),
            key=lambda v: v.arrived_min,
        )
        for earlier, later in zip(visits, visits[1:]):
            assert later.arrived_min >= earlier.left_min


def test_certifications_still_hold_after_repeated_replanning() -> None:
    sim = _day()
    orchestrator.run_day(sim, RulesMonitor(), time_limit_s=2)

    by_res = {r.resource_id: r for r in sim.scenario.resources}
    by_order = {o.work_order_id: o for o in sim.all_orders}
    for visit in sim.executed:
        required = set(by_order[visit.work_order_id].required_characteristics)
        assert required <= set(by_res[visit.resource_id].characteristics)


def test_the_loop_is_deterministic() -> None:
    a = _day()
    b = _day()
    log_a = orchestrator.run_day(a, RulesMonitor(), time_limit_s=2)
    log_b = orchestrator.run_day(b, RulesMonitor(), time_limit_s=2)
    assert [r.at_min for r in log_a.replans] == [r.at_min for r in log_b.replans]
