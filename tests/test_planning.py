"""Invariants that must hold for any plan we would show a dispatcher.

These are not smoke tests. Each one corresponds to a way a schedule can be
wrong in a manner that only becomes visible when a technician is already
standing on the wrong doorstep.
"""

from __future__ import annotations

import pytest

from fieldpilot.agents import rules_triage
from fieldpilot.domain.models import Location
from fieldpilot.planning import baseline, metrics, solver
from fieldpilot.planning.travel import TravelMatrix
from fieldpilot.sim import scenario as scenario_mod

SEEDS = [42, 7, 101, 2026]


def _prepared(seed: int, n_orders: int = 26):
    scn = scenario_mod.build(seed=seed, n_orders=n_orders)
    rules_triage.apply(scn.work_orders, scn.accounts)
    locations = [o.location for o in scn.work_orders]
    locations += [r.start_location for r in scn.resources]
    matrix = TravelMatrix.estimated(locations + [locations[0]])
    return scn, matrix


@pytest.mark.parametrize("seed", SEEDS)
def test_scenario_is_reproducible(seed: int) -> None:
    a = scenario_mod.build(seed=seed)
    b = scenario_mod.build(seed=seed)
    assert [o.work_order_id for o in a.work_orders] == [o.work_order_id for o in b.work_orders]
    assert [o.duration_min for o in a.work_orders] == [o.duration_min for o in b.work_orders]
    assert a.accounts.keys() == b.accounts.keys()


@pytest.mark.parametrize("seed", SEEDS)
def test_solver_respects_certifications(seed: int) -> None:
    """A technician must never be sent to a job they are not certified for.

    This is the constraint with legal consequences: an uncertified person on a
    gas job is not an inefficiency, it is an incident.
    """
    scn, matrix = _prepared(seed)
    plan = solver.solve(scn.work_orders, scn.resources, matrix, time_limit_s=3)
    by_res = {r.resource_id: r for r in scn.resources}
    by_order = {o.work_order_id: o for o in scn.work_orders}

    for booking in plan.bookings:
        resource = by_res[booking.resource_id]
        order = by_order[booking.work_order_id]
        missing = set(order.required_characteristics) - set(resource.characteristics)
        assert not missing, f"{resource.name} lacks {missing} for {order.work_order_id}"


@pytest.mark.parametrize("seed", SEEDS)
def test_solver_respects_time_windows_and_shifts(seed: int) -> None:
    scn, matrix = _prepared(seed)
    plan = solver.solve(scn.work_orders, scn.resources, matrix, time_limit_s=3)
    by_res = {r.resource_id: r for r in scn.resources}
    by_order = {o.work_order_id: o for o in scn.work_orders}

    for booking in plan.bookings:
        order = by_order[booking.work_order_id]
        resource = by_res[booking.resource_id]
        assert order.window_start_min <= booking.arrival_min <= order.window_end_min
        assert booking.arrival_min >= resource.shift_start_min
        assert booking.departure_min <= resource.shift_end_min


@pytest.mark.parametrize("seed", SEEDS)
def test_no_technician_is_in_two_places_at_once(seed: int) -> None:
    """Consecutive visits must not overlap, and must leave time to drive."""
    scn, matrix = _prepared(seed)
    plan = solver.solve(scn.work_orders, scn.resources, matrix, time_limit_s=3)

    for resource in scn.resources:
        route = plan.bookings_for(resource.resource_id)
        for earlier, later in zip(route, route[1:]):
            assert later.arrival_min >= earlier.departure_min, (
                f"{resource.name} overlaps {earlier.work_order_id} and {later.work_order_id}"
            )
            assert later.arrival_min >= earlier.departure_min + later.travel_min, (
                f"{resource.name} teleports between "
                f"{earlier.work_order_id} and {later.work_order_id}"
            )


@pytest.mark.parametrize("seed", SEEDS)
def test_every_order_is_either_booked_once_or_reported_unserved(seed: int) -> None:
    """Nothing may vanish. A dropped job the dispatcher never sees is the worst
    failure mode a dispatch system has."""
    scn, matrix = _prepared(seed)
    plan = solver.solve(scn.work_orders, scn.resources, matrix, time_limit_s=3)

    booked = [b.work_order_id for b in plan.bookings]
    assert len(booked) == len(set(booked)), "an order was booked twice"

    accounted = set(booked) | set(plan.unserved_work_order_ids)
    assert accounted == {o.work_order_id for o in scn.work_orders}


@pytest.mark.parametrize("seed", SEEDS)
def test_baseline_also_respects_certifications(seed: int) -> None:
    """The comparison is only meaningful if the baseline plays by the same rules."""
    scn, matrix = _prepared(seed)
    plan = baseline.dispatch(scn.work_orders, scn.resources, matrix)
    by_res = {r.resource_id: r for r in scn.resources}
    by_order = {o.work_order_id: o for o in scn.work_orders}

    for booking in plan.bookings:
        resource = by_res[booking.resource_id]
        order = by_order[booking.work_order_id]
        assert set(order.required_characteristics) <= set(resource.characteristics)


@pytest.mark.parametrize("seed", SEEDS)
def test_solver_beats_baseline_on_weighted_coverage(seed: int) -> None:
    """The claim the demo video makes, asserted in CI so it cannot quietly rot."""
    scn, matrix = _prepared(seed)
    naive = baseline.dispatch(scn.work_orders, scn.resources, matrix)
    optimised = solver.solve(scn.work_orders, scn.resources, matrix, time_limit_s=3)

    m_naive = metrics.score(naive, scn.work_orders, scn.accounts)
    m_opt = metrics.score(optimised, scn.work_orders, scn.accounts)

    assert m_opt.weighted_coverage_pct > m_naive.weighted_coverage_pct
    assert m_opt.orders_served >= m_naive.orders_served


def test_triage_ranks_safety_above_routine() -> None:
    """The bridge from judgement to cost has to actually order things correctly."""
    scn = scenario_mod.build(seed=42)
    rules_triage.apply(scn.work_orders, scn.accounts)

    safety = [o for o in scn.work_orders if o.severity.value == "safety"]
    cosmetic = [o for o in scn.work_orders if o.severity.value == "cosmetic"]
    if safety and cosmetic:
        assert min(o.penalty_cost for o in safety) > max(o.penalty_cost for o in cosmetic)


def test_unqualified_work_is_reported_not_hidden() -> None:
    """An order nobody can serve must come back as unserved, not disappear."""
    scn = scenario_mod.build(seed=42, n_orders=6)
    rules_triage.apply(scn.work_orders, scn.accounts)
    scn.work_orders[0].required_characteristics = ["nuclear-welding"]

    plan = solver.solve(scn.work_orders, scn.resources, time_limit_s=2)
    assert scn.work_orders[0].work_order_id in plan.unserved_work_order_ids


def test_empty_day_does_not_crash() -> None:
    scn = scenario_mod.build(seed=1, n_orders=3)
    assert solver.solve([], scn.resources).bookings == []
    assert solver.solve(scn.work_orders, []).unserved_work_order_ids


def test_haversine_is_symmetric_and_zero_on_self() -> None:
    a = Location(lat=-34.60, lon=-58.38)
    b = Location(lat=-34.62, lon=-58.44)
    assert a.haversine_km(a) == pytest.approx(0.0, abs=1e-9)
    assert a.haversine_km(b) == pytest.approx(b.haversine_km(a), rel=1e-9)
    assert 3.0 < a.haversine_km(b) < 10.0
