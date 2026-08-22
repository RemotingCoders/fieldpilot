"""What goes wrong during a field service day.

These are not random perturbations chosen to make the demo dramatic. Each one
is a failure mode that shows up in real dispatch data, and each one breaks the
plan in a structurally different way:

- an overrun pushes every later visit on that route
- an absent customer wastes a slot and creates a new job to reschedule
- a missing part turns one visit into two
- an emergency arrives that no plan accounted for
- a technician disappears and their whole remaining route is orphaned

A system that only handles the first one is a delay tracker, not a dispatcher.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class EventKind(str, Enum):
    JOB_STARTED = "job_started"
    JOB_COMPLETED = "job_completed"
    JOB_OVERRUNNING = "job_overrunning"
    CUSTOMER_ABSENT = "customer_absent"
    PARTS_MISSING = "parts_missing"
    URGENT_ORDER_ARRIVED = "urgent_order_arrived"
    ORDER_CANCELED = "order_canceled"
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    WINDOW_MISSED = "window_missed"
    PLAN_REPLACED = "plan_replaced"


# Events the dispatcher must actually decide about, as opposed to events that
# merely record progress. The disruption monitor wakes on these.
ACTIONABLE = {
    EventKind.JOB_OVERRUNNING,
    EventKind.CUSTOMER_ABSENT,
    EventKind.PARTS_MISSING,
    EventKind.URGENT_ORDER_ARRIVED,
    EventKind.ORDER_CANCELED,
    EventKind.RESOURCE_UNAVAILABLE,
    EventKind.WINDOW_MISSED,
}


class SimEvent(BaseModel):
    """One thing that happened, at one minute of the simulated day."""

    at_min: int
    kind: EventKind
    resource_id: str | None = None
    work_order_id: str | None = None
    description: str = ""
    payload: dict = Field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return self.kind in ACTIONABLE

    def line(self) -> str:
        clock = f"{self.at_min // 60:02d}:{self.at_min % 60:02d}"
        mark = "!" if self.actionable else " "
        return f"{clock} {mark} {self.kind.value:<22} {self.description}"


class Disruption(BaseModel):
    """How likely each kind of trouble is, per job or per day.

    Defaults are deliberately on the harsh side of a real residential HVAC
    operation. A day that never breaks proves nothing about an agent whose
    entire purpose is to handle days that break.
    """

    # Rolled once per job, at the moment the technician arrives.
    p_overrun: float = 0.28
    p_customer_absent: float = 0.10
    p_parts_missing: float = 0.08

    # How much an overrun overruns, as a multiple of the estimate.
    overrun_min_factor: float = 1.3
    overrun_max_factor: float = 2.1

    # Ordinary variation on jobs that go fine.
    noise_min_factor: float = 0.85
    noise_max_factor: float = 1.15

    # Rolled once per simulated day.
    n_urgent_orders: int = 2
    p_cancellation: float = 0.35
    p_resource_lost: float = 0.25

    # A failed visit still burns time on site before the technician gives up.
    absent_visit_min: int = 12
    parts_visit_min_factor: float = 0.4
