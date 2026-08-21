"""The dispatcher we are trying to beat.

First-in-first-out, assigned to whichever qualified technician can arrive
soonest. This is not a strawman: it is what a spreadsheet and a phone produce,
and it is what most small field service operations actually run on.

The video shows the same simulated day twice, once through this and once
through the agent. Without this half, the improvement is a claim rather than a
measurement.
"""

from __future__ import annotations

import time
import uuid

from fieldpilot.domain.models import (
    Booking,
    BookableResource,
    BookingStatus,
    Location,
    Plan,
    WorkOrder,
)
from fieldpilot.planning.travel import TravelMatrix


def dispatch(
    orders: list[WorkOrder],
    resources: list[BookableResource],
    matrix: TravelMatrix | None = None,
) -> Plan:
    """Greedy earliest-arrival assignment in arrival order of the request."""
    started = time.monotonic()

    if not orders or not resources:
        return Plan(
            unserved_work_order_ids=[o.work_order_id for o in orders],
            planner="fifo-nearest",
        )

    locations: list[Location] = [o.location for o in orders]
    locations += [r.start_location for r in resources]
    if matrix is None:
        matrix = TravelMatrix.estimated(locations)

    n_orders = len(orders)

    # Where each technician is, and when they are free.
    at_node = {i: n_orders + i for i in range(len(resources))}
    free_at = {i: r.shift_start_min for i, r in enumerate(resources)}

    # Longest-waiting first, which is how a human dispatcher works a backlog.
    queue = sorted(
        range(n_orders),
        key=lambda i: (-orders[i].days_waiting, orders[i].window_start_min),
    )

    bookings: list[Booking] = []
    served: set[str] = set()

    for order_idx in queue:
        order = orders[order_idx]
        best: tuple[int, int, int] | None = None  # (arrival, vehicle, travel)

        for v, resource in enumerate(resources):
            if not resource.can_serve(order):
                continue
            travel = matrix(at_node[v], order_idx)
            arrival = max(free_at[v] + travel, order.window_start_min)
            if arrival > order.window_end_min:
                continue
            service = int(round(order.duration_min * resource.duration_factor))
            if arrival + service > resource.shift_end_min:
                continue
            if best is None or arrival < best[0]:
                best = (arrival, v, travel)

        if best is None:
            continue

        arrival, v, travel = best
        resource = resources[v]
        service = int(round(order.duration_min * resource.duration_factor))
        bookings.append(
            Booking(
                booking_id=f"bkg-{uuid.uuid4().hex[:8]}",
                work_order_id=order.work_order_id,
                resource_id=resource.resource_id,
                arrival_min=arrival,
                departure_min=arrival + service,
                travel_min=travel,
                status=BookingStatus.SCHEDULED,
            )
        )
        served.add(order.work_order_id)
        at_node[v] = order_idx
        free_at[v] = arrival + service

    return Plan(
        bookings=bookings,
        unserved_work_order_ids=[o.work_order_id for o in orders if o.work_order_id not in served],
        planner="fifo-nearest",
        solve_ms=int((time.monotonic() - started) * 1000),
    )
