"""What the comms agent is allowed to say, and what happens when it says more."""

from __future__ import annotations

import pytest

from fieldpilot.agents.comms import (
    MAX_CHARS,
    DraftedMessage,
    Notification,
    NotificationKind,
    verify,
)


def _late(**kwargs) -> Notification:
    base = dict(
        kind=NotificationKind.RUNNING_LATE,
        customer_name="Marta Giordano",
        work_order_id="wo-014",
        technician_name="Ana",
        original_time="11:00",
        new_time="15:40",
        reason="Ana is finishing an emergency gas call.",
        options=["We can also come tomorrow at 09:00 if that suits you better."],
    )
    base.update(kwargs)
    return Notification(**base)


# ----------------------------------------------------------------------
# The template floor
# ----------------------------------------------------------------------

def test_every_kind_produces_a_sendable_template():
    """Nothing here may fail into silence."""
    for kind in NotificationKind:
        note = _late(kind=kind)
        text = note.template()
        assert text.strip()
        assert len(text) <= MAX_CHARS * 2


def test_the_template_passes_its_own_verifier():
    """If the floor could not pass the check, the check would be unusable."""
    note = _late()
    assert verify(note.template(), note) == []


def test_the_template_addresses_a_business_by_its_whole_name():
    """Greeting a print shop as "Imprenta" tells the customer instantly that a
    machine wrote to them."""
    note = _late(customer_name="Imprenta Salguero")
    assert note.template().startswith("Imprenta Salguero,")


def test_a_template_with_no_optional_facts_still_reads():
    note = _late(technician_name="", reason="", options=[])
    assert "None" not in note.template()
    assert "  " not in note.template()


# ----------------------------------------------------------------------
# The verifier
# ----------------------------------------------------------------------

def test_a_faithful_message_passes():
    note = _late()
    assert verify(
        "Marta, your 11:00 visit is running late. Ana should reach you around 15:40.",
        note,
    ) == []


def test_an_invented_time_is_caught():
    """The core failure: a time nobody in the system committed to."""
    note = _late()
    problems = verify("Marta, Ana will be with you by 12:30.", note)
    assert any("not in the facts" in p for p in problems)


@pytest.mark.parametrize(
    "message,label",
    [
        ("Marta, sorry for the delay — there will be no charge for this visit.", "price"),
        ("Marta, we guarantee Ana arrives at 15:40.", "guarantee"),
        ("Marta, we will refund you for the wait.", "refund"),
        ("Marta, we would like to offer you a discount.", "compensation"),
        ("Marta, Ana will be there within the hour.", "time commitment"),
        ("Marta, message us on WhatsApp to rebook.", "contact"),
    ],
)
def test_commitments_are_refused(message, label):
    """These are promises made on the company's behalf by something with no
    authority to make them."""
    assert verify(message, _late()) != [], label


def test_spanish_commitments_are_caught_too():
    """The model writes whichever language it feels like."""
    assert verify("Marta, la visita va sin cargo.", _late()) != []
    assert verify("Marta, le garantizamos la llegada.", _late()) != []


def test_an_overlong_message_is_refused():
    note = _late()
    problems = verify("Marta, " + "x" * (MAX_CHARS + 1), note)
    assert any("too long" in p for p in problems)


def test_times_licence_their_own_components():
    """15:40 in the facts must not make '40' look invented."""
    note = _late()
    assert verify("Marta, Ana arrives at 15:40 rather than 11:00.", note) == []


def test_numbers_from_the_options_are_allowed():
    note = _late()
    assert verify("Marta, we can come tomorrow at 09:00 instead.", note) == []


# ----------------------------------------------------------------------
# What draft() does with a bad draft
# ----------------------------------------------------------------------

def test_a_rejected_draft_falls_back_to_the_template_not_a_repair(monkeypatch):
    """A patched-up message is one nobody has read in its final form."""
    from fieldpilot.agents import comms

    note = _late()

    async def _bad(_notification, _model):
        return DraftedMessage(
            text="Marta, we guarantee Ana by 12:30 and there will be no charge.",
            source="model",
            violations=["guarantee", "price promise"],
        )

    monkeypatch.setattr(comms, "_draft_async", _bad)
    result = comms.draft(note)
    assert result.source == "template"
    assert result.text == note.template()
    assert result.violations  # the reason is kept, not swallowed


def test_a_clean_draft_is_used(monkeypatch):
    from fieldpilot.agents import comms

    async def _good(_notification, _model):
        return DraftedMessage(text="Marta, Ana is running late.", source="model")

    monkeypatch.setattr(comms, "_draft_async", _good)
    assert comms.draft(_late()).source == "model"


def test_a_crash_still_sends_something(monkeypatch):
    from fieldpilot.agents import comms

    async def _boom(_notification, _model):
        raise RuntimeError("quota exceeded")

    monkeypatch.setattr(comms, "_draft_async", _boom)
    result = comms.draft(_late())
    assert result.source == "template"
    assert result.text.strip()
    assert "quota" in (result.error or "")


def test_an_empty_draft_falls_back(monkeypatch):
    from fieldpilot.agents import comms

    async def _empty(_notification, _model):
        return DraftedMessage(text="   ", source="model")

    monkeypatch.setattr(comms, "_draft_async", _empty)
    assert comms.draft(_late()).source == "template"


def test_drafting_without_credentials_never_raises():
    """Same contract as intake and triage: no cloud account, no crash."""
    from fieldpilot.agents import comms

    result = comms.draft(_late(), model="definitely-not-a-model")
    assert result.text.strip()
