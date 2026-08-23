"""Running a whole day with somebody watching it.

Ties the three pieces together: the world happens, the monitor decides whether
what happened matters, and the solver redraws the day when it does.

The loop is deliberately dull. All the judgement lives in the monitor, all the
mathematics lives in the solver, and this file only moves information between
them. That separation is the point — it is what makes each half testable and
what makes the claim "the model decides, the solver executes" checkable rather
than rhetorical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from fieldpilot.agents.monitor import MonitorAction, MonitorDecision, Situation
from fieldpilot.domain.models import Account, WorkOrder
from fieldpilot.planning import solver
from fieldpilot.sim.engine import DAY_END_MIN, Simulator
from fieldpilot.sim.events import EventKind, SimEvent

# Typical minutes consumed per completed visit, service plus travel. Used only
# to estimate remaining capacity, which the monitor needs to tell a tight day
# from a day with slack.
MINUTES_PER_VISIT = 75

# How often the monitor wakes up. Ten simulated minutes is roughly how often a
# dispatcher glances at the board; going finer costs model calls without
# changing any decision, since nothing meaningful happens in ninety seconds.
DEFAULT_TICK_MIN = 10


class Monitor(Protocol):
    name: str

    def decide(self, situation: Situation) -> MonitorDecision: ...


TriageFn = Callable[[list[WorkOrder], dict[str, Account]], object]


@dataclass
class Replan:
    """One moment where the day was redrawn."""

    at_min: int
    reasoning: str
    pending_before: int
    scheduled_after: int
    trigger: str

    def line(self) -> str:
        clock = f"{self.at_min // 60:02d}:{self.at_min % 60:02d}"
        return (f"{clock} REPLAN  {self.scheduled_after}/{self.pending_before} "
                f"pending jobs rescheduled — {self.reasoning}")


@dataclass
class DayLog:
    monitor_name: str
    events: list[SimEvent] = field(default_factory=list)
    decisions: list[tuple[int, MonitorDecision]] = field(default_factory=list)
    replans: list[Replan] = field(default_factory=list)
    absorbed: int = 0
    retriage_calls: int = 0

    @property
    def replan_count(self) -> int:
        return len(self.replans)

    def timeline(self, only_actionable: bool = True) -> list[str]:
        """Events and decisions interleaved, in the order they happened."""
        rows: list[tuple[int, str]] = []
        for event in self.events:
            if only_actionable and not event.actionable:
                continue
            rows.append((event.at_min, "  " + event.line()))
        for replan in self.replans:
            rows.append((replan.at_min, "  " + replan.line()))
        return [text for _, text in sorted(rows, key=lambda r: r[0])]


def run_day(
    sim: Simulator,
    monitor: Monitor,
    tick_min: int = DEFAULT_TICK_MIN,
    time_limit_s: int = 3,
    triage_fn: TriageFn | None = None,
) -> DayLog:
    """Advance the simulated day, letting the monitor intervene.

    The initial plan must already be loaded. This runs from the current minute
    to the end of the day.
    """
    log = DayLog(monitor_name=monitor.name)

    last_replan_min = sim.now_min
    replans = 0
    accumulated_overrun = 0
    triaged: set[str] = {o.work_order_id for o in sim.scenario.work_orders}

    while sim.now_min < DAY_END_MIN:
        target = min(sim.now_min + tick_min, DAY_END_MIN)
        events = sim.advance(target)
        log.events.extend(events)

        for event in events:
            if event.kind == EventKind.JOB_OVERRUNNING:
                accumulated_overrun += int(event.payload.get("overrun_min", 0))

        actionable = [e for e in events if e.actionable]
        if not actionable:
            continue

        pending = sim.snapshot_orders()
        available = sim.snapshot_resources()

        capacity = sum(
            max(0, r.shift_end_min - max(r.shift_start_min, sim.now_min))
            for r in available
        ) // MINUTES_PER_VISIT

        situation = Situation(
            now_min=sim.now_min,
            events=actionable,
            pending_orders=len(pending),
            available_technicians=len(available),
            minutes_left=DAY_END_MIN - sim.now_min,
            minutes_since_replan=sim.now_min - last_replan_min,
            replans_so_far=replans,
            accumulated_overrun_min=accumulated_overrun,
            capacity_jobs_left=capacity,
        )

        decision = monitor.decide(situation)
        log.decisions.append((sim.now_min, decision))

        if decision.action != MonitorAction.REPLAN:
            log.absorbed += 1
            continue

        if not available or not pending:
            # Nothing to redraw. Recorded as absorbed rather than as a re-plan,
            # so the count reflects work actually done.
            log.absorbed += 1
            continue

        # Anything that arrived since the morning has never been scored. Score
        # it before it competes with the rest of the backlog for a technician.
        fresh = [o for o in pending if o.work_order_id not in triaged]
        if fresh and triage_fn is not None:
            triage_fn(pending, sim.accounts)
            log.retriage_calls += 1
        triaged.update(o.work_order_id for o in pending)

        plan = solver.solve(pending, available, time_limit_s=time_limit_s)
        sim.load_plan(plan)

        log.replans.append(
            Replan(
                at_min=sim.now_min,
                reasoning=decision.reasoning,
                pending_before=len(pending),
                scheduled_after=len(plan.bookings),
                trigger=actionable[0].kind.value,
            )
        )
        last_replan_min = sim.now_min
        replans += 1
        accumulated_overrun = 0

    return log
