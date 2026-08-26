"""Constraint solver for the daily dispatch plan.

This module contains no language model and no judgement. It takes work orders
whose `penalty_cost` has already been decided, and it produces the arrangement
of visits that minimises travel plus the cost of whatever it could not fit.

Keeping this half deterministic is the point. The agent decides what matters;
this decides how to physically achieve it.
"""

from __future__ import annotations

import time
import uuid

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from fieldpilot.domain.models import (
    Booking,
    BookableResource,
    BookingStatus,
    Location,
    Plan,
    WorkOrder,
)
from fieldpilot.memory.durations import DurationMemory
from fieldpilot.planning.travel import TravelMatrix


def solve(
    orders: list[WorkOrder],
    resources: list[BookableResource],
    matrix: TravelMatrix | None = None,
    time_limit_s: int = 5,
    solution_limit: int | None = None,
    memory: "DurationMemory | None" = None,
) -> Plan:
    """Build the day's routes.

    Orders that no technician is qualified for, or that do not fit anyone's
    day, come back in `unserved_work_order_ids` rather than being silently
    dropped. A dispatcher needs to see them.

    **On reproducibility.** `time_limit_s` is wall clock, so the same inputs on
    a loaded machine explore fewer nodes and can return a different plan. That
    is not a theoretical concern: it made a determinism test in this repository
    flaky, passing alone and failing inside the full suite. Anything that has to
    reproduce — a test, a recorded demo, a published number — should pass
    `solution_limit` instead, which counts improving solutions and does not care
    how fast the machine is. The time limit stays as a ceiling; if it is the one
    that binds, reproducibility is lost again, so keep it generous.
    """
    started = time.monotonic()

    if not orders or not resources:
        return Plan(
            unserved_work_order_ids=[o.work_order_id for o in orders],
            planner="ortools",
            solve_ms=0,
        )

    # Orders nobody is qualified for never enter the model. Feeding the solver
    # a node with an empty allowed-vehicle list makes it report infeasible
    # instead of simply leaving the node out.
    eligible: dict[str, list[int]] = {}
    schedulable: list[WorkOrder] = []
    impossible: list[WorkOrder] = []
    for order in orders:
        vehicles = [i for i, r in enumerate(resources) if r.can_serve(order)]
        if vehicles:
            eligible[order.work_order_id] = vehicles
            schedulable.append(order)
        else:
            impossible.append(order)

    if not schedulable:
        return Plan(
            unserved_work_order_ids=[o.work_order_id for o in impossible],
            planner="ortools",
            solve_ms=int((time.monotonic() - started) * 1000),
        )

    n_orders = len(schedulable)
    n_vehicles = len(resources)

    # Node layout: work orders, then one start node per technician, then a
    # single shared end node that costs nothing to reach. Technicians finish
    # wherever their last job is rather than driving back to a depot.
    locations: list[Location] = [o.location for o in schedulable]
    locations += [r.start_location for r in resources]
    end_node = n_orders + n_vehicles
    locations.append(locations[0])  # placeholder, never used in a real leg

    if matrix is None:
        matrix = TravelMatrix.estimated(locations)

    starts = [n_orders + v for v in range(n_vehicles)]
    ends = [end_node] * n_vehicles

    manager = pywrapcp.RoutingIndexManager(len(locations), n_vehicles, starts, ends)
    routing = pywrapcp.RoutingModel(manager)

    base_service = [o.duration_min for o in schedulable]

    def service_factor(resource_id: str, incident_type_id: str) -> float:
        """The planner's belief about how long this person takes on this work.

        1.0 — the estimate as written — until memory has earned otherwise.
        """
        if memory is None:
            return 1.0
        return memory.factor(resource_id, incident_type_id)

    def travel_only(from_index: int, to_index: int) -> int:
        to_node = manager.IndexToNode(to_index)
        if to_node == end_node:
            return 0
        from_node = manager.IndexToNode(from_index)
        return matrix(from_node, to_node)

    cost_cb = routing.RegisterTransitCallback(travel_only)
    routing.SetArcCostEvaluatorOfAllVehicles(cost_cb)

    # Service time is charged per technician, so a resource the duration memory has
    # learned is slower carries that into the schedule instead of the plan
    # assuming everyone works at the same pace.
    #
    # Where that factor comes from is the important part. It used to be read
    # straight off `resource.duration_factor`, which is the simulator's ground
    # truth for how fast each person works — a number no real dispatcher has.
    # The planner was being handed the answer. Now it gets 1.0 until a memory
    # bank has watched enough completed visits to have earned an opinion.
    transit_cbs: list[int] = []
    for v, resource in enumerate(resources):
        def transit(
            from_index: int,
            to_index: int,
            _rid=resource.resource_id,
        ) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            service = 0
            if from_node < n_orders:
                factor = service_factor(_rid, schedulable[from_node].incident_type_id)
                service = int(round(base_service[from_node] * factor))
            leg = 0 if to_node == end_node else matrix(from_node, to_node)
            return service + leg

        transit_cbs.append(routing.RegisterTransitCallback(transit))

    horizon = max(r.shift_end_min for r in resources)
    routing.AddDimensionWithVehicleTransits(
        transit_cbs,
        horizon,      # waiting is allowed: arriving early at a time window is fine
        horizon,      # nothing may run past the latest shift end
        False,        # shifts do not all start at zero
        "Time",
    )
    time_dim = routing.GetDimensionOrDie("Time")

    for node, order in enumerate(schedulable):
        index = manager.NodeToIndex(node)
        time_dim.CumulVar(index).SetRange(order.window_start_min, order.window_end_min)
        # Restrict this visit to technicians holding the required certifications.
        # -1 is the "not performed" value and must stay in the domain, otherwise
        # the order becomes mandatory and an oversubscribed day goes infeasible.
        routing.VehicleVar(index).SetValues([-1, *eligible[order.work_order_id]])
        # Leaving this order unserved costs what triage said it costs.
        routing.AddDisjunction([index], order.penalty_cost)

    for v, resource in enumerate(resources):
        start = routing.Start(v)
        end = routing.End(v)
        time_dim.CumulVar(start).SetRange(resource.shift_start_min, resource.shift_end_min)
        time_dim.CumulVar(end).SetRange(resource.shift_start_min, resource.shift_end_min)
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(start))
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(end))

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.FromSeconds(time_limit_s)
    if solution_limit is not None:
        params.solution_limit = solution_limit

    solution = routing.SolveWithParameters(params)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    # If the search used essentially all of its wall clock, the clock is what
    # stopped it, and the result depends on how fast this machine happened to
    # be. Only a search that stopped early — because the solution limit was
    # reached — can be promised to repeat.
    reproducible = (
        solution_limit is not None and elapsed_ms < time_limit_s * 950
    )

    if solution is None:
        return Plan(
            unserved_work_order_ids=[o.work_order_id for o in orders],
            planner="ortools",
            solve_ms=elapsed_ms,
            reproducible=reproducible,
        )

    bookings: list[Booking] = []
    served: set[str] = set()

    for v, resource in enumerate(resources):
        index = routing.Start(v)
        prev_node = manager.IndexToNode(index)
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node < n_orders:
                order = schedulable[node]
                arrival = solution.Min(time_dim.CumulVar(index))
                service = int(round(
                    order.duration_min
                    * service_factor(resource.resource_id, order.incident_type_id)
                ))
                bookings.append(
                    Booking(
                        booking_id=f"bkg-{uuid.uuid4().hex[:8]}",
                        work_order_id=order.work_order_id,
                        resource_id=resource.resource_id,
                        arrival_min=arrival,
                        departure_min=arrival + service,
                        travel_min=matrix(prev_node, node),
                        status=BookingStatus.SCHEDULED,
                    )
                )
                served.add(order.work_order_id)
                prev_node = node
            index = solution.Value(routing.NextVar(index))

    unserved = [o.work_order_id for o in orders if o.work_order_id not in served]

    return Plan(
        bookings=bookings,
        unserved_work_order_ids=unserved,
        planner="ortools",
        solve_ms=elapsed_ms,
        reproducible=reproducible,
    )
