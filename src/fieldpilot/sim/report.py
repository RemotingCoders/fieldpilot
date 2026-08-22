"""Scoring the day that actually happened.

Plan metrics measure an intention. These measure an outcome, and the gap
between the two is the entire argument for having an agent watching the day
instead of a scheduler that runs once at 7am.
"""

from __future__ import annotations

from pydantic import BaseModel

from fieldpilot.domain.models import Severity, SlaTier
from fieldpilot.sim.engine import Outcome, Simulator

_TIER_WEIGHT = {
    SlaTier.PLATINUM: 8,
    SlaTier.GOLD: 4,
    SlaTier.SILVER: 2,
    SlaTier.NONE: 1,
}


class DayReport(BaseModel):
    label: str

    jobs_completed: int
    jobs_failed: int          # nobody home, or the part was not on the van
    windows_missed: int       # the technician could no longer arrive in time
    jobs_never_attempted: int

    weighted_completion_pct: float
    safety_completed: int
    safety_total: int

    total_lateness_min: int
    worst_lateness_min: int
    replans: int
    actionable_events: int

    def summary_line(self) -> str:
        return (
            f"{self.label:<22} "
            f"done {self.jobs_completed:>2}  "
            f"failed {self.jobs_failed:>2}  "
            f"missed-window {self.windows_missed:>2}  "
            f"never-tried {self.jobs_never_attempted:>2}  "
            f"weighted {self.weighted_completion_pct:5.1f}%  "
            f"safety {self.safety_completed}/{self.safety_total}  "
            f"late {self.total_lateness_min:>4}min"
        )


def build(sim: Simulator, label: str) -> DayReport:
    from fieldpilot.sim.events import EventKind

    accounts = sim.accounts
    by_id = {o.work_order_id: o for o in sim.all_orders}

    # Only orders the dispatcher ever knew about count against them: an
    # emergency that arrived at 14:00 was not missed at 08:00.
    considered = [
        o
        for o in sim.scenario.work_orders
    ] + [o for arrives, o, _ in sim._urgent if arrives <= sim.now_min]

    completed = {v.work_order_id for v in sim.executed if v.outcome == Outcome.COMPLETED}
    attempted = {v.work_order_id for v in sim.executed}

    failed = sum(1 for v in sim.executed if v.outcome != Outcome.COMPLETED)
    windows_missed = sum(1 for e in sim.events if e.kind == EventKind.WINDOW_MISSED)
    canceled = {
        e.work_order_id for e in sim.events if e.kind == EventKind.ORDER_CANCELED
    }

    never = [
        o
        for o in considered
        if o.work_order_id not in attempted and o.work_order_id not in canceled
    ]
    never_count = max(0, len(never) - windows_missed)

    def weight(order_id: str) -> int:
        order = by_id.get(order_id)
        if not order:
            return 1
        account = accounts.get(order.account_id)
        return _TIER_WEIGHT[account.sla_tier if account else SlaTier.NONE]

    total_weight = sum(weight(o.work_order_id) for o in considered) or 1
    done_weight = sum(weight(wid) for wid in completed)

    safety_total = sum(1 for o in considered if o.severity == Severity.SAFETY)
    safety_done = sum(
        1 for wid in completed if wid in by_id and by_id[wid].severity == Severity.SAFETY
    )

    lateness = [v.lateness_min for v in sim.executed]

    return DayReport(
        label=label,
        jobs_completed=len(completed),
        jobs_failed=failed,
        windows_missed=windows_missed,
        jobs_never_attempted=never_count,
        weighted_completion_pct=100.0 * done_weight / total_weight,
        safety_completed=safety_done,
        safety_total=safety_total,
        total_lateness_min=sum(lateness),
        worst_lateness_min=max(lateness, default=0),
        replans=sum(1 for e in sim.events if e.kind == EventKind.PLAN_REPLACED),
        actionable_events=sum(1 for e in sim.events if e.actionable),
    )
