"""The disruption monitor: deciding whether a broken plan is worth fixing.

This is the part that makes the system an agent rather than a scheduler. It
runs through the day, watches what actually happens, and answers one question
each time something goes wrong:

    is this worth re-planning for, or should the crew absorb it?

Both answers are expensive in different currencies. Re-planning too eagerly
churns routes under technicians who are already driving, destroys the customer
promises made that morning, and burns solver time and tokens. Absorbing too
much lets a plan rot until the last three jobs of the day quietly fall off the
end and nobody notices until the complaints arrive.

Getting that trade-off right is judgement, not arithmetic. The re-plan itself,
once the decision is made, is pure OR-Tools.

As with triage, there is a deterministic version alongside the model. It is the
control, the fallback when the API is unreachable, and the thing the model has
to actually beat.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, Field

from fieldpilot.sim.events import EventKind, SimEvent

DEFAULT_MODEL = os.getenv("FIELDPILOT_MODEL", "gemini-3.5-flash")
APP_NAME = "fieldpilot-monitor"

INPUT_USD_PER_MTOK = 1.50
OUTPUT_USD_PER_MTOK = 9.00


class MonitorAction(str, Enum):
    ABSORB = "absorb"
    REPLAN = "replan"


class MonitorDecision(BaseModel):
    action: MonitorAction
    reasoning: str = Field(
        description="One sentence a dispatcher would accept as a reason."
    )


@dataclass
class Situation:
    """Everything the monitor knows at the moment it has to decide."""

    now_min: int
    events: list[SimEvent]
    pending_orders: int
    available_technicians: int
    minutes_left: int
    minutes_since_replan: int
    replans_so_far: int
    accumulated_overrun_min: int

    # Roughly how many more visits the remaining crew can physically fit before
    # the shifts end. Compared against pending work, this is what separates a
    # day with slack from a day where something has to be sacrificed — and that
    # distinction decides whether re-planning can help at all.
    capacity_jobs_left: int = 0

    def clock(self) -> str:
        return f"{self.now_min // 60:02d}:{self.now_min % 60:02d}"

    def as_json(self) -> str:
        return json.dumps(
            {
                "time_now": self.clock(),
                "minutes_left_in_day": self.minutes_left,
                "pending_jobs": self.pending_orders,
                "available_technicians": self.available_technicians,
                "minutes_since_last_replan": self.minutes_since_replan,
                "replans_so_far_today": self.replans_so_far,
                "accumulated_overrun_min": self.accumulated_overrun_min,
                "jobs_the_crew_can_still_fit": self.capacity_jobs_left,
                "new_events": [
                    {"kind": e.kind.value, "what": e.description} for e in self.events
                ],
            },
            ensure_ascii=False,
        )


# --------------------------------------------------------------------------
# The deterministic monitor
# --------------------------------------------------------------------------

# Re-planning less often than this leaves the crew working a stale plan; more
# often than this and they are being redirected while still driving.
MIN_MINUTES_BETWEEN_REPLANS = 30

# Below this much of the day left, a re-plan cannot pay for itself.
MIN_MINUTES_LEFT = 45

# Delay that has to pile up before it is worth redrawing the day for it alone.
OVERRUN_THRESHOLD_MIN = 45

# Above this ratio of spare capacity to pending work, re-planning is churn: if
# everything still fits, re-ordering it only moves promises around.
SLACK_RATIO = 1.15

# Events that change what is possible, rather than merely when.
STRUCTURAL = {
    EventKind.URGENT_ORDER_ARRIVED,
    EventKind.RESOURCE_UNAVAILABLE,
    EventKind.ORDER_CANCELED,
}


class RulesMonitor:
    """Re-plan on structural change or accumulated delay, with a cooldown.

    A good-faith heuristic, not a strawman: it captures the three things a
    competent dispatcher actually watches, and it refuses to thrash.
    """

    name = "rules"

    def decide(self, situation: Situation) -> MonitorDecision:
        if situation.minutes_left < MIN_MINUTES_LEFT:
            return MonitorDecision(
                action=MonitorAction.ABSORB,
                reasoning="too little of the day left for a re-plan to pay off",
            )

        if situation.minutes_since_replan < MIN_MINUTES_BETWEEN_REPLANS:
            return MonitorDecision(
                action=MonitorAction.ABSORB,
                reasoning="re-planned recently; redirecting the crew again would thrash",
            )

        # With comfortable slack the morning plan is already close to right,
        # and re-planning from scattered mid-day positions tends to lose more
        # than it gains. Measured: at 26 orders re-planning cost 4.5 points of
        # true value; at 48 it gained 10.5.
        if situation.capacity_jobs_left > situation.pending_orders * SLACK_RATIO:
            return MonitorDecision(
                action=MonitorAction.ABSORB,
                reasoning="the crew can still fit everything pending; nothing to gain",
            )

        structural = [e for e in situation.events if e.kind in STRUCTURAL]
        if structural:
            return MonitorDecision(
                action=MonitorAction.REPLAN,
                reasoning=f"structural change: {structural[0].kind.value}",
            )

        if situation.accumulated_overrun_min >= OVERRUN_THRESHOLD_MIN:
            return MonitorDecision(
                action=MonitorAction.REPLAN,
                reasoning=(
                    f"{situation.accumulated_overrun_min} min of delay has piled up"
                ),
            )

        return MonitorDecision(
            action=MonitorAction.ABSORB,
            reasoning="delay is within what the day can absorb",
        )


# --------------------------------------------------------------------------
# The model monitor
# --------------------------------------------------------------------------

INSTRUCTION = """\
You are the dispatch supervisor for a field service crew in Buenos Aires. A
plan was built this morning. The day is now happening to it.

Each time something goes wrong you get the current state and the events since
you last looked. Decide one thing: **replan** or **absorb**.

Re-planning is not free:

  - Technicians already driving get redirected, which they hate and which
    wastes the travel already spent.
  - Every customer promised a window this morning may get a different one.
  - Doing it repeatedly makes the whole crew distrust the system.

Absorbing is not free either. Delay compounds down a route. A job that slips
past its window is lost for the day, and the customer finds out by waiting.

Re-plan when what is *possible* has changed:

  - An emergency arrived that nobody planned for.
  - A technician is gone and their remaining route is orphaned.
  - A cancellation opened a slot big enough to fit something real.
  - Delay has accumulated to the point where later jobs will miss their
    windows whatever happens next.

Absorb when only the *timing* has moved and the route still holds — a job
running fifteen minutes over on a light afternoon is not a reason to redraw
four people's days.

Three things to weigh that are easy to miss:

  - **Whether anything has to be sacrificed at all.** Compare pending jobs
    against how many the crew can still fit. If everything fits comfortably,
    re-planning cannot improve the outcome — it only reshuffles promises and
    annoys people. Absorb. This is measured, not theoretical: on a day with
    slack, re-planning made the outcome *worse*.
  - **How much day is left.** At 16:40 there is almost nothing a new plan can
    recover, and the disruption is pure cost.
  - **How recently you re-planned.** If you did it twenty minutes ago, the
    crew is still absorbing that one. Raise your bar.

Give one sentence of reasoning: the sentence you would say to the crew over
the radio to explain why they are getting a new route, or why they are not.
"""


@dataclass
class MonitorStats:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: int = 0
    fell_back: int = 0

    @property
    def estimated_usd(self) -> float:
        return (self.input_tokens * INPUT_USD_PER_MTOK
                + self.output_tokens * OUTPUT_USD_PER_MTOK) / 1_000_000

    def summary_line(self) -> str:
        line = (f"monitor: {self.calls} model calls, "
                f"{self.input_tokens} in / {self.output_tokens} out tokens, "
                f"~${self.estimated_usd:.4f}")
        if self.fell_back:
            line += f"  (fell back to rules {self.fell_back}x)"
        return line


class GeminiMonitor:
    """Asks the model whether the disruption is worth redrawing the day for.

    Falls back to the rules monitor on any failure. A dispatch system that
    freezes because an API call timed out is worse than one that quietly
    reverts to a heuristic and records that it did.
    """

    name = "gemini"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or DEFAULT_MODEL
        self.stats = MonitorStats()
        self._fallback = RulesMonitor()
        self.decisions: list[tuple[int, MonitorDecision]] = []

    def decide(self, situation: Situation) -> MonitorDecision:
        try:
            decision, tokens_in, tokens_out = asyncio.run(
                self._ask(situation)
            )
            self.stats.calls += 1
            self.stats.input_tokens += tokens_in
            self.stats.output_tokens += tokens_out
        except Exception:  # noqa: BLE001 - degrade, never stall the day
            self.stats.errors += 1
            self.stats.fell_back += 1
            decision = self._fallback.decide(situation)
            decision.reasoning = f"[rules fallback] {decision.reasoning}"

        self.decisions.append((situation.now_min, decision))
        return decision

    async def _ask(self, situation: Situation) -> tuple[MonitorDecision, int, int]:
        from google.adk.agents import LlmAgent
        from google.adk.runners import InMemoryRunner
        from google.genai import types

        agent = LlmAgent(
            name="disruption_monitor",
            model=self.model,
            description="Decides whether a disrupted day is worth re-planning.",
            instruction=INSTRUCTION,
            output_schema=MonitorDecision,
            output_key="decision",
        )

        runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
        session = await runner.session_service.create_session(
            app_name=APP_NAME, user_id="supervisor"
        )

        payload = None
        tokens_in = tokens_out = 0

        async for event in runner.run_async(
            user_id="supervisor",
            session_id=session.id,
            new_message=types.Content(
                role="user", parts=[types.Part(text=situation.as_json())]
            ),
        ):
            usage = getattr(event, "usage_metadata", None)
            if usage:
                tokens_in += getattr(usage, "prompt_token_count", 0) or 0
                tokens_out += getattr(usage, "candidates_token_count", 0) or 0
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        payload = part.text

        if not payload:
            raise RuntimeError("monitor returned no content")

        return MonitorDecision.model_validate_json(payload), tokens_in, tokens_out


class NoMonitor:
    """Nobody is watching. The control condition."""

    name = "none"

    def decide(self, situation: Situation) -> MonitorDecision:
        return MonitorDecision(
            action=MonitorAction.ABSORB, reasoning="no monitor running"
        )
