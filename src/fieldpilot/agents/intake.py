"""Intake: turning what the customer actually sent into a work order.

Everything downstream of here assumes a tidy record — an incident type, a
duration, the certifications the job needs, a time window. Nothing that arrives
from a customer looks like that. What arrives is a voice note at 07:40, a photo
taken in a dark basement, and a sentence like "it's making that noise again and
my mother-in-law is here until Thursday".

This is the part of the system where the case for a language model is easiest
to make and hardest to fake. A rules engine cannot listen to audio. The
interesting question is not whether a model can transcribe — it is whether the
photo and the voice change the *classification*, or whether they are decoration
on a decision the text alone already made. There is an ablation for exactly
that, because the honest answer might be no.

Two design choices worth defending:

**The customer's own words are preserved verbatim** alongside the structured
fields. A dispatcher overruling the system needs to see what was actually said,
not a summary of it, and a summary is where quiet mistranslations hide.

**Ambiguity escalates instead of guessing.** A model asked to produce a schema
will always produce one. `needs_human` exists so that "I cannot tell what this
is" is an available answer rather than a confident wrong one.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

from pydantic import BaseModel, Field

from fieldpilot.domain.models import Severity, WorkOrder
from fieldpilot.sim.scenario import INCIDENT_TYPES

DEFAULT_MODEL = os.getenv("FIELDPILOT_MODEL", "gemini-3.5-flash")
APP_NAME = "fieldpilot-intake"

INPUT_USD_PER_MTOK = 1.50
OUTPUT_USD_PER_MTOK = 9.00

KNOWN_INCIDENTS = [t.incident_type_id for t in INCIDENT_TYPES]
INCIDENT_BY_ID = {t.incident_type_id: t for t in INCIDENT_TYPES}
KNOWN_CHARACTERISTICS = ["gas", "hvac", "elec", "plumb"]


class IntakeResult(BaseModel):
    """A work order as far as intake can determine it."""

    incident_type_id: str = Field(
        description=f"One of: {', '.join(KNOWN_INCIDENTS)}, or 'unknown'."
    )
    severity: Severity
    required_characteristics: list[str] = Field(
        description=f"Subset of: {', '.join(KNOWN_CHARACTERISTICS)}"
    )
    estimated_duration_min: int

    # Minutes from midnight. 0 and 1050 mean "any time in the working day".
    window_start_min: int = 8 * 60
    window_end_min: int = 17 * 60 + 30

    address: str = Field(
        default="",
        description="The service address exactly as the customer gave it, "
                    "including street number, floor and flat. Empty if none "
                    "was mentioned.",
    )

    customer_words: str = Field(
        description="What the customer actually said, transcribed verbatim, in "
                    "their own language. Never translated, never summarised."
    )
    notes: str = Field(
        description="The situation in one line for the dispatcher, in English."
    )

    confidence: float = Field(description="0 to 1, on the classification.")
    needs_human: bool = Field(
        description="True when the input is too ambiguous to dispatch on."
    )
    reasoning: str = Field(description="Why this classification, in one sentence.")

    def settled_severity(self) -> tuple[Severity, str]:
        """The severity actually dispatched on, and why it differs if it does.

        A caution about the evidence, because it caught me out. Five repeats of
        one request showed severity changing on four of four comparisons, which
        looked decisive. Ten repeats of the same request showed one of nine.
        **The severity instability did not replicate**, and the first number
        was small-sample noise being read as a finding — the same error the
        repeated ablation exists to prevent, made while building it.

        This rule stays anyway, on the design argument rather than that
        measurement: severity sets `penalty_cost`, which is what decides which
        jobs get dropped, and an incident type exists precisely so that its
        consequences do not have to be re-derived per call. The instability
        that *did* replicate is on skills; see `dispatch_characteristics`.

        The model can still raise severity, because a customer can describe
        something worse than the category implies — a routine service call that
        mentions a burning smell is a safety call. It cannot lower it: that is
        the direction where an unstable output quietly drops real work.
        """
        incident = INCIDENT_BY_ID.get(self.incident_type_id)
        if incident is None:
            return self.severity, ""

        default = incident.default_severity
        if self.severity.rank > default.rank:
            return self.severity, (
                f"intake raised severity {default.value} -> {self.severity.value}"
            )
        if self.severity.rank < default.rank:
            return default, (
                f"intake said {self.severity.value}; held at the {default.value} "
                f"default for {self.incident_type_id}"
            )
        return default, ""

    def dispatch_characteristics(self) -> tuple[list[str], str]:
        """The certifications actually required, and what the model wanted.

        Same reason as severity, and better supported by the evidence: across
        two repeated ablations the model would not settle on whether a split
        that will not heat needs `hvac` or `hvac`+`elec`. That is not a
        cosmetic wobble. Three of the four technicians hold `hvac`; one holds
        `hvac`+`elec`. A coin flip on this field is a coin flip between a job
        three people can take and a job only Bruno can take.

        In Field Service the incident type is what carries required
        characteristics — that is the whole point of having a type. So the type
        decides, and the model's own list is recorded rather than obeyed. If a
        photograph reveals something that genuinely changes the trade, the
        honest way to say so is a different incident type or a raised severity,
        both of which this result can still express.
        """
        incident = INCIDENT_BY_ID.get(self.incident_type_id)
        if incident is None:
            return list(self.required_characteristics), ""

        required = list(incident.required_characteristics)
        if set(required) != set(self.required_characteristics):
            wanted = "/".join(sorted(self.required_characteristics)) or "none"
            return required, (
                f"intake proposed skills {wanted}; "
                f"{self.incident_type_id} requires {'/'.join(required)}"
            )
        return required, ""

    def to_work_order(self, work_order_id: str, account_id: str, location) -> WorkOrder:
        severity, note = self.settled_severity()
        characteristics, skills_note = self.dispatch_characteristics()
        note = "; ".join(n for n in (note, skills_note) if n)
        notes = f"{self.notes} [{note}]".strip() if note else self.notes
        return WorkOrder(
            work_order_id=work_order_id,
            account_id=account_id,
            incident_type_id=self.incident_type_id,
            location=location,
            window_start_min=self.window_start_min,
            window_end_min=self.window_end_min,
            duration_min=max(15, self.estimated_duration_min),
            required_characteristics=characteristics,
            severity=severity,
            notes=notes,
        )


INSTRUCTION = f"""\
You are the intake desk of a field service company in Buenos Aires that covers
residential and light-commercial HVAC, gas and plumbing.

Requests arrive however the customer felt like sending them: a typed message, a
voice note, a photograph of the equipment, or several at once. Turn what you
receive into one work order.

Classify the problem as one of these, or `unknown` if you genuinely cannot
tell: {', '.join(KNOWN_INCIDENTS)}.

The same wall split fails in two directions and they are different jobs:
`ac-not-cooling` is a unit that will not cool, `split-no-heat` is one that will
not heat. Take the direction from what the customer actually says, never from
the time of year. A gas boiler that produces no heat is `boiler-no-heat`, which
is a different machine.

Certifications a job may need: {', '.join(KNOWN_CHARACTERISTICS)}. Gas work and
anything involving a gas smell requires `gas`. Boilers usually need both `gas`
and `hvac`. Water leaks need `plumb`.

Severity:
  safety          — danger to people or property: gas, fire risk, water near
                    electrics, flooding
  out_of_service  — the property or business cannot function
  degraded        — works badly
  cosmetic        — routine, appearance, maintenance

**Use everything you are given, and let it change your answer.**

  - A photograph carries information the text does not. Corrosion, the size of
    a puddle, an old model number, a scorch mark, water near a distribution
    board — these change severity, duration and sometimes the trade required.
    If the picture contradicts the words, say so in your reasoning and trust
    what you can see.
  - A voice note carries urgency the transcript loses, and often mentions
    constraints in passing: who is home, when they leave, whether a child or an
    elderly person is affected.
  - Customers describe symptoms, not diagnoses. "It smells funny in the
    kitchen" near a gas appliance is a gas call.

**Time windows.** Convert what they say into minutes from midnight. "After
three" is 900 to 1050. "Mornings only" is 480 to 720. "I'm home all day" is 480
to 1050. Do not invent a narrow window from a vague statement — a wrong window
loses the visit entirely.

Customers often offer two separate windows: "until two when I'm here, or after
four when my wife is". The scheduler currently accepts only one window per
order, which is a real limitation of the data model rather than of you. Take
the widest span that is genuinely workable, and record the alternatives in
`notes` so a dispatcher can use them. Never silently discard the second window
— for a customer who is only home in two narrow slots, that is the difference
between a completed visit and a wasted trip.

**Duration.** Estimate honestly from the problem and anything visible. An
installation is not a repair. If the photo shows the unit is in a crawlspace or
behind a wall of boxes, that is real extra time.

**address** is the service address as the customer gave it, with the street
number, floor and flat if they said them. Do not guess, complete or correct it.
A geocoder runs on this string afterwards, and an invented street number sends
a van to a real building that is not the right one. Leave it empty if they
never said where.

**customer_words** must be what they actually said, transcribed as spoken and
in the language they used. Do not translate it, tidy it, or shorten it. The
dispatcher who overrules you needs the original.

**needs_human** is true when you cannot responsibly dispatch on what you have.
Check each of these separately, and escalate if *any* of them holds:

  - You cannot tell what the problem is.
  - You cannot tell which trade it needs.
  - **There is no address, or no way to identify which property this is.**
  - The audio is inaudible or the photo shows something you do not recognise.
  - The inputs describe what look like different problems.

These are independent. Being certain about the diagnosis does not resolve a
missing address — knowing exactly what is broken tells you nothing about where
to send the van. Do not let confidence in one dimension suppress an escalation
raised by another.

An honest escalation costs one phone call. A confident wrong classification
sends an uncertified technician to a gas leak.

Be specific in `reasoning`, and say what each input contributed. If the photo
changed your mind, that sentence is the most useful thing you will write.
"""


@dataclass
class IntakeOutcome:
    result: IntakeResult | None
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    used_image: bool = False
    used_audio: bool = False
    geocode: object | None = None   # GeocodeResult, when an address was given

    @property
    def estimated_usd(self) -> float:
        return (self.input_tokens * INPUT_USD_PER_MTOK
                + self.output_tokens * OUTPUT_USD_PER_MTOK) / 1_000_000

    def summary_line(self) -> str:
        if self.error:
            return f"intake failed: {self.error[:80]}"
        assert self.result is not None
        inputs = ["text"]
        if self.used_image:
            inputs.append("image")
        if self.used_audio:
            inputs.append("audio")
        flag = "  ESCALATED" if self.result.needs_human else ""
        severity, adjusted = self.result.settled_severity()
        shown = severity.value + ("*" if adjusted else "")
        return (
            f"{self.result.incident_type_id:<22} {shown:<15} "
            f"{self.result.estimated_duration_min:>3}min  "
            f"skills={'/'.join(self.result.required_characteristics) or '-':<12} "
            f"conf={self.result.confidence:.2f}  [{'+'.join(inputs)}]"
            f"  ~${self.estimated_usd:.4f}{flag}"
        )


def _part_from_file(path: Path):
    from google.genai import types

    mime, _ = mimetypes.guess_type(str(path))
    if mime is None:
        suffix = path.suffix.lower()
        mime = {
            ".m4a": "audio/mp4",
            ".ogg": "audio/ogg",
            ".opus": "audio/ogg",
            ".webm": "audio/webm",
            ".heic": "image/heic",
        }.get(suffix, "application/octet-stream")
    return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)


async def _run(text: str, image: Path | None, audio: Path | None, model: str):
    from google.adk.agents import LlmAgent
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    agent = LlmAgent(
        name="intake",
        model=model,
        description="Turns raw customer requests into structured work orders.",
        instruction=INSTRUCTION,
        output_schema=IntakeResult,
        output_key="work_order",
    )

    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    session = await runner.session_service.create_session(
        app_name=APP_NAME, user_id="intake-desk"
    )

    parts = []
    if text:
        parts.append(types.Part(text=text))
    if image is not None:
        parts.append(_part_from_file(image))
    if audio is not None:
        parts.append(_part_from_file(audio))
    if not parts:
        raise ValueError("intake needs at least one of text, image or audio")

    payload = None
    tokens_in = tokens_out = 0

    async for event in runner.run_async(
        user_id="intake-desk",
        session_id=session.id,
        new_message=types.Content(role="user", parts=parts),
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
        raise RuntimeError("intake returned no content")

    return IntakeResult.model_validate_json(payload), tokens_in, tokens_out


def receive(
    text: str = "",
    image: str | Path | None = None,
    audio: str | Path | None = None,
    model: str | None = None,
    geocode_address: bool = True,
) -> IntakeOutcome:
    """Classify one incoming request. Never raises."""
    model = model or DEFAULT_MODEL
    image_path = Path(image) if image else None
    audio_path = Path(audio) if audio else None

    for path in (image_path, audio_path):
        if path is not None and not path.exists():
            return IntakeOutcome(result=None, error=f"missing file: {path}")

    try:
        result, tokens_in, tokens_out = asyncio.run(
            _run(text, image_path, audio_path, model)
        )
    except Exception as exc:  # noqa: BLE001 - an intake failure is a phone call
        return IntakeOutcome(result=None, error=f"{type(exc).__name__}: {exc}")

    outcome = IntakeOutcome(
        result=result,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        used_image=image_path is not None,
        used_audio=audio_path is not None,
    )

    # Resolve the address. This is the last link between a customer message and
    # a point the solver can route to, and it is the one that fails silently:
    # a plausible coordinate in the wrong suburb looks exactly like a correct
    # one until a technician is standing outside the wrong building.
    if geocode_address and result.address:
        from fieldpilot.planning import geocode as geocode_mod

        found = geocode_mod.geocode(result.address)
        outcome.geocode = found

        if not found.usable and found.source != "offline":
            result.needs_human = True
            result.reasoning += (
                f" Address could not be resolved to a dispatchable location "
                f"({found.error or 'outside the service area'})."
            )
        elif found.vague:
            result.needs_human = True
            result.reasoning += (
                " The address resolved only to a neighbourhood, not a door; "
                "the street number is probably missing or wrong."
            )

    return outcome


def disagreement(a: IntakeResult, b: IntakeResult) -> list[str]:
    """What changed between two classifications of the same request.

    Used by the ablation: run intake with and without the photo and see whether
    the picture actually moved anything. If this comes back empty every time,
    the image is decoration and the honest thing is to say so.
    """
    changes: list[str] = []
    if a.incident_type_id != b.incident_type_id:
        changes.append(f"type {b.incident_type_id} -> {a.incident_type_id}")
    if a.severity != b.severity:
        changes.append(f"severity {b.severity.value} -> {a.severity.value}")
    if set(a.required_characteristics) != set(b.required_characteristics):
        changes.append(
            f"skills {'/'.join(sorted(b.required_characteristics))} -> "
            f"{'/'.join(sorted(a.required_characteristics))}"
        )
    if abs(a.estimated_duration_min - b.estimated_duration_min) >= 15:
        changes.append(
            f"duration {b.estimated_duration_min} -> {a.estimated_duration_min} min"
        )
    if (a.window_start_min, a.window_end_min) != (b.window_start_min, b.window_end_min):
        changes.append("time window")
    if a.needs_human != b.needs_human:
        changes.append(f"escalation {b.needs_human} -> {a.needs_human}")
    return changes


# --------------------------------------------------------------------------
# Repeated ablation
#
# A single paired run cannot tell "the photo changed the answer" from "the
# model gave a different answer". Both look identical on one trial: two
# outputs that differ. The only way to separate them is to also measure how
# much the model disagrees with *itself* on unchanged inputs, and compare the
# two rates.
#
# This was written after exactly that mistake. Two single-trial ablations on
# the same request gave opposite conclusions — first "the photo changed
# nothing", then "the photo changed three things" — and one of the three was a
# changed time window, which a photograph of an air conditioner cannot
# possibly carry. That is a noise floor announcing itself.
# --------------------------------------------------------------------------

FIELDS = ("type", "severity", "skills", "duration", "window", "escalation")

# Below this many trials no verdict is offered. Five is not a sample: with four
# self-comparisons behind it, a one-run difference reads as a finding.
MIN_TRIALS_FOR_A_VERDICT = 10

# How far the paired rate has to clear the self-disagreement rate before the
# difference is called anything. Deliberately blunt — the alternative is a
# significance test on counts this small, which would dress up the same
# guesswork in better notation.
MARGIN = 0.40


def changed_dispatch_fields(a: IntakeResult, b: IntakeResult) -> set[str]:
    """The same comparison, made after the taxonomy has had its say.

    This is the number that matters operationally. Nobody dispatches on the raw
    model output; they dispatch on the work order built from it. If the model
    wobbles on a field the taxonomy owns, that wobble never reaches a van, and
    a report that only measured the raw output would overstate the problem.
    """
    changed: set[str] = set()
    if a.incident_type_id != b.incident_type_id:
        changed.add("type")
    if a.settled_severity()[0] != b.settled_severity()[0]:
        changed.add("severity")
    if set(a.dispatch_characteristics()[0]) != set(b.dispatch_characteristics()[0]):
        changed.add("skills")
    if abs(a.estimated_duration_min - b.estimated_duration_min) >= 15:
        changed.add("duration")
    if (a.window_start_min, a.window_end_min) != (b.window_start_min, b.window_end_min):
        changed.add("window")
    if a.needs_human != b.needs_human:
        changed.add("escalation")
    return changed


def changed_fields(a: IntakeResult, b: IntakeResult) -> set[str]:
    """Which fields differ between two classifications, as bare names."""
    changed: set[str] = set()
    if a.incident_type_id != b.incident_type_id:
        changed.add("type")
    if a.severity != b.severity:
        changed.add("severity")
    if set(a.required_characteristics) != set(b.required_characteristics):
        changed.add("skills")
    if abs(a.estimated_duration_min - b.estimated_duration_min) >= 15:
        changed.add("duration")
    if (a.window_start_min, a.window_end_min) != (b.window_start_min, b.window_end_min):
        changed.add("window")
    if a.needs_human != b.needs_human:
        changed.add("escalation")
    return changed


@dataclass
class AblationReport:
    """How often the photo moves each field, against how often nothing does."""

    trials: int
    paired: dict[str, int]        # photo vs no photo, same trial
    self_with: dict[str, int]     # photo run vs the first photo run
    self_without: dict[str, int]  # no-photo run vs the first no-photo run
    cost_usd: float
    # The same self-comparison made on the work order the pipeline would
    # actually dispatch, after the taxonomy has overridden what it owns.
    self_dispatch: dict[str, int] = dataclass_field(default_factory=dict)
    failures: int = 0

    def noise(self, field: str) -> int:
        """Self-disagreement on identical input, taken from the worse arm.

        The worse of the two rather than the average: if either arm cannot
        reproduce itself on this field, the field is unreliable no matter which
        side the photo was added to.
        """
        return max(self.self_with.get(field, 0), self.self_without.get(field, 0))

    def verdict(self, field: str) -> str:
        """Whether this field's movement is above its own noise floor.

        The first version of this compared the two counts directly and called
        anything larger a result. At five trials that promoted 2/5 against 1/4
        to "above the noise floor" — a difference of one run, on a field a
        photograph cannot physically carry. A rule that can produce that is not
        a rule, so it now needs both a real sample and a real margin.
        """
        signal = self.paired.get(field, 0)
        if signal == 0:
            return "not moved by the photo"
        if self.trials < MIN_TRIALS_FOR_A_VERDICT:
            return f"too few trials to say (need {MIN_TRIALS_FOR_A_VERDICT})"

        comparisons = max(self.trials - 1, 1)
        signal_rate = signal / self.trials
        noise_rate = self.noise(field) / comparisons

        if signal_rate <= noise_rate:
            return "indistinguishable from noise"
        if signal_rate - noise_rate < MARGIN:
            return "too close to the noise to call"
        if signal == self.trials and self.noise(field) == 0:
            return "changed every trial"
        return "above the noise floor"

    def unstable_fields(self) -> list[str]:
        """Fields where the model cannot reproduce its own answer.

        Reported separately because it is not a fact about the photo at all.
        It is a fact about how much of this output can be built on.
        """
        comparisons = max(self.trials - 1, 1)
        return [
            f for f in FIELDS
            if self.noise(f) / comparisons > 0.5
        ]

    def lines(self) -> list[str]:
        comparisons = max(self.trials - 1, 0)
        out = [
            f"{self.trials} paired trials, ~${self.cost_usd:.4f} total"
            + (f"  ({self.failures} call(s) failed and were skipped)" if self.failures else ""),
            "",
            f"  {'field':<12} {'photo changed it':>17} {'model changed its own mind':>28}   verdict",
        ]
        for field in FIELDS:
            noise = max(self.self_with.get(field, 0), self.self_without.get(field, 0))
            out.append(
                f"  {field:<12} {self.paired.get(field, 0):>10}/{self.trials:<6} "
                f"{noise:>19}/{comparisons:<7}  {self.verdict(field)}"
            )
        if comparisons == 0:
            out += [
                "",
                "  With one trial there is no noise column, so nothing here can be",
                "  told apart from run-to-run variation. Use --repeat 10 or more.",
            ]
        unstable = self.unstable_fields()
        if unstable:
            out += [
                "",
                "  The model does not reproduce its own answer on identical input for:",
                "    " + ", ".join(unstable),
                "  That is not about the photo. Anything reading the raw model output",
                "  on these fields is reading a coin flip.",
            ]

        if self.self_dispatch or unstable:
            absorbed = [
                f for f in FIELDS
                if self.self_with.get(f, 0) > self.self_dispatch.get(f, 0)
            ]
            out += [
                "",
                f"  Same {comparisons} comparisons, made on the work order that would "
                "actually be dispatched:",
                "    " + (
                    ", ".join(
                        f"{f} {self.self_dispatch.get(f, 0)}/{comparisons}"
                        for f in FIELDS
                    )
                ),
            ]
            if absorbed:
                out.append(
                    "  Absorbed by the taxonomy before reaching a van: "
                    + ", ".join(absorbed)
                )
        return out


def ablation_study(
    text: str = "",
    image: str | Path | None = None,
    audio: str | Path | None = None,
    repeat: int = 5,
) -> AblationReport:
    """Run the with-photo/without-photo comparison `repeat` times.

    Reports the paired change rate beside the self-disagreement rate, because
    the first number is only interpretable next to the second.
    """
    from collections import Counter

    withs: list[IntakeResult] = []
    withouts: list[IntakeResult] = []
    paired: Counter = Counter()
    cost = 0.0
    failures = 0

    for _ in range(repeat):
        seen = receive(text=text, image=image, audio=audio)
        blind = receive(text=text, audio=audio)
        cost += seen.estimated_usd + blind.estimated_usd
        if seen.result is None or blind.result is None:
            failures += 1
            continue
        withs.append(seen.result)
        withouts.append(blind.result)
        paired.update(changed_fields(seen.result, blind.result))

    def drift(runs: list[IntakeResult], compare=changed_fields) -> Counter:
        counter: Counter = Counter()
        for later in runs[1:]:
            counter.update(compare(later, runs[0]))
        return counter

    return AblationReport(
        trials=len(withs),
        paired=dict(paired),
        self_with=dict(drift(withs)),
        self_without=dict(drift(withouts)),
        self_dispatch=dict(drift(withs, changed_dispatch_fields)),
        cost_usd=cost,
        failures=failures,
    )
