"""The demo must be filmable: one command, no crash, every act present."""

from __future__ import annotations

import pytest

from fieldpilot.cli import main


@pytest.fixture(scope="module")
def demo_output():
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main([
            "demo", "--offline", "--seed", "42",
            "--orders", "30", "--solution-limit", "20",
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
