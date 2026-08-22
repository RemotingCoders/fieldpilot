"""Triage tests that need no API key.

The model call itself cannot be unit tested cheaply, but everything around it
can — and that surrounding logic is where a dispatch system quietly goes wrong.
A model that returns eight scores for a backlog of twenty-six is not an error
anyone notices at runtime; it is eighteen jobs silently sinking to the bottom of
the day. These tests exist so that failure is loud.
"""

from __future__ import annotations

import json

import pytest

from fieldpilot.agents import rules_triage, triage
from fieldpilot.agents.triage import TriageDecision, _apply_decisions
from fieldpilot.sim import scenario as scenario_mod


@pytest.fixture()
def backlog():
    scn = scenario_mod.build(seed=42, n_orders=12)
    return scn


def test_every_order_is_scored_even_when_the_model_returns_nothing(backlog) -> None:
    result = _apply_decisions(backlog.work_orders, backlog.accounts, [], "test-model")

    assert result.scored_by_model == 0
    assert result.scored_by_rules == len(backlog.work_orders)
    assert result.degraded
    assert all(o.penalty_cost > 0 for o in backlog.work_orders)
    assert all(o.triage_rationale for o in backlog.work_orders)


def test_partial_model_output_is_topped_up_from_rules(backlog) -> None:
    """The failure mode that matters: a model that answers for only some orders."""
    covered = backlog.work_orders[:5]
    decisions = [
        TriageDecision(
            work_order_id=o.work_order_id,
            penalty_cost=12_345,
            rationale="model said so",
        )
        for o in covered
    ]

    result = _apply_decisions(backlog.work_orders, backlog.accounts, decisions, "m")

    assert result.scored_by_model == 5
    assert result.scored_by_rules == len(backlog.work_orders) - 5
    for order in covered:
        assert order.penalty_cost == 12_345
    for order in backlog.work_orders[5:]:
        assert order.penalty_cost != 12_345
        assert order.triage_rationale.startswith("[rules]")


@pytest.mark.parametrize("bad_penalty", [0, -5, 99, 500_001, 10_000_000])
def test_out_of_range_penalties_are_rejected(backlog, bad_penalty: int) -> None:
    """A model typo that adds a zero would dominate the entire cost function."""
    order = backlog.work_orders[0]
    decisions = [
        TriageDecision(
            work_order_id=order.work_order_id,
            penalty_cost=bad_penalty,
            rationale="nonsense",
        )
    ]

    result = _apply_decisions(backlog.work_orders, backlog.accounts, decisions, "m")

    assert result.scored_by_model == 0
    assert order.penalty_cost != bad_penalty
    assert order.triage_rationale.startswith("[rules]")


def test_empty_rationale_is_rejected(backlog) -> None:
    """A number with no explanation is not usable by a dispatcher."""
    order = backlog.work_orders[0]
    decisions = [
        TriageDecision(work_order_id=order.work_order_id, penalty_cost=9_000, rationale="   ")
    ]
    result = _apply_decisions(backlog.work_orders, backlog.accounts, decisions, "m")
    assert result.scored_by_model == 0


def test_decisions_for_unknown_orders_are_ignored(backlog) -> None:
    """Hallucinated ids must not create phantom work."""
    decisions = [
        TriageDecision(work_order_id="wo-does-not-exist", penalty_cost=50_000, rationale="x")
    ]
    result = _apply_decisions(backlog.work_orders, backlog.accounts, decisions, "m")

    assert result.scored_by_model == 0
    assert len(result.orders) == len(backlog.work_orders)
    assert all(o.work_order_id != "wo-does-not-exist" for o in result.orders)


def test_backlog_description_is_valid_json_and_complete(backlog) -> None:
    payload = triage.describe_backlog(backlog.work_orders, backlog.accounts)
    rows = json.loads(payload)

    assert len(rows) == len(backlog.work_orders)
    sent_ids = {r["id"] for r in rows}
    assert sent_ids == {o.work_order_id for o in backlog.work_orders}

    # The signals that justify having a model at all must actually reach it.
    for key in ("severity", "sla", "days_waiting", "times_rescheduled"):
        assert all(key in r for r in rows)


def test_apply_never_raises_without_credentials(backlog, monkeypatch) -> None:
    """No API key, no network, no crash — the day still gets planned."""
    monkeypatch.setattr(
        triage,
        "_run_agent",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no credentials")),
    )

    result = triage.apply(backlog.work_orders, backlog.accounts, model="unreachable")

    assert result.error is not None
    assert result.degraded
    assert result.scored_by_rules == len(backlog.work_orders)
    assert all(o.penalty_cost > 0 for o in backlog.work_orders)


def test_empty_backlog_is_handled() -> None:
    assert triage.apply([], {}).orders == []


def test_rules_and_model_paths_produce_solvable_plans(backlog) -> None:
    """Whatever writes the penalties, the solver must still be able to plan."""
    from fieldpilot.planning import solver

    rules_triage.apply(backlog.work_orders, backlog.accounts)
    plan = solver.solve(backlog.work_orders, backlog.resources, time_limit_s=2)
    assert plan.bookings or plan.unserved_work_order_ids


def test_cost_reporting_is_visible_when_tokens_were_spent() -> None:
    """Spending should be legible at the moment it happens, not a day later
    in a billing console."""
    from fieldpilot.agents.triage import TriageResult

    result = TriageResult(orders=[], model="gemini-3.5-flash")
    result.scored_by_model = 48
    result.input_tokens = 6_000
    result.output_tokens = 4_000

    assert result.estimated_usd > 0
    line = result.summary_line()
    assert "6000 in" in line and "4000 out" in line and "$" in line


def test_no_cost_shown_when_nothing_was_spent() -> None:
    from fieldpilot.agents.triage import TriageResult

    result = TriageResult(orders=[], model="rules-only")
    assert "$" not in result.summary_line()


def test_prompt_forbids_logistics_from_moving_the_score() -> None:
    """The one clear error the model made on the first real run was nudging a
    purely logistical note upwards. The instruction now addresses it, and this
    test keeps that instruction from being edited away silently."""
    from fieldpilot.agents.triage import INSTRUCTION

    lowered = INSTRUCTION.lower()
    assert "logistics never move it" in lowered
    assert "parking" in lowered and "intercom" in lowered
    assert "ordering" in lowered
