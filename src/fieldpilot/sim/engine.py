"""The world that executes a plan and breaks it.

The simulator knows nothing about agents. It advances a clock, sends
technicians to the visits the current plan assigns them, and reports what
actually happened. Plans can be swapped mid-day: anything already started is
untouchable, everything still pending follows the new plan.

That single property — replan without rewriting history — is what the whole
disruption loop is built on, so it is enforced here rather than trusted.
"""

from __future__ import annotations

import random
from enum import Enum

from pydantic import BaseModel, Field

from fieldpilot.domain.models import (
    Account,
    BookableResource,
    Location,
    Plan,
    Severity,
    SlaTier,
    WorkOrder,
)
from fieldpilot.planning.travel import TravelMatrix
from fieldpilot.sim.events import Disruption, EventKind, SimEvent
from fieldpilot.sim.scenario import INCIDENT_TYPES, LAT_RANGE, LON_RANGE, Scenario

DAY_START_MIN = 8 * 60
DAY_END_MIN = 17 * 60 + 30


class Outcome(str, Enum):
    COMPLETED = "completed"
    ABSENT = "absent"
    NEEDS_PARTS = "needs_parts"


class ExecutedVisit(BaseModel):
    work_order_id: str
    resource_id: str
    arrived_min: int
    left_min: int
    outcome: Outcome
    planned_arrival_min: int | None = None

    @property
    def lateness_min(self) -> int:
        """Minutes later than the plan promised. Never negative."""
        if self.planned_arrival_min is None:
            return 0
        return max(0, self.arrived_min - self.planned_arrival_min)


class ResourceState(BaseModel):
    resource_id: str
    node: int                    # where the technician physically is
    free_at_min: int
    available: bool = True
    current_order_id: str | None = None
    # When the technician actually reaches the door. Between committing to a
    # visit and this minute they are driving, and a van that breaks down while
    # driving does not arrive.
    arrival_min: int = 0
    busy_until_min: int = 0
    planned_departure_min: int | None = None
    overrun_flagged: bool = False
    outcome_pending: Outcome = Outcome.COMPLETED


class Simulator:
    """Runs one day. Deterministic for a given seed."""

    def __init__(
        self,
        scenario: Scenario,
        disruption: Disruption | None = None,
        seed: int | None = None,
    ) -> None:
        self.scenario = scenario
        self.disruption = disruption or Disruption()
        self.rng = random.Random(seed if seed is not None else scenario.seed + 977)

        self.now_min = DAY_START_MIN
        self.events: list[SimEvent] = []
        self.executed: list[ExecutedVisit] = []

        # Emergencies are generated up front so their locations can go into the
        # travel matrix, but they stay invisible until their arrival minute.
        self._urgent: list[tuple[int, WorkOrder, Account]] = []
        self._make_urgent_orders()

        self.all_orders: list[WorkOrder] = list(scenario.work_orders) + [
            o for _, o, _ in self._urgent
        ]
        self.accounts: dict[str, Account] = dict(scenario.accounts)
        for _, _, account in self._urgent:
            self.accounts[account.account_id] = account

        self._order_index = {o.work_order_id: i for i, o in enumerate(self.all_orders)}
        locations: list[Location] = [o.location for o in self.all_orders]
        locations += [r.start_location for r in scenario.resources]
        self.matrix = TravelMatrix.estimated(locations)
        self._n_orders = len(self.all_orders)

        self.state: dict[str, ResourceState] = {}
        for i, resource in enumerate(scenario.resources):
            self.state[resource.resource_id] = ResourceState(
                resource_id=resource.resource_id,
                node=self._n_orders + i,
                free_at_min=resource.shift_start_min,
            )

        self._resources = {r.resource_id: r for r in scenario.resources}
        self._queue: dict[str, list[str]] = {r.resource_id: [] for r in scenario.resources}
        self._planned_arrival: dict[str, int] = {}
        self._settled: set[str] = set()     # orders finished, failed or cancelled
        self._started: set[str] = set()

        self._day_events = self._make_day_events()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _make_urgent_orders(self) -> None:
        """Emergencies that arrive after the day has already been planned."""
        gas = next(t for t in INCIDENT_TYPES if t.incident_type_id == "gas-smell")
        breakdown = next(t for t in INCIDENT_TYPES if t.incident_type_id == "boiler-no-heat")

        for i in range(self.disruption.n_urgent_orders):
            incident = gas if i % 2 == 0 else breakdown
            arrives = self.rng.randint(9 * 60, 14 * 60)
            location = Location(
                lat=self.rng.uniform(*LAT_RANGE),
                lon=self.rng.uniform(*LON_RANGE),
                address="Emergency call, Buenos Aires",
            )
            account = Account(
                account_id=f"acc-urg-{i:02d}",
                name=f"Emergency caller {i + 1}",
                sla_tier=self.rng.choice([SlaTier.PLATINUM, SlaTier.GOLD]),
                annual_value_usd=self.rng.uniform(5_000, 30_000),
                location=location,
            )
            order = WorkOrder(
                work_order_id=f"wo-urg-{i:02d}",
                account_id=account.account_id,
                incident_type_id=incident.incident_type_id,
                location=location,
                window_start_min=arrives,
                window_end_min=DAY_END_MIN,
                duration_min=incident.default_duration_min,
                required_characteristics=list(incident.required_characteristics),
                severity=incident.default_severity,
                penalty_cost=120_000 if incident.default_severity == Severity.SAFETY else 30_000,
                triage_rationale="inbound emergency",
            )
            self._urgent.append((arrives, order, account))

    def _make_day_events(self) -> list[SimEvent]:
        """One-off disruptions scheduled across the day."""
        planned: list[SimEvent] = []

        for arrives, order, account in self._urgent:
            planned.append(
                SimEvent(
                    at_min=arrives,
                    kind=EventKind.URGENT_ORDER_ARRIVED,
                    work_order_id=order.work_order_id,
                    description=(
                        f"{account.name} ({account.sla_tier.value}) reports "
                        f"{order.incident_type_id}"
                    ),
                    payload={"severity": order.severity.value},
                )
            )

        if self.rng.random() < self.disruption.p_cancellation:
            victim = self.rng.choice(self.scenario.work_orders)
            planned.append(
                SimEvent(
                    at_min=self.rng.randint(9 * 60, 13 * 60),
                    kind=EventKind.ORDER_CANCELED,
                    work_order_id=victim.work_order_id,
                    description=(
                        f"{self.scenario.accounts[victim.account_id].name} cancels; "
                        f"a slot just opened up"
                    ),
                )
            )

        if self.rng.random() < self.disruption.p_resource_lost:
            resource = self.rng.choice(self.scenario.resources)
            planned.append(
                SimEvent(
                    at_min=self.rng.randint(10 * 60, 14 * 60),
                    kind=EventKind.RESOURCE_UNAVAILABLE,
                    resource_id=resource.resource_id,
                    description=f"{resource.name} is out for the rest of the day (van breakdown)",
                )
            )

        return sorted(planned, key=lambda e: e.at_min)

    # ------------------------------------------------------------------
    # Plan handling
    # ------------------------------------------------------------------

    def load_plan(self, plan: Plan) -> SimEvent | None:
        """Adopt a plan for everything not already started.

        Visits in progress or already finished are never rewritten: a
        technician standing in someone's kitchen is a fact, not a proposal.
        """
        replaced = 0
        for resource in self.scenario.resources:
            rid = resource.resource_id
            bookings = [
                b
                for b in plan.bookings_for(rid)
                if b.work_order_id not in self._settled
                and b.work_order_id not in self._started
            ]
            self._queue[rid] = [b.work_order_id for b in bookings]
            for booking in bookings:
                self._planned_arrival[booking.work_order_id] = booking.arrival_min
            replaced += len(bookings)

        if self.now_min > DAY_START_MIN:
            event = SimEvent(
                at_min=self.now_min,
                kind=EventKind.PLAN_REPLACED,
                description=f"plan replaced; {replaced} visits still pending",
            )
            self.events.append(event)
            return event
        return None

    def location_of(self, resource_id: str) -> Location:
        """Where this technician physically is right now."""
        node = self.state[resource_id].node
        if node < self._n_orders:
            return self.all_orders[node].location
        return self.scenario.resources[node - self._n_orders].start_location

    def snapshot_resources(self) -> list[BookableResource]:
        """The fleet as it exists at this minute, ready to hand to the solver.

        The solver assumes technicians begin the day at their base. Re-planning
        at eleven in the morning means starting from where each of them actually
        is, and from the minute they actually come free — a technician halfway
        through a boiler repair is not available until they finish it.

        Unavailable technicians are simply absent from the list, which is what
        makes their orphaned route re-plannable onto everyone else.
        """
        snapshot: list[BookableResource] = []
        for resource in self.scenario.resources:
            state = self.state[resource.resource_id]
            if not state.available:
                continue
            free_at = max(state.free_at_min, self.now_min)
            if state.current_order_id:
                free_at = max(free_at, state.busy_until_min)
            if free_at >= resource.shift_end_min:
                continue
            snapshot.append(
                resource.model_copy(
                    update={
                        "start_location": self.location_of(resource.resource_id),
                        "shift_start_min": free_at,
                    }
                )
            )
        return snapshot

    def snapshot_orders(self) -> list[WorkOrder]:
        """Pending work, with time windows clipped to what is still reachable.

        An order whose window opened at 09:00 cannot be served at 09:00 once it
        is 11:20. Leaving the original window in place would let the solver
        build a plan that is already impossible.
        """
        pending: list[WorkOrder] = []
        for order in self.known_orders():
            if order.window_end_min <= self.now_min:
                continue
            pending.append(
                order.model_copy(
                    update={"window_start_min": max(order.window_start_min, self.now_min)}
                )
            )
        return pending

    def known_orders(self) -> list[WorkOrder]:
        """Orders the dispatcher is aware of right now, still needing service."""
        known = [
            o
            for o in self.scenario.work_orders
            if o.work_order_id not in self._settled and o.work_order_id not in self._started
        ]
        known += [
            o
            for arrives, o, _ in self._urgent
            if arrives <= self.now_min and o.work_order_id not in self._settled
        ]
        return known

    def available_resources(self) -> list[BookableResource]:
        return [
            r
            for r in self.scenario.resources
            if self.state[r.resource_id].available
        ]

    # ------------------------------------------------------------------
    # Clock
    # ------------------------------------------------------------------

    def advance(self, to_min: int) -> list[SimEvent]:
        """Run the world forward. Returns everything that happened on the way."""
        to_min = min(to_min, DAY_END_MIN)
        emitted: list[SimEvent] = []

        while self.now_min < to_min:
            self.now_min += 1
            emitted.extend(self._tick())

        return emitted

    def _tick(self) -> list[SimEvent]:
        t = self.now_min
        out: list[SimEvent] = []

        for event in [e for e in self._day_events if e.at_min == t]:
            out.append(event)
            self.events.append(event)
            if event.kind == EventKind.ORDER_CANCELED and event.work_order_id:
                self._settled.add(event.work_order_id)
            if event.kind == EventKind.RESOURCE_UNAVAILABLE and event.resource_id:
                state = self.state[event.resource_id]
                state.available = False
                # Their remaining route is orphaned and must be re-planned or lost.
                self._queue[event.resource_id] = []
                # A job they are already standing in front of still gets done.
                # A job they are merely driving towards does not: release it so
                # the dispatcher can give it to somebody else.
                if state.current_order_id and t < state.arrival_min:
                    abandoned = state.current_order_id
                    self._started.discard(abandoned)
                    state.current_order_id = None
                    state.busy_until_min = 0
                    state.arrival_min = 0
                    state.planned_departure_min = None
                    state.overrun_flagged = False
                    stranded = SimEvent(
                        at_min=t,
                        kind=EventKind.WINDOW_MISSED,
                        resource_id=event.resource_id,
                        work_order_id=abandoned,
                        description=(
                            f"{abandoned} was dropped in transit; "
                            f"{event.resource_id} never arrived"
                        ),
                    )
                    out.append(stranded)
                    self.events.append(stranded)

        for resource in self.scenario.resources:
            out.extend(self._tick_resource(resource, t))

        return out

    def _tick_resource(self, resource: BookableResource, t: int) -> list[SimEvent]:
        state = self.state[resource.resource_id]
        out: list[SimEvent] = []

        # Mid-visit: flag an overrun the moment the plan says it should be over.
        if state.current_order_id:
            if (
                not state.overrun_flagged
                and state.planned_departure_min is not None
                and t == state.planned_departure_min
                and state.busy_until_min > state.planned_departure_min
            ):
                state.overrun_flagged = True
                over = state.busy_until_min - state.planned_departure_min
                event = SimEvent(
                    at_min=t,
                    kind=EventKind.JOB_OVERRUNNING,
                    resource_id=resource.resource_id,
                    work_order_id=state.current_order_id,
                    description=(
                        f"{resource.name} is running ~{over} min over on "
                        f"{state.current_order_id}"
                    ),
                    payload={"overrun_min": over},
                )
                out.append(event)
                self.events.append(event)

            if t >= state.busy_until_min:
                out.append(self._finish_visit(resource, state, t))
            return out

        if not state.available or t < state.free_at_min:
            return out

        while self._queue[resource.resource_id]:
            order_id = self._queue[resource.resource_id][0]
            if order_id in self._settled:
                self._queue[resource.resource_id].pop(0)
                continue
            order = self.all_orders[self._order_index[order_id]]
            travel = self.matrix(state.node, self._order_index[order_id])
            arrival = max(t + travel, order.window_start_min)

            if arrival > order.window_end_min or arrival + order.duration_min > resource.shift_end_min:
                self._queue[resource.resource_id].pop(0)
                self._settled.add(order_id)
                event = SimEvent(
                    at_min=t,
                    kind=EventKind.WINDOW_MISSED,
                    resource_id=resource.resource_id,
                    work_order_id=order_id,
                    description=(
                        f"{resource.name} can no longer reach {order_id} inside its window"
                    ),
                )
                out.append(event)
                self.events.append(event)
                continue

            self._queue[resource.resource_id].pop(0)
            out.append(self._begin_visit(resource, state, order, arrival))
            break

        return out

    def _begin_visit(
        self,
        resource: BookableResource,
        state: ResourceState,
        order: WorkOrder,
        arrival: int,
    ) -> SimEvent:
        d = self.disruption
        estimate = int(round(order.duration_min * resource.duration_factor))

        roll = self.rng.random()
        if roll < d.p_customer_absent:
            outcome = Outcome.ABSENT
            actual = d.absent_visit_min
        elif roll < d.p_customer_absent + d.p_parts_missing:
            outcome = Outcome.NEEDS_PARTS
            actual = max(10, int(estimate * d.parts_visit_min_factor))
        elif roll < d.p_customer_absent + d.p_parts_missing + d.p_overrun:
            outcome = Outcome.COMPLETED
            actual = int(estimate * self.rng.uniform(d.overrun_min_factor, d.overrun_max_factor))
        else:
            outcome = Outcome.COMPLETED
            actual = int(estimate * self.rng.uniform(d.noise_min_factor, d.noise_max_factor))

        state.current_order_id = order.work_order_id
        state.arrival_min = arrival
        state.busy_until_min = arrival + actual
        state.planned_departure_min = (
            self._planned_arrival.get(order.work_order_id, arrival) + estimate
        )
        state.overrun_flagged = False
        state.outcome_pending = outcome
        state.node = self._order_index[order.work_order_id]
        self._started.add(order.work_order_id)

        state.free_at_min = arrival

        event = SimEvent(
            at_min=arrival,
            kind=EventKind.JOB_STARTED,
            resource_id=resource.resource_id,
            work_order_id=order.work_order_id,
            description=f"{resource.name} on site at {order.work_order_id}",
            payload={"estimate_min": estimate},
        )
        self.events.append(event)
        return event

    def _finish_visit(self, resource: BookableResource, state: ResourceState, t: int) -> SimEvent:
        order_id = state.current_order_id or ""
        outcome = state.outcome_pending
        arrival = state.arrival_min

        self.executed.append(
            ExecutedVisit(
                work_order_id=order_id,
                resource_id=resource.resource_id,
                arrived_min=arrival,
                left_min=t,
                outcome=outcome,
                planned_arrival_min=self._planned_arrival.get(order_id),
            )
        )

        if outcome == Outcome.COMPLETED:
            kind = EventKind.JOB_COMPLETED
            description = f"{resource.name} completed {order_id}"
            self._settled.add(order_id)
        elif outcome == Outcome.ABSENT:
            kind = EventKind.CUSTOMER_ABSENT
            description = f"nobody home at {order_id}; {resource.name} leaving"
            self._settled.add(order_id)
        else:
            kind = EventKind.PARTS_MISSING
            description = f"{order_id} needs a part {resource.name} does not carry"
            self._settled.add(order_id)

        state.current_order_id = None
        state.arrival_min = 0
        state.busy_until_min = 0
        state.planned_departure_min = None
        state.overrun_flagged = False
        state.free_at_min = t

        event = SimEvent(
            at_min=t,
            kind=kind,
            resource_id=resource.resource_id,
            work_order_id=order_id,
            description=description,
        )
        self.events.append(event)
        return event
