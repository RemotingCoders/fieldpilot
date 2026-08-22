"""Scoring a plan.

These are the numbers that go on screen at 3:00 in the demo video. They are
deliberately the numbers a dispatch manager already cares about, not proxies
invented to flatter the agent.
"""

from __future__ import annotations

from pydantic import BaseModel

from fieldpilot.domain.models import Plan, SlaTier, WorkOrder, Account


# What each contract tier is worth when we decide "did we protect the
# relationships that pay the bills".
_TIER_WEIGHT = {
    SlaTier.PLATINUM: 8,
    SlaTier.GOLD: 4,
    SlaTier.SILVER: 2,
    SlaTier.NONE: 1,
}


class PlanMetrics(BaseModel):
    planner: str
    orders_total: int
    orders_served: int
    orders_unserved: int
    coverage_pct: float

    travel_minutes: int
    service_minutes: int

    # Weighted by contract tier: serving one platinum account is worth more
    # than serving one walk-in, and a planner that ignores this looks fine on
    # raw counts while quietly burning the customers that matter.
    weighted_coverage_pct: float

    penalty_incurred: int
    safety_unserved: int
    solve_ms: int

    # Share of the day's genuine urgency that the plan actually delivers.
    # Scored against `true_penalty`, which the scenario authors independently
    # and no triage implementation ever sees. This is the number that says
    # whether a triage method understood the day.
    true_value_pct: float = 0.0

    def summary_line(self) -> str:
        return (
            f"{self.planner:<14} "
            f"served {self.orders_served}/{self.orders_total} "
            f"({self.coverage_pct:.0f}%)  "
            f"weighted {self.weighted_coverage_pct:.0f}%  "
            f"travel {self.travel_minutes}min  "
            f"true value {self.true_value_pct:5.1f}%  "
            f"safety missed {self.safety_unserved}"
        )


def score(
    plan: Plan,
    orders: list[WorkOrder],
    accounts: dict[str, Account] | None = None,
) -> PlanMetrics:
    accounts = accounts or {}
    by_id = {o.work_order_id: o for o in orders}
    unserved = set(plan.unserved_work_order_ids)

    def weight(order: WorkOrder) -> int:
        account = accounts.get(order.account_id)
        tier = account.sla_tier if account else SlaTier.NONE
        return _TIER_WEIGHT[tier]

    total_weight = sum(weight(o) for o in orders) or 1
    served_weight = sum(weight(o) for o in orders if o.work_order_id not in unserved)

    travel = sum(b.travel_min for b in plan.bookings)
    service = sum(b.departure_min - b.arrival_min for b in plan.bookings)
    penalty = sum(by_id[wid].penalty_cost for wid in unserved if wid in by_id)
    safety_missed = sum(
        1 for wid in unserved if wid in by_id and by_id[wid].severity.value == "safety"
    )

    served_count = len(orders) - len(unserved)

    true_total = sum(o.true_penalty for o in orders)
    true_served = sum(o.true_penalty for o in orders if o.work_order_id not in unserved)
    true_pct = 100.0 * true_served / true_total if true_total else 0.0

    return PlanMetrics(
        planner=plan.planner,
        orders_total=len(orders),
        orders_served=served_count,
        orders_unserved=len(unserved),
        coverage_pct=100.0 * served_count / len(orders) if orders else 0.0,
        travel_minutes=travel,
        service_minutes=service,
        weighted_coverage_pct=100.0 * served_weight / total_weight,
        penalty_incurred=penalty,
        safety_unserved=safety_missed,
        solve_ms=plan.solve_ms,
        true_value_pct=true_pct,
    )
