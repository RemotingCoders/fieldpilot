"""Triage: the bridge from judgement to arithmetic.

This is the piece the whole project rests on. It reads a backlog the way a
dispatcher reads it — contract tier, how long someone has been waiting, whether
they have already been bumped, whether the words "gas" and "smell" appear
together — and turns that reading into one integer per work order.

That integer is `penalty_cost`: what it costs the business to leave this job
undone today. The solver never sees the reasoning. It sees the number, and it
minimises travel plus the penalties of whatever it could not fit.

Two deliberate choices worth defending:

**The backlog is scored in one call, not one call per order.** Triage is
inherently comparative — "who first" only means something relative to everything
else waiting. Scoring in isolation loses that, and costs 26x more.

**Anything the model fails to score falls back to the rules engine.** A missing
score is not an option: an unscored order would get a default penalty and
quietly sink to the bottom of the day. The fallback is counted and reported, so
a degraded run is visible rather than silent.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from fieldpilot.agents import rules_triage
from fieldpilot.domain.models import Account, WorkOrder

DEFAULT_MODEL = os.getenv("FIELDPILOT_MODEL", "gemini-3.5-flash")
APP_NAME = "fieldpilot-triage"

# Anything outside this is a model mistake, not a judgement call, and gets
# replaced rather than trusted.
MIN_PENALTY = 100
MAX_PENALTY = 500_000

# Published Gemini 3.5 Flash rates at the time of writing, used only for a
# rough running total. Verify against the billing console before trusting them.
INPUT_USD_PER_MTOK = 1.50
OUTPUT_USD_PER_MTOK = 9.00


class TriageDecision(BaseModel):
    work_order_id: str
    penalty_cost: int = Field(description="Cost of leaving this job undone today.")
    rationale: str = Field(description="One short sentence a dispatcher would accept.")


class TriageBatch(BaseModel):
    decisions: list[TriageDecision]


INSTRUCTION = """\
You are the triage stage of a field service dispatch system for a residential
and light-commercial HVAC, gas and plumbing company in Buenos Aires.

You will receive the day's backlog of work orders. For each one, decide
`penalty_cost`: how much it costs the business to leave that job undone today.
A constraint solver minimises travel time plus the penalties of the jobs it
cannot fit, so your numbers decide what gets sacrificed when the day is
oversubscribed. It always is.

Use this scale:

  100000+   Immediate danger to people or property. Gas smells, active
            flooding, anything where waiting a day risks harm.
   20000    A property is unusable or a business cannot operate. No heat in
            winter, a clinic without climate control, a bakery with a dead oven.
    5000    Degraded but survivable. Something works badly.
     800    Cosmetic or routine. Annual maintenance, a rattling cover.

Then adjust for the things a distance calculation cannot see:

  - Contract tier. Platinum accounts pay for priority and expect it.
  - Days waiting. A job nobody reached in a week is a complaint forming.
  - Reschedule count. Being bumped twice is the strongest churn signal in this
    industry. Weigh it heavily; it is invisible to every other part of the
    system.
  - Account value, but only as a tiebreaker. A safety issue at a small account
    always outranks a routine visit at a large one.

Never let contract tier outrank a safety severity. A gas smell at a customer
with no contract comes before a thermostat swap for a platinum account.

Write one short, concrete rationale per order — the sentence you would say to a
dispatcher who asked "why is that one first?". Cite the specific signals that
moved the number. Never write generic filler like "high priority".

Read the `notes` field carefully. It is free text a call taker typed, it is
often empty, and when it is not it usually contains the single fact that
decides the day. Nothing else in the record captures it.

Notes cut both ways, and most of the work is telling which:

  - Some describe a situation far worse than the job category implies. An
    elderly tenant with no other heat. Water reaching an electrical panel. A
    customer threatening to end a whole-building contract.
  - Some describe a situation far *less* urgent than the category implies. The
    flat is empty. They borrowed a heater. Another contractor already fixed it.
    These should lower the penalty, sometimes well below the category default.
  - Some are pure logistics — parking, gate codes, ask for Marcela — and mean
    nothing about urgency at all. Do not let the mere presence of a note move
    the number.

Reason about what the situation implies, not about which words appear.

Two rules about how far to move a number:

  - **Logistics never move it.** If a note is about access, parking, keys,
    intercoms, pets, or who to ask for at reception, the penalty must be
    exactly what it would have been with no note at all. Mentioning it in the
    rationale is fine; letting it change the score is not.
  - **Stay inside one order of magnitude.** A situation should rarely move a
    job more than about five times up or down from its category base. If you
    find yourself twenty times above the base, you are describing how alarming
    the sentence sounds rather than how much worse the outcome is.

What matters is the *ordering* you produce, not the absolute size of the
numbers. Two jobs at 40000 and 8000 rank the same as 400000 and 80000, and the
smaller pair leaves room to express everything else on the list.

Return a decision for every single work order you were given. Do not skip any.
"""


@dataclass
class TriageResult:
    """What triage did, including where it fell back."""

    orders: list[WorkOrder]
    scored_by_model: int = 0
    scored_by_rules: int = 0
    model: str = ""
    error: str | None = None
    raw_rationales: dict[str, str] = field(default_factory=dict)

    # What this call actually cost, reported at the point of spending rather
    # than discovered in a billing console a day later.
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def degraded(self) -> bool:
        return self.scored_by_rules > 0

    @property
    def estimated_usd(self) -> float:
        """Rough cost of this call.

        Prices move, so this is an order-of-magnitude figure for keeping an eye
        on a fixed credit pool, not an invoice. Billing reports are the source
        of truth; this exists so the number is visible while you are the one
        spending it.
        """
        return (self.input_tokens * INPUT_USD_PER_MTOK
                + self.output_tokens * OUTPUT_USD_PER_MTOK) / 1_000_000

    def summary_line(self) -> str:
        total = self.scored_by_model + self.scored_by_rules
        note = f"  (fell back on {self.scored_by_rules})" if self.degraded else ""
        if self.error:
            note += f"  error: {self.error[:60]}"
        cost = ""
        if self.input_tokens or self.output_tokens:
            cost = (f"  [{self.input_tokens} in / {self.output_tokens} out tokens, "
                    f"~${self.estimated_usd:.4f}]")
        return f"triage: {self.scored_by_model}/{total} scored by {self.model}{note}{cost}"


def describe_backlog(orders: list[WorkOrder], accounts: dict[str, Account]) -> str:
    """Compact, unambiguous rendering of the backlog for the model.

    Deliberately terse: every extra token here is multiplied by the number of
    orders and paid for on every re-plan.
    """
    rows = []
    for order in orders:
        account = accounts.get(order.account_id)
        rows.append(
            {
                "id": order.work_order_id,
                "problem": order.incident_type_id,
                "severity": order.severity.value,
                "customer": account.name if account else "unknown",
                "sla": account.sla_tier.value if account else "none",
                "annual_value_usd": round(account.annual_value_usd) if account else 0,
                "days_waiting": order.days_waiting,
                "times_rescheduled": order.reschedule_count,
                "duration_min": order.duration_min,
                "notes": order.notes,
            }
        )
    return json.dumps(rows, ensure_ascii=False, indent=None)


def _apply_decisions(
    orders: list[WorkOrder],
    accounts: dict[str, Account],
    decisions: list[TriageDecision],
    model: str,
) -> TriageResult:
    """Write valid decisions onto orders; fall back to rules for the rest."""
    by_id = {d.work_order_id: d for d in decisions}
    result = TriageResult(orders=orders, model=model)

    for order in orders:
        decision = by_id.get(order.work_order_id)
        usable = (
            decision is not None
            and MIN_PENALTY <= decision.penalty_cost <= MAX_PENALTY
            and bool(decision.rationale.strip())
        )
        if usable and decision is not None:
            order.penalty_cost = decision.penalty_cost
            order.triage_rationale = decision.rationale.strip()
            result.scored_by_model += 1
            result.raw_rationales[order.work_order_id] = order.triage_rationale
        else:
            penalty, rationale = rules_triage.penalty_for(
                order, accounts.get(order.account_id)
            )
            order.penalty_cost = penalty
            order.triage_rationale = f"[rules] {rationale}"
            result.scored_by_rules += 1

    return result


async def _run_agent(prompt: str, model: str) -> tuple[TriageBatch, int, int]:
    from google.adk.agents import LlmAgent
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    agent = LlmAgent(
        name="triage",
        model=model,
        description="Scores a field service backlog into solver penalties.",
        instruction=INSTRUCTION,
        output_schema=TriageBatch,
        output_key="triage",
    )

    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    session = await runner.session_service.create_session(
        app_name=APP_NAME, user_id="dispatcher"
    )

    payload = None
    tokens_in = 0
    tokens_out = 0

    async for event in runner.run_async(
        user_id="dispatcher",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
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
        raise RuntimeError("triage agent returned no content")

    return TriageBatch.model_validate_json(payload), tokens_in, tokens_out


def apply(
    orders: list[WorkOrder],
    accounts: dict[str, Account],
    model: str | None = None,
) -> TriageResult:
    """Score the backlog with Gemini, falling back to rules on any failure.

    Never raises. A dispatch system that stops working because a model call
    timed out is worse than one that degrades to a heuristic and says so.
    """
    model = model or DEFAULT_MODEL
    if not orders:
        return TriageResult(orders=orders, model=model)

    prompt = (
        "Score every work order in this backlog.\n\n"
        f"{describe_backlog(orders, accounts)}"
    )

    try:
        batch, tokens_in, tokens_out = asyncio.run(_run_agent(prompt, model))
    except Exception as exc:  # noqa: BLE001 - degrade, never crash the day
        result = _apply_decisions(orders, accounts, [], model)
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    result = _apply_decisions(orders, accounts, batch.decisions, model)
    result.input_tokens = tokens_in
    result.output_tokens = tokens_out
    return result
