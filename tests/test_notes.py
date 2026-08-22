"""The notes experiment must stay honest as the project changes.

These tests guard the properties that make the triage comparison meaningful.
If any of them fails, the headline result is no longer trustworthy and the
README claim has to change.
"""

from __future__ import annotations

import random

import pytest

from fieldpilot.agents import rules_triage
from fieldpilot.sim import notes as notes_mod
from fieldpilot.sim import scenario as scenario_mod


def test_notes_never_state_a_priority() -> None:
    """Notes must describe situations, not conclusions.

    The moment a note says "urgent", the experiment stops testing judgement and
    starts testing string matching.
    """
    banned = {"urgent", "priority", "critical", "asap", "emergency", "important"}
    everything = notes_mod.ESCALATING + notes_mod.DEESCALATING + notes_mod.LOGISTICAL

    for situation in everything:
        for text in situation.phrasings:
            lowered = text.lower()
            for word in banned:
                assert word not in lowered, f"{word!r} leaks the answer in: {text}"


def test_every_situation_has_several_phrasings() -> None:
    """One wording per situation is what let a keyword scanner win before."""
    everything = notes_mod.ESCALATING + notes_mod.DEESCALATING + notes_mod.LOGISTICAL
    for situation in everything:
        assert len(situation.phrasings) >= 3
        assert len(set(situation.phrasings)) == len(situation.phrasings)


def test_deescalating_and_logistical_notes_exist_in_quantity() -> None:
    """If every note escalated, blind inflation would be a winning strategy."""
    rng = random.Random(7)
    picked = [notes_mod.sample(rng) for _ in range(4000)]
    up = sum(1 for n in picked if n.impact > 1.05)
    down = sum(1 for n in picked if n.impact < 0.95)
    flat = sum(1 for n in picked if n.text and abs(n.impact - 1.0) < 0.01)

    assert up > 500
    assert down > 400
    assert flat > 200
    # Neither direction may dominate so heavily that a constant strategy wins.
    assert 0.5 < down / up < 1.5


def test_logistical_notes_carry_no_urgency() -> None:
    for situation in notes_mod.LOGISTICAL:
        assert situation.impact == 1.0


def test_ground_truth_is_set_for_every_order() -> None:
    scn = scenario_mod.build(seed=42, n_orders=48)
    assert all(o.true_penalty > 0 for o in scn.work_orders)


def test_ground_truth_moves_with_the_note_not_the_category() -> None:
    """A de-escalating note must actually pull true urgency below the
    structured-only estimate, otherwise there is nothing for a reader to find."""
    scn = scenario_mod.build(seed=42, n_orders=60)

    lowered = 0
    raised = 0
    for order in scn.work_orders:
        structured, _ = rules_triage.penalty_for(order, scn.accounts[order.account_id])
        if order.true_penalty < structured * 0.9:
            lowered += 1
        elif order.true_penalty > structured * 1.1:
            raised += 1

    assert lowered >= 5, "no order was de-prioritised by its note"
    assert raised >= 5, "no order was escalated by its note"


def test_scenario_with_notes_is_still_reproducible() -> None:
    a = scenario_mod.build(seed=99, n_orders=40)
    b = scenario_mod.build(seed=99, n_orders=40)
    assert [o.notes for o in a.work_orders] == [o.notes for o in b.work_orders]
    assert [o.true_penalty for o in a.work_orders] == [o.true_penalty for o in b.work_orders]


def test_triage_never_receives_the_ground_truth() -> None:
    """The hidden answer must not leak into what any triage method can read."""
    from fieldpilot.agents import triage

    scn = scenario_mod.build(seed=42, n_orders=20)
    payload = triage.describe_backlog(scn.work_orders, scn.accounts)

    assert "true_penalty" not in payload
    for order in scn.work_orders:
        assert str(order.true_penalty) not in payload.replace(order.notes, "")


@pytest.mark.parametrize("seed", [42, 7, 2026])
def test_keyword_pass_is_not_a_strawman(seed: int) -> None:
    """The naive alternative has to actually do something, or the comparison
    is worthless."""
    scn = scenario_mod.build(seed=seed, n_orders=48)
    moved = 0
    for order in scn.work_orders:
        factor, hits = rules_triage.keyword_adjustment(order.notes)
        if hits:
            moved += 1
    assert moved >= 3, "the keyword pass fires on almost nothing"
