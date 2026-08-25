"""What a person has to look at before the day is over.

Every automated dispatch system ends its day with a residue: things it could
not place, things it placed on a guess, things it decided about that it should
not have decided about alone. The dangerous version of this system is the one
that finishes the day silently.

So this module answers one question — *what did we get wrong or fail to do
today, and which of it needs a human before tomorrow* — and it answers it
deterministically. There is no model here on purpose. An escalation queue that
a model can talk itself out of raising is not a safety net, and the whole point
of the queue is that it is the thing that catches what everything upstream,
including the model, got wrong.

Severity ordering is by consequence, not by tidiness:

- **`blocking`** — somebody could be hurt, or the company is exposed. A reported
  gas smell that nobody reached today is the canonical case. This does not wait
  for morning.
- **`same_day`** — a customer is owed a phone call from a person: told nothing,
  told something wrong, or promised something the system cannot keep.
- **`review`** — the system did something defensible that a person should still
  see, because a pattern of defensible decisions is how a bad one hides.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from fieldpilot.domain.models import Severity, WorkOrder

# How many days a job may sit in the backlog before its continued deferral
# stops being a scheduling outcome and starts being a decision somebody should
# make on purpose.
STALE_AFTER_DAYS = 5

# How many failed attempts before the customer stops being a scheduling problem
# and becomes a phone call.
MAX_FAILED_ATTEMPTS = 2


class Urgency(str, Enum):
    BLOCKING = "blocking"
    SAME_DAY = "same_day"
    REVIEW = "review"

    @property
    def rank(self) -> int:
        return {"blocking": 0, "same_day": 1, "review": 2}[self.value]


@dataclass
class Escalation:
    urgency: Urgency
    what: str
    why: str
    work_order_id: str = ""
    customer: str = ""

    def line(self) -> str:
        tag = self.urgency.value.upper().replace("_", " ")
        ref = f" [{self.work_order_id}]" if self.work_order_id else ""
        return f"{tag:<9}{ref} {self.what} — {self.why}"


def _sorted(items: list[Escalation]) -> list[Escalation]:
    return sorted(items, key=lambda e: (e.urgency.rank, e.work_order_id))


def from_unserved(
    unserved: list[WorkOrder],
    accounts: dict | None = None,
) -> list[Escalation]:
    """What being left undone today actually means, per job.

    An unserved job is not by itself an escalation — a day that fits everything
    is a day that was overstaffed. What matters is *which* jobs, and a safety
    call is categorically different from a maintenance visit that slid a day.
    """
    accounts = accounts or {}
    out: list[Escalation] = []

    for order in unserved:
        name = getattr(accounts.get(order.account_id), "name", "")

        if order.severity is Severity.SAFETY:
            out.append(Escalation(
                urgency=Urgency.BLOCKING,
                what="Safety call not attended today",
                why=(
                    "A reported safety condition was left unserved. This needs a "
                    "person tonight, not a slot tomorrow."
                ),
                work_order_id=order.work_order_id,
                customer=name,
            ))
            continue

        if order.days_waiting >= STALE_AFTER_DAYS:
            out.append(Escalation(
                urgency=Urgency.SAME_DAY,
                what=f"Waiting {order.days_waiting} days and dropped again",
                why=(
                    "Deferring this once is scheduling. Deferring it every day is "
                    "a decision nobody has made deliberately."
                ),
                work_order_id=order.work_order_id,
                customer=name,
            ))
            continue

        if order.reschedule_count >= 2:
            out.append(Escalation(
                urgency=Urgency.SAME_DAY,
                what=f"Rescheduled {order.reschedule_count} times, dropped again",
                why="The customer has rearranged their day for us more than once.",
                work_order_id=order.work_order_id,
                customer=name,
            ))
            continue

        if order.severity is Severity.OUT_OF_SERVICE:
            out.append(Escalation(
                urgency=Urgency.REVIEW,
                what="Out-of-service job not reached",
                why="The customer has no heating or no hot water tonight.",
                work_order_id=order.work_order_id,
                customer=name,
            ))

    return _sorted(out)


def from_visits(visits, accounts: dict | None = None) -> list[Escalation]:
    """Failed attempts, counted per customer rather than per visit.

    One missed visit is a customer who stepped out. The same customer missed
    twice is either a wrong number, a wrong address, or somebody who has given
    up on us, and none of those are fixed by booking a third van.
    """
    accounts = accounts or {}
    attempts: dict[str, int] = {}
    for visit in visits:
        outcome = getattr(visit.outcome, "value", visit.outcome)
        if outcome in {"absent", "needs_parts"}:
            attempts[visit.work_order_id] = attempts.get(visit.work_order_id, 0) + 1

    return _sorted([
        Escalation(
            urgency=Urgency.SAME_DAY,
            what=f"{count} failed attempts on the same job",
            why=(
                "Sending a third van costs another slot and will probably fail "
                "the same way. Somebody should phone."
            ),
            work_order_id=work_order_id,
        )
        for work_order_id, count in attempts.items()
        if count >= MAX_FAILED_ATTEMPTS
    ])


def from_intake(outcome) -> list[Escalation]:
    """Everything intake was not confident enough to dispatch on.

    Intake already decides this; this module's job is to make sure the decision
    reaches a person rather than sitting in a field nobody reads.
    """
    out: list[Escalation] = []
    result = getattr(outcome, "result", None)
    if result is None:
        return [Escalation(
            urgency=Urgency.SAME_DAY,
            what="Intake could not read a request at all",
            why=getattr(outcome, "error", "") or "No structured result was produced.",
        )]

    if getattr(result, "needs_human", False):
        out.append(Escalation(
            urgency=Urgency.SAME_DAY,
            what="Intake escalated this request",
            why=f"Confidence {result.confidence:.2f}. {result.reasoning}",
        ))

    geocode = getattr(outcome, "geocode", None)
    if geocode is not None:
        if not geocode.in_service_area:
            out.append(Escalation(
                urgency=Urgency.BLOCKING,
                what="Address resolved outside the service area",
                why=(
                    "A wrong match looks exactly like a valid coordinate. The only "
                    "sign is that it is hundreds of kilometres away."
                ),
            ))
        elif geocode.vague:
            out.append(Escalation(
                urgency=Urgency.REVIEW,
                what="Address resolved only to a neighbourhood",
                why="A technician sent to a centroid has not been sent anywhere.",
            ))
        elif geocode.source == "offline":
            out.append(Escalation(
                urgency=Urgency.BLOCKING,
                what="No real geocode for this address",
                why=(
                    "The offline stand-in produced a plausible-looking point that "
                    "is not a location. Nothing may be dispatched on it."
                ),
            ))

    return _sorted(out)


def from_comms(drafts) -> list[Escalation]:
    """Messages the model tried to send that were refused.

    Not a customer problem — an operations one. A rejected draft means the
    template went out instead, so the customer is fine. It is recorded because
    a rising rejection rate is the earliest signal that the prompt, the model,
    or the facts being fed to it have drifted.
    """
    return [
        Escalation(
            urgency=Urgency.REVIEW,
            what="Customer message rejected before sending",
            why=(
                f"{', '.join(draft.violations)}. The safe template was sent "
                "instead; nothing wrong reached the customer."
            ),
        )
        for draft in drafts
        if getattr(draft, "violations", None)
    ]


@dataclass
class Queue:
    """Everything a person has to see, in the order they should see it."""

    items: list[Escalation]

    @classmethod
    def build(cls, *groups: list[Escalation]) -> "Queue":
        merged: list[Escalation] = []
        for group in groups:
            merged.extend(group)
        return cls(items=_sorted(merged))

    @property
    def blocking(self) -> list[Escalation]:
        return [e for e in self.items if e.urgency is Urgency.BLOCKING]

    def lines(self) -> list[str]:
        if not self.items:
            return ["nothing needs a person tonight"]
        counts = {u: sum(1 for e in self.items if e.urgency is u) for u in Urgency}
        header = "  ".join(
            f"{u.value.replace('_', ' ')} {counts[u]}" for u in Urgency if counts[u]
        )
        return [header, ""] + [e.line() for e in self.items]
