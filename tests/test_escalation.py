"""What must reach a person, and in what order."""

from __future__ import annotations

from fieldpilot.agents import escalation as esc
from fieldpilot.agents.escalation import Queue, Urgency
from fieldpilot.domain.models import Location, Severity, WorkOrder


def _order(**kwargs) -> WorkOrder:
    base = dict(
        work_order_id="wo-001",
        account_id="acc-001",
        incident_type_id="annual-service",
        location=Location(lat=-34.6, lon=-58.4, address="x"),
        window_start_min=480,
        window_end_min=1050,
        duration_min=60,
        required_characteristics=["hvac"],
        severity=Severity.DEGRADED,
    )
    base.update(kwargs)
    return WorkOrder(**base)


def test_an_ordinary_dropped_job_is_not_an_escalation():
    """A day that fits everything was overstaffed. Dropping work is normal, and
    a queue that says so about every job is a queue nobody reads."""
    assert esc.from_unserved([_order()]) == []


def test_an_unserved_safety_call_blocks():
    items = esc.from_unserved([_order(severity=Severity.SAFETY)])
    assert items[0].urgency is Urgency.BLOCKING


def test_a_job_waiting_a_week_stops_being_a_scheduling_outcome():
    items = esc.from_unserved([_order(days_waiting=9)])
    assert items[0].urgency is Urgency.SAME_DAY


def test_a_repeatedly_rescheduled_customer_is_escalated():
    items = esc.from_unserved([_order(reschedule_count=2)])
    assert items[0].urgency is Urgency.SAME_DAY


def test_out_of_service_gets_seen_but_does_not_block():
    items = esc.from_unserved([_order(severity=Severity.OUT_OF_SERVICE)])
    assert items[0].urgency is Urgency.REVIEW


def test_safety_outranks_every_other_reason_on_the_same_job():
    """A safety call that has also waited nine days is still a safety call."""
    items = esc.from_unserved([
        _order(severity=Severity.SAFETY, days_waiting=9, reschedule_count=3)
    ])
    assert len(items) == 1
    assert items[0].urgency is Urgency.BLOCKING


def test_one_missed_visit_is_not_an_escalation():
    from fieldpilot.sim.engine import ExecutedVisit, Outcome

    visits = [ExecutedVisit(
        work_order_id="wo-1", resource_id="res-01",
        arrived_min=500, left_min=510, outcome=Outcome.ABSENT,
    )]
    assert esc.from_visits(visits) == []


def test_the_same_customer_missed_twice_is_a_phone_call():
    from fieldpilot.sim.engine import ExecutedVisit, Outcome

    visits = [
        ExecutedVisit(work_order_id="wo-1", resource_id="res-01",
                      arrived_min=500, left_min=510, outcome=Outcome.ABSENT),
        ExecutedVisit(work_order_id="wo-1", resource_id="res-02",
                      arrived_min=700, left_min=710, outcome=Outcome.ABSENT),
    ]
    items = esc.from_visits(visits)
    assert len(items) == 1
    assert items[0].urgency is Urgency.SAME_DAY


def test_completed_visits_never_escalate():
    from fieldpilot.sim.engine import ExecutedVisit, Outcome

    visits = [
        ExecutedVisit(work_order_id="wo-1", resource_id="res-01",
                      arrived_min=500, left_min=560, outcome=Outcome.COMPLETED)
        for _ in range(5)
    ]
    assert esc.from_visits(visits) == []


class _Geo:
    def __init__(self, in_area=True, vague=False, source="maps"):
        self.in_service_area, self.vague, self.source = in_area, vague, source


class _Result:
    needs_human = False
    confidence = 0.9
    reasoning = "clear"


class _Outcome:
    def __init__(self, geocode=None, result=None):
        self.geocode, self.result = geocode, result if result is not None else _Result()


def test_an_offline_geocode_blocks_dispatch():
    """The bug that started this: plausible coordinates from a hash."""
    items = esc.from_intake(_Outcome(geocode=_Geo(source="offline")))
    assert items[0].urgency is Urgency.BLOCKING


def test_an_out_of_area_geocode_blocks():
    items = esc.from_intake(_Outcome(geocode=_Geo(in_area=False)))
    assert items[0].urgency is Urgency.BLOCKING


def test_a_vague_geocode_is_reviewed_not_blocked():
    items = esc.from_intake(_Outcome(geocode=_Geo(vague=True)))
    assert items[0].urgency is Urgency.REVIEW


def test_a_clean_intake_raises_nothing():
    assert esc.from_intake(_Outcome(geocode=_Geo())) == []


def test_intake_failing_entirely_still_reaches_a_person():
    class _Broken:
        result = None
        error = "model unavailable"

    items = esc.from_intake(_Broken())
    assert items[0].urgency is Urgency.SAME_DAY


def test_a_rejected_message_is_an_operations_signal_not_a_customer_one():
    class _Draft:
        violations = ["guarantee"]

    items = esc.from_comms([_Draft()])
    assert items[0].urgency is Urgency.REVIEW
    assert "nothing wrong reached the customer" in items[0].why


def test_a_clean_draft_raises_nothing():
    class _Draft:
        violations = []

    assert esc.from_comms([_Draft()]) == []


def test_the_queue_puts_blocking_first():
    queue = Queue.build(
        esc.from_unserved([_order(days_waiting=9)]),
        esc.from_unserved([_order(work_order_id="wo-002", severity=Severity.SAFETY)]),
    )
    assert queue.items[0].urgency is Urgency.BLOCKING
    assert len(queue.blocking) == 1


def test_an_empty_queue_says_so_rather_than_printing_nothing():
    """Silence and 'all clear' look identical on a terminal, and only one of
    them means the check ran."""
    assert Queue.build().lines() == ["nothing needs a person tonight"]
