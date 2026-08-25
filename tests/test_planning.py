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

# Solving under a wall-clock limit is not reproducible: the same inputs on a
# busy machine explore fewer nodes and return a different plan. That made this
# file flaky. Counting improving solutions instead is machine-independent, and
# it also runs the suite roughly thirty times faster.
REPRODUCIBLE = 30


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
    plan = solver.solve(scn.work_orders, scn.resources, matrix, time_limit_s=10, solution_limit=REPRODUCIBLE)
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
    plan = solver.solve(scn.work_orders, scn.resources, matrix, time_limit_s=10, solution_limit=REPRODUCIBLE)
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
    plan = solver.solve(scn.work_orders, scn.resources, matrix, time_limit_s=10, solution_limit=REPRODUCIBLE)

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
    plan = solver.solve(scn.work_orders, scn.resources, matrix, time_limit_s=10, solution_limit=REPRODUCIBLE)

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
    optimised = solver.solve(scn.work_orders, scn.resources, matrix, time_limit_s=10, solution_limit=REPRODUCIBLE)

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

    plan = solver.solve(scn.work_orders, scn.resources, time_limit_s=10, solution_limit=REPRODUCIBLE)
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


def test_a_solution_limited_solve_is_marked_reproducible():
    scn = scenario_mod.build(seed=42)
    rules_triage.apply(scn.work_orders, scn.accounts)
    plan = solver.solve(
        scn.work_orders, scn.resources, time_limit_s=10, solution_limit=REPRODUCIBLE
    )
    assert plan.reproducible


def test_a_wall_clock_solve_is_not_claimed_reproducible():
    """The flag exists to stop a number being trusted more than it should be."""
    scn = scenario_mod.build(seed=42)
    rules_triage.apply(scn.work_orders, scn.accounts)
    plan = solver.solve(scn.work_orders, scn.resources, time_limit_s=1)
    assert not plan.reproducible


def test_the_same_solution_limit_gives_the_same_plan_twice():
    """The property the flaky determinism test actually needed."""
    def once():
        scn = scenario_mod.build(seed=42)
        rules_triage.apply(scn.work_orders, scn.accounts)
        plan = solver.solve(
            scn.work_orders, scn.resources, time_limit_s=10, solution_limit=REPRODUCIBLE
        )
        return sorted(
            (b.work_order_id, b.resource_id, b.arrival_min) for b in plan.bookings
        )

    assert once() == once()


# ----------------------------------------------------------------------
# The Cloud Storage cache layer
# ----------------------------------------------------------------------

class _FakeBlob:
    def __init__(self, store, key):
        self.store, self.key = store, key

    def download_as_text(self):
        if self.key not in self.store:
            raise FileNotFoundError(self.key)
        return self.store[self.key]

    def upload_from_string(self, payload):
        self.store[self.key] = payload


class _FakeBucket:
    def __init__(self, store):
        self.store = store

    def blob(self, key):
        return _FakeBlob(self.store, key)


def _gcs_env(tmp_path, monkeypatch, store):
    from fieldpilot.planning import geocode as geocode_mod

    monkeypatch.setattr(geocode_mod, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(geocode_mod, "_gcs_synced", False)
    monkeypatch.setattr(geocode_mod, "_bucket", lambda: _FakeBucket(store))
    return geocode_mod


def test_remote_cache_is_pulled_and_merged_once(tmp_path, monkeypatch):
    """A fresh instance inherits every address any other instance paid for."""
    import json

    store = {"geocode/cache.json": json.dumps(
        {"Av. Cabildo 2340": {"lat": -34.56, "lon": -58.46, "precision": "ROOFTOP"}}
    )}
    geocode_mod = _gcs_env(tmp_path, monkeypatch, store)

    cache = geocode_mod._load_cache()
    assert "Av. Cabildo 2340" in cache


def test_saving_pushes_to_the_bucket(tmp_path, monkeypatch):
    import json

    store = {}
    geocode_mod = _gcs_env(tmp_path, monkeypatch, store)
    geocode_mod._save_cache({"x": {"lat": 1.0, "lon": 2.0}})
    assert "x" in json.loads(store["geocode/cache.json"])


def test_local_entries_win_over_remote_on_merge(tmp_path, monkeypatch):
    import json

    store = {"geocode/cache.json": json.dumps({"a": {"lat": 0.0, "lon": 0.0}})}
    geocode_mod = _gcs_env(tmp_path, monkeypatch, store)
    (tmp_path / "cache.json").write_text(json.dumps({"a": {"lat": 9.9, "lon": 9.9}}))

    cache = geocode_mod._load_cache()
    assert cache["a"]["lat"] == 9.9


def test_a_broken_bucket_is_a_cold_cache_not_a_crash(tmp_path, monkeypatch):
    """The backend contract of every optional layer in this project."""
    from fieldpilot.planning import geocode as geocode_mod

    class _Boom:
        def blob(self, key):
            raise ConnectionError("no network")

    monkeypatch.setattr(geocode_mod, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(geocode_mod, "_gcs_synced", False)
    monkeypatch.setattr(geocode_mod, "_bucket", lambda: _Boom())

    assert geocode_mod._load_cache() == {}
    geocode_mod._save_cache({"x": {"lat": 1.0, "lon": 2.0}})  # must not raise


def test_no_bucket_configured_means_local_only(tmp_path, monkeypatch):
    from fieldpilot.planning import geocode as geocode_mod

    monkeypatch.setattr(geocode_mod, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(geocode_mod, "_gcs_synced", False)
    monkeypatch.delenv("FIELDPILOT_GCS_BUCKET", raising=False)

    assert geocode_mod._load_cache() == {}
