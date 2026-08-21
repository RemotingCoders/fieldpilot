"""Deterministic triage: the control group.

This is not the product. It exists for two reasons.

First, it keeps the whole system runnable with no API key and no spend, which
is what lets the demo scenario be rehearsed as many times as it takes.

Second, and more importantly, it is the control in the experiment. Comparing
`solver + rules triage` against `solver + agent triage` isolates exactly what
the language model contributes, separately from what the optimiser contributes.
Without this, a good result could be the solver doing all the work while the
model takes the credit.
"""

from __future__ import annotations

from fieldpilot.domain.models import Account, Severity, SlaTier, WorkOrder

_SEVERITY_BASE = {
    Severity.SAFETY: 40_000,
    Severity.OUT_OF_SERVICE: 8_000,
    Severity.DEGRADED: 2_500,
    Severity.COSMETIC: 800,
}

_TIER_MULTIPLIER = {
    SlaTier.PLATINUM: 2.5,
    SlaTier.GOLD: 1.6,
    SlaTier.SILVER: 1.2,
    SlaTier.NONE: 1.0,
}


def penalty_for(order: WorkOrder, account: Account | None) -> tuple[int, str]:
    """Return the cost of leaving this order unserved, plus why.

    The rationale string matters as much as the number: a dispatcher who cannot
    see why the system ranked a job will not trust the ranking.
    """
    base = _SEVERITY_BASE[order.severity]
    reasons = [f"severity={order.severity.value}"]

    tier = account.sla_tier if account else SlaTier.NONE
    multiplier = _TIER_MULTIPLIER[tier]
    if multiplier > 1.0:
        reasons.append(f"{tier.value} SLA")

    # Waiting compounds: a job nobody has reached in a week is a complaint
    # forming, regardless of how minor it looked when it was logged.
    waiting_bonus = 1.0 + min(order.days_waiting, 10) * 0.12
    if order.days_waiting >= 3:
        reasons.append(f"waiting {order.days_waiting}d")

    # Being bumped twice is the strongest churn signal in field service, and
    # it is invisible to anything that only looks at distance and duration.
    reschedule_bonus = 1.0 + order.reschedule_count * 0.75
    if order.reschedule_count:
        reasons.append(f"rescheduled {order.reschedule_count}x")

    value_bonus = 1.0
    if account and account.annual_value_usd > 20_000:
        value_bonus = 1.3
        reasons.append("high-value account")

    penalty = int(base * multiplier * waiting_bonus * reschedule_bonus * value_bonus)
    return penalty, ", ".join(reasons)


def apply(orders: list[WorkOrder], accounts: dict[str, Account]) -> list[WorkOrder]:
    """Write `penalty_cost` and `triage_rationale` onto every order, in place."""
    for order in orders:
        penalty, rationale = penalty_for(order, accounts.get(order.account_id))
        order.penalty_cost = penalty
        order.triage_rationale = rationale
    return orders
