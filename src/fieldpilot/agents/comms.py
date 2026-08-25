"""Telling the customer, without letting the model promise anything.

When a plan changes, somebody is waiting at home for a van that is no longer
coming at eleven. The system knows this before they do, and the difference
between a good field service operation and a bad one is largely whether that
message goes out.

Writing it is a language job — tone, apology, the difference between "we are
running late" and "your technician is finishing an emergency two streets away
and will be with you by 15:40". So a model writes it.

**But a model writing customer messages is a liability, not a feature.** The
failure mode is not bad prose. It is a message that says "we'll be there within
the hour" when nothing in the system supports that, or "there will be no charge
for this visit", or a phone number it invented. Those are commitments made on
the company's behalf by something with no authority to make them, and the
customer is entitled to hold the company to them.

So the split here is the same one the rest of this project uses, applied to
words instead of numbers:

- **The facts are computed, never generated.** A `Notification` carries the new
  arrival time, the reason, and the options — all derived from the plan and the
  simulator. The model receives them and may not introduce others.
- **The draft is checked before it is sent.** Every number in the message must
  appear in the facts. Commitment language is refused outright. A message that
  fails goes out as the deterministic template instead, and the failure is
  counted rather than swallowed.
- **The template always exists.** Not as a fallback bolted on afterwards — it
  is written first, and the model's job is to be better than it. If the model
  is unavailable, over quota, or writing things it should not, the customer
  still gets told.

The template being the floor is what makes it safe to let a model near this at
all. Nothing here can fail into silence.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, Field

DEFAULT_MODEL = os.getenv("FIELDPILOT_MODEL", "gemini-3.5-flash")
APP_NAME = "fieldpilot-comms"

# Rough Gemini Flash pricing, same basis as triage and intake.
INPUT_USD_PER_MTOK = 0.30
OUTPUT_USD_PER_MTOK = 2.50

# A customer message that runs past this stops being read. Field service
# notifications are glanced at on a phone between other things.
MAX_CHARS = 320


class NotificationKind(str, Enum):
    RUNNING_LATE = "running_late"
    RESCHEDULED = "rescheduled"
    NOT_TODAY = "not_today"
    ON_THE_WAY = "on_the_way"
    MISSED_YOU = "missed_you"


# Language that commits the company to something the system cannot guarantee.
# Matched on the drafted message, in both English and Spanish, because the
# model will happily write either.
FORBIDDEN = [
    (r"\bno charge\b|\bfree of charge\b|\bsin cargo\b|\bgratis\b|\bbonificad", "price promise"),
    (r"\bguarantee\b|\bguaranteed\b|\bgarantiza|\bgarantizamos\b", "guarantee"),
    (r"\brefund\b|\breembols|\bdevoluci[oó]n del dinero\b", "refund offer"),
    (r"\bcompensat|\bindemniza|\bdescuento\b|\bdiscount\b", "compensation offer"),
    (r"\bwithin the hour\b|\ben menos de una hora\b", "unbacked time commitment"),
    (r"\bcall me\b|\bwhatsapp\b|\+\d{2,}", "invented contact route"),
]


@dataclass
class Notification:
    """Everything true about one thing the customer needs to be told.

    Every field here is computed from the plan or the simulator. Nothing on
    this object came from a model, which is precisely what makes it usable as
    the yardstick for checking one.
    """

    kind: NotificationKind
    customer_name: str
    work_order_id: str
    technician_name: str = ""
    original_time: str = ""       # "11:00"
    new_time: str = ""            # "15:40"
    reason: str = ""              # plain, already-safe phrasing
    options: list[str] = field(default_factory=list)

    def facts(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "customer": self.customer_name,
            "technician": self.technician_name,
            "originally_booked_for": self.original_time,
            "new_arrival_time": self.new_time,
            "reason": self.reason,
            "options_we_can_offer": "; ".join(self.options),
        }

    def numbers(self) -> set[str]:
        """Every numeric token the message is allowed to contain."""
        found: set[str] = set()
        for value in (self.original_time, self.new_time, *self.options, self.reason):
            found.update(_numbers_in(value))
        return found

    def template(self) -> str:
        """The message that goes out when nothing smarter is available.

        Deliberately plain. It is not trying to be warm; it is trying to be
        true, short, and impossible to get wrong.
        """
        # Not the first word. These accounts are businesses — "Imprenta
        # Salguero", "Geriatrico El Refugio" — and greeting a print shop as
        # "Imprenta" is the kind of small wrongness that tells a customer
        # immediately that a machine wrote to them.
        name = self.customer_name.strip() or "Hello"
        who = f" {self.technician_name}" if self.technician_name else ""

        if self.kind is NotificationKind.ON_THE_WAY:
            body = f"Your technician{who} is on the way and should arrive around {self.new_time}."
        elif self.kind is NotificationKind.RUNNING_LATE:
            body = (
                f"Your visit booked for {self.original_time} is running late. "
                f"We now expect{who} at about {self.new_time}."
            )
        elif self.kind is NotificationKind.RESCHEDULED:
            body = (
                f"We have moved your visit from {self.original_time} to {self.new_time}."
            )
        elif self.kind is NotificationKind.MISSED_YOU:
            body = "Our technician called today and could not reach you."
        else:
            body = (
                f"We are not going to reach you today for the visit booked at "
                f"{self.original_time}."
            )

        if self.reason:
            body += f" {self.reason}"
        if self.options:
            body += " " + " ".join(self.options)
        return f"{name}, {body}".strip()


def _numbers_in(text: str) -> set[str]:
    """Numeric tokens, with times normalised so 15:40 also licences 15 and 40."""
    tokens: set[str] = set()
    for match in re.findall(r"\d+", text or ""):
        tokens.add(match.lstrip("0") or "0")
    return tokens


@dataclass
class DraftedMessage:
    text: str
    source: str                       # "model" or "template"
    violations: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    @property
    def estimated_usd(self) -> float:
        return (self.input_tokens * INPUT_USD_PER_MTOK
                + self.output_tokens * OUTPUT_USD_PER_MTOK) / 1_000_000

    def line(self) -> str:
        flag = f"  REJECTED: {', '.join(self.violations)}" if self.violations else ""
        return f"[{self.source}] {self.text}{flag}"


class _Draft(BaseModel):
    message: str = Field(
        description=(
            "The message to send the customer. One short paragraph, no greeting "
            "line breaks, no signature."
        )
    )


INSTRUCTION = """\
You write short notifications to customers of a Buenos Aires field service
company, on behalf of the dispatch desk.

You will be given a set of facts. Write one message conveying them.

Hard rules, in order of importance:

1. **Use only the facts given.** Do not add a time, a duration, a price, a
   phone number, or a name that is not in the facts. If a fact is empty, the
   message simply does not mention it.
2. **Promise nothing.** You are not authorised to offer a discount, a refund,
   free work, compensation, or any guarantee. Do not imply one. "We are sorry"
   is fine. "We will make this right for you" is not, because you do not know
   what that would mean.
3. **Do not invent a way to reach us.** No phone numbers, no WhatsApp, no
   "call me back". If options are given, offer exactly those.
4. Under 320 characters. Warm, direct, no corporate padding. Address the
   customer by the name given. Many are businesses rather than people; use
   the whole name and do not shorten it to its first word.
5. Write in English.

A late technician is an inconvenience, not a catastrophe. Match that.
"""


def verify(message: str, notification: Notification) -> list[str]:
    """What is wrong with this draft, if anything.

    Two checks, both blunt on purpose. A subtle checker that a model can talk
    its way past is worse than none, because it produces confidence.
    """
    problems: list[str] = []

    if len(message) > MAX_CHARS:
        problems.append(f"too long ({len(message)} chars)")

    for pattern, label in FORBIDDEN:
        if re.search(pattern, message, re.IGNORECASE):
            problems.append(label)

    allowed = notification.numbers()
    invented = sorted(_numbers_in(message) - allowed)
    if invented:
        problems.append(f"numbers not in the facts: {', '.join(invented)}")

    return problems


def draft(notification: Notification, model: str | None = None) -> DraftedMessage:
    """Write the customer message. Never raises, always returns something sendable."""
    try:
        result = asyncio.run(_draft_async(notification, model or DEFAULT_MODEL))
    except Exception as exc:  # noqa: BLE001 - an unsendable message is worse than a plain one
        return DraftedMessage(
            text=notification.template(),
            source="template",
            error=f"{type(exc).__name__}: {exc}",
        )

    if result.violations or not result.text.strip():
        # The draft is discarded rather than patched. A message that had to be
        # repaired is a message nobody has read in its final form.
        return DraftedMessage(
            text=notification.template(),
            source="template",
            violations=result.violations,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
    return result


async def _draft_async(notification: Notification, model: str) -> DraftedMessage:
    from google.adk.agents import LlmAgent
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    agent = LlmAgent(
        name="comms",
        model=model,
        description="Writes customer notifications from computed facts.",
        instruction=INSTRUCTION,
        output_schema=_Draft,
        output_key="draft",
    )
    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    session = await runner.session_service.create_session(
        app_name=APP_NAME, user_id="dispatch"
    )

    facts = "\n".join(f"- {k}: {v}" for k, v in notification.facts().items() if v)
    payload = None
    tokens_in = tokens_out = 0

    async for event in runner.run_async(
        user_id="dispatch",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=facts)]),
    ):
        usage = getattr(event, "usage_metadata", None)
        if usage is not None:
            tokens_in += getattr(usage, "prompt_token_count", 0) or 0
            tokens_out += getattr(usage, "candidates_token_count", 0) or 0
        if event.is_final_response() and event.content and event.content.parts:
            payload = event.content.parts[0].text

    if not payload:
        return DraftedMessage(
            text=notification.template(), source="template",
            input_tokens=tokens_in, output_tokens=tokens_out,
            error="no response",
        )

    import json

    try:
        message = json.loads(payload).get("message", "").strip()
    except (json.JSONDecodeError, AttributeError):
        message = payload.strip()

    return DraftedMessage(
        text=message,
        source="model",
        violations=verify(message, notification),
        input_tokens=tokens_in,
        output_tokens=tokens_out,
    )
