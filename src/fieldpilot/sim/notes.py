"""Free-text notes on work orders, and the hidden truth behind them.

Every field service system has this field. It is where the call taker types
what the customer actually said, and it is where most of the information that
decides a day lives. No structured column captures "the tenant is 89 and has no
other heat source".

## How this is set up so the experiment is honest

Each note carries a hidden `impact` multiplier that never reaches any triage
implementation. The scenario uses it to compute each order's *true* urgency —
what the business would, in hindsight, have wanted done first. Triage methods
are then scored on how much true urgency their plan actually delivered.

Four properties keep this from being a rigged demo:

1. **Notes state situations, not priorities.** None says "urgent". They say what
   happened, and the implication has to be worked out.
2. **A third point downwards.** Some notes mean a job matters *less*. A method
   that reads text and simply inflates every score does worse than one that
   ignores text entirely. Discrimination is required, not enthusiasm.
3. **Some are pure logistics.** Parking instructions and gate codes carry no
   urgency. A reader that reacts to the presence of text is punished.
4. **Every situation has several phrasings.** This is the one that matters
   most. An earlier version of this file had one wording per situation, and a
   keyword scanner recovered 77% of the available signal — because the same
   author wrote both the notes and the keyword list. Real call takers write the
   same situation a dozen different ways, and no fixed vocabulary covers it.
   The paraphrases below were written as a person would write them, without
   consulting the keyword list; some happen to contain keyword terms and some
   do not, which is exactly the real distribution.

The honest caveat, stated here and in the README: the ground truth is authored
alongside the notes, so this measures whether a method recovers signal encoded
in prose. That is a real and necessary capability, but it is not by itself
proof of business value in a live deployment.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Note:
    text: str
    impact: float  # hidden multiplier on true urgency; never shown to triage


@dataclass(frozen=True)
class Situation:
    """One real circumstance, written the several ways a person might write it."""

    impact: float
    phrasings: tuple[str, ...]


# --------------------------------------------------------------------------
# Situations that make a job matter far more than its category suggests
# --------------------------------------------------------------------------

ESCALATING: list[Situation] = [
    Situation(
        3.4,
        (
            "Water is now reaching the electrical panel in the basement.",
            "The puddle has spread under the fuse box downstairs.",
            "Whatever is dripping is landing near where the mains come in.",
            "Caretaker turned the power off to that floor as a precaution.",
        ),
    ),
    Situation(
        3.0,
        (
            "Building manager says they will terminate the contract for the whole "
            "property if nobody comes today.",
            "Administration is threatening to go out to tender for all sixty units.",
            "He was clear that this is the last time before they change supplier.",
            "They have asked us to put our renewal in writing by Friday, and this "
            "visit is why.",
        ),
    ),
    Situation(
        3.3,
        (
            "There are small children in the flat and it has smelled of gas since yesterday.",
            "Mother reports a strange odour in the kitchen; two toddlers at home.",
            "Family says the whole hallway smells odd and they are sleeping elsewhere.",
            "Neighbour phoned as well, says the landing does not smell right.",
        ),
    ),
    Situation(
        2.6,
        (
            "Tenant is 89, lives alone, and has no other source of heat.",
            "Occupant is an older lady on her own with nothing else to warm the flat.",
            "Resident is frail and there is no backup heating in the property.",
            "Daughter called on her behalf, worried about her being cold overnight.",
        ),
    ),
    Situation(
        2.3,
        (
            "Shop is closed to the public until this is fixed; they lose a day of trade.",
            "They have had to shut the doors and turn customers away.",
            "Cannot open tomorrow either unless somebody attends.",
            "Owner says every hour shut is money he does not get back.",
        ),
    ),
    Situation(
        2.1,
        (
            "Third van we have sent to this address this month.",
            "We have been out here twice already and it is still not resolved.",
            "Customer pointed out this is the third appointment for the same fault.",
            "Previous two visits did not fix it; they are not pleased.",
        ),
    ),
    Situation(
        2.4,
        (
            "Ward is being kept warm with portable heaters that trip the breakers.",
            "They are running space heaters and the circuit keeps cutting out.",
            "Staff are plugging in radiators and blowing fuses doing it.",
            "Temporary heating is overloading the supply.",
        ),
    ),
    Situation(
        2.2,
        (
            "Their walk-in freezer is on the same circuit and the stock is at risk.",
            "If this goes down again the cold room goes with it.",
            "Chef says the produce will not survive another outage.",
            "Same board feeds the refrigeration, so stock is exposed.",
        ),
    ),
]

# --------------------------------------------------------------------------
# Situations that make a job matter considerably less
# --------------------------------------------------------------------------

DEESCALATING: list[Situation] = [
    Situation(
        0.26,
        (
            "Owner is abroad until next month and left word that there is no rush.",
            "Nobody will be there for several weeks; happy for us to come later.",
            "They are away travelling and asked us to leave it until they return.",
            "Keyholder is on holiday, so nothing can happen for a while anyway.",
        ),
    ),
    Situation(
        0.32,
        (
            "They say the noise stopped by itself; they still want it looked at eventually.",
            "It seems to have settled down on its own since they called.",
            "The fault has not come back, but they would like it checked sometime.",
            "Behaving normally again, so no hurry from their side.",
        ),
    ),
    Situation(
        0.37,
        (
            "Customer borrowed an electric heater from a neighbour and can wait.",
            "They have sorted themselves out with a plug-in radiator for now.",
            "Managing fine in the meantime with a heater they already had.",
            "Not cold, they have improvised something that works.",
        ),
    ),
    Situation(
        0.42,
        (
            "Another contractor already got it running; this would just be a check.",
            "Someone else attended and it is working, we are only confirming.",
            "It was patched by their own maintenance chap yesterday.",
            "Running fine since a third party looked at it; this is a formality.",
        ),
    ),
    Situation(
        0.45,
        (
            "Flat is empty, no tenants until the new lease starts.",
            "Property is unoccupied at the moment.",
            "Nobody living there; it is between lettings.",
            "Vacant unit, so no one is affected either way.",
        ),
    ),
]

# --------------------------------------------------------------------------
# Real notes that carry no urgency at all
# --------------------------------------------------------------------------

LOGISTICAL: list[Situation] = [
    Situation(
        1.0,
        (
            "Doorbell is broken, call the mobile on arrival.",
            "Buzzer does not work; ring ahead.",
            "Phone when outside, the intercom is dead.",
        ),
    ),
    Situation(
        1.0,
        (
            "Park on the side street, the entrance is narrow.",
            "No space out front, leave the van round the corner.",
            "Loading bay is tight, better to park on the avenue.",
        ),
    ),
    Situation(
        1.0,
        (
            "Ask for Marcela at the front desk.",
            "Reception will have the keys, ask for the duty manager.",
            "Speak to the caretaker, he is expecting us.",
        ),
    ),
    Situation(
        1.0,
        (
            "Access is through the rear courtyard, gate code 4417.",
            "Side gate, the code is on the job sheet.",
            "Go around the back; the front is locked during the day.",
        ),
    ),
    Situation(
        1.0,
        (
            "Dog in the yard, owner will shut it in before we arrive.",
            "There is a large dog, they will put it away.",
            "Warn the engineer about the dog out the back.",
        ),
    ),
]

EMPTY = Note("", 1.0)


def _pick(rng: random.Random, situations: list[Situation]) -> Note:
    situation = rng.choice(situations)
    return Note(rng.choice(situation.phrasings), situation.impact)


def sample(rng: random.Random) -> Note:
    """Pick a note for one work order.

    Most orders have no note, which is what the field looks like in practice.
    Of the ones that do, escalating and de-escalating are close to balanced so
    that neither ignoring the text nor over-reacting to it is a winning
    strategy.
    """
    roll = rng.random()
    if roll < 0.44:
        return EMPTY
    if roll < 0.68:
        return _pick(rng, ESCALATING)
    if roll < 0.87:
        return _pick(rng, DEESCALATING)
    return _pick(rng, LOGISTICAL)
