"""The demo must be filmable: one command, no crash, every act present."""

from __future__ import annotations

import pytest

from fieldpilot.cli import main


@pytest.fixture(scope="module")
def demo_output():
    import io
    from contextlib import redirect_stdout

    # Deliberately no --solution-limit: this is the exact invocation the video
    # films, and the flag's default is what once let the filmed take print
    # `reproducible: False` while every test passed the flag explicitly.
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main([
            "demo", "--offline", "--seed", "42", "--orders", "30",
        ])
    assert code == 0
    return buffer.getvalue()


def test_every_act_appears_in_order(demo_output):
    acts = [
        "THE MORNING BACKLOG",
        "THE PLAN",
        "THE DAY HAPPENS ANYWAY",
        "TELLING THE CUSTOMERS",
        "WHAT NEEDS A PERSON TONIGHT",
        "SCORECARD",
    ]
    positions = [demo_output.find(a) for a in acts]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions)


def test_offline_mode_says_so_and_spends_nothing(demo_output):
    """The rehearsal switch must be visible on screen, and rehearsals are free."""
    assert "offline rehearsal" in demo_output
    assert "model spend" not in demo_output


def test_the_plan_is_declared_reproducible(demo_output):
    """The video rules require an unedited live run; a plan that changes
    between rehearsal and take would make the take unrehearsable."""
    assert "reproducible: True" in demo_output


def test_escalation_queue_agrees_with_the_scorecard(demo_output):
    """If the scorecard admits an unserved safety call, act 6 must raise it.

    The first version of act 6 read the morning plan's unserved list, so an
    emergency that arrived mid-day and was never resolved could not appear —
    the queue said "nothing needs a person tonight" directly above a scorecard
    showing safety 2/3. The queue exists to catch exactly that.
    """
    import re

    match = re.search(r"safety (\d+)/(\d+)", demo_output)
    assert match, "scorecard must report safety served/total"
    served, total = int(match.group(1)), int(match.group(2))
    if served < total:
        assert "BLOCKING" in demo_output
        assert "nothing needs a person tonight" not in demo_output


def test_offline_demo_never_calls_a_model(monkeypatch):
    """Belt and braces: rehearsal mode must stay free even if someone edits
    the demo later. Any model call in offline mode is a test failure."""
    import fieldpilot.agents.intake as intake_mod
    import fieldpilot.agents.triage as triage_mod

    def _boom(*args, **kwargs):
        raise AssertionError("model call attempted in offline mode")

    monkeypatch.setattr(intake_mod, "receive", _boom)
    monkeypatch.setattr(triage_mod, "apply", _boom)

    import io
    from contextlib import redirect_stdout

    with redirect_stdout(io.StringIO()):
        assert main([
            "demo", "--offline", "--seed", "7",
            "--orders", "20", "--solution-limit", "20",
        ]) == 0
