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


# --------------------------------------------------------------------------
# Naive text handling
#
# The obvious objection to "a language model reads the notes" is: you could
# have just grepped for keywords. So here is that system, built in good faith
# rather than as a strawman — a decent keyword pass of the kind a competent
# engineer would write in an afternoon.
#
# It is included so the comparison is against the real alternative, not against
# a version of the alternative chosen to lose.
# --------------------------------------------------------------------------

_ESCALATING_TERMS = {
    "gas": 1.8,
    "smell": 1.4,
    "children": 1.6,
    "child": 1.6,
    "elderly": 1.5,
    "89": 1.4,
    "lives alone": 1.5,
    "no other source": 1.5,
    "electrical panel": 2.0,
    "water is": 1.8,
    "flood": 2.0,
    "terminate": 2.0,
    "contract": 1.5,
    "closed to the public": 1.6,
    "lose a day": 1.4,
    "third van": 1.6,
    "called three times": 1.3,
    "distressed": 1.3,
    "at risk": 1.4,
    "trip the breakers": 1.5,
}

_DEESCALATING_TERMS = {
    "no rush": 0.4,
    "can wait": 0.4,
    "next week": 0.5,
    "next month": 0.5,
    "stopped by itself": 0.5,
    "already got it running": 0.5,
    "empty": 0.6,
    "no tenants": 0.6,
    "not in use": 0.5,
    "just be a check": 0.6,
    "eventually": 0.7,
}


def keyword_adjustment(notes: str) -> tuple[float, list[str]]:
    """Scan a note for terms that plainly point up or down.

    Returns a multiplier and the terms that fired, so the reasoning stays
    inspectable. Multiplicative effects are damped: three matching terms should
    not cube the penalty.
    """
    if not notes:
        return 1.0, []

    text = notes.lower()
    hits: list[str] = []
    factor = 1.0

    for term, weight in _ESCALATING_TERMS.items():
        if term in text:
            hits.append(term)
            factor *= weight ** 0.6

    for term, weight in _DEESCALATING_TERMS.items():
        if term in text:
            hits.append(term)
            factor *= weight ** 0.6

    return max(0.15, min(factor, 6.0)), hits


def apply_with_keywords(
    orders: list[WorkOrder], accounts: dict[str, Account]
) -> list[WorkOrder]:
    """Rules, plus a keyword pass over the free-text notes."""
    for order in orders:
        penalty, rationale = penalty_for(order, accounts.get(order.account_id))
        factor, hits = keyword_adjustment(order.notes)
        order.penalty_cost = max(1, int(penalty * factor))
        if hits:
            rationale += f", keywords: {'/'.join(hits[:3])}"
        order.triage_rationale = rationale
    return orders
