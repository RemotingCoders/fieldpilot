"""Command line entry points.

`python -m fieldpilot.cli compare` runs the same simulated day through both
planners and prints the difference. This is the command the demo video shows.
"""

from __future__ import annotations

import argparse
import sys

from fieldpilot.agents import rules_triage
from fieldpilot.domain.models import Location
from fieldpilot.planning import baseline, metrics, solver
from fieldpilot.planning.travel import TravelMatrix
from fieldpilot.sim import scenario as scenario_mod


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def cmd_compare(args: argparse.Namespace) -> int:
    scn = scenario_mod.build(seed=args.seed, n_orders=args.orders)
    rules_triage.apply(scn.work_orders, scn.accounts)

    # Both planners see exactly the same geography and the same travel times.
    locations: list[Location] = [o.location for o in scn.work_orders]
    locations += [r.start_location for r in scn.resources]
    shared = TravelMatrix.estimated(locations + [locations[0]])

    naive = baseline.dispatch(scn.work_orders, scn.resources, shared)
    optimised = solver.solve(
        scn.work_orders, scn.resources, shared, time_limit_s=args.time_limit
    )

    m_naive = metrics.score(naive, scn.work_orders, scn.accounts)
    m_opt = metrics.score(optimised, scn.work_orders, scn.accounts)

    print()
    print(f"Scenario seed {scn.seed} — {len(scn.work_orders)} work orders, "
          f"{len(scn.resources)} technicians")
    print("-" * 78)
    print(m_naive.summary_line())
    print(m_opt.summary_line())
    print("-" * 78)

    served_delta = m_opt.orders_served - m_naive.orders_served
    weighted_delta = m_opt.weighted_coverage_pct - m_naive.weighted_coverage_pct

    # Raw travel minutes are the wrong comparison and would flatter the wrong
    # planner: the optimiser drives further precisely because it completes more
    # jobs. Travel per completed job is the number a fleet manager budgets on.
    naive_per_job = m_naive.travel_minutes / max(m_naive.orders_served, 1)
    opt_per_job = m_opt.travel_minutes / max(m_opt.orders_served, 1)
    per_job_pct = 100.0 * (naive_per_job - opt_per_job) / naive_per_job if naive_per_job else 0.0

    print(f"orders served       {served_delta:+d}")
    print(
        f"travel per job      {naive_per_job:.1f} -> {opt_per_job:.1f} min "
        f"({per_job_pct:+.0f}%)"
    )
    print(
        f"total travel        {m_naive.travel_minutes} -> {m_opt.travel_minutes} min "
        f"(higher because more jobs get done)"
    )
    print(f"weighted coverage   {weighted_delta:+.1f} pts")
    print(f"safety jobs missed  {m_naive.safety_unserved} -> {m_opt.safety_unserved}")
    print()

    if args.routes:
        for resource in scn.resources:
            print(f"{resource.name} ({', '.join(resource.characteristics)})")
            for booking in optimised.bookings_for(resource.resource_id):
                order = next(
                    o for o in scn.work_orders if o.work_order_id == booking.work_order_id
                )
                account = scn.accounts[order.account_id]
                print(
                    f"   {_hhmm(booking.arrival_min)}-{_hhmm(booking.departure_min)}  "
                    f"{order.incident_type_id:<22} {account.name:<28} "
                    f"[{account.sla_tier.value}] {order.triage_rationale}"
                )
            print()

    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    scn = scenario_mod.build(seed=args.seed, n_orders=args.orders)
    rules_triage.apply(scn.work_orders, scn.accounts)
    plan = solver.solve(scn.work_orders, scn.resources, time_limit_s=args.time_limit)
    print(metrics.score(plan, scn.work_orders, scn.accounts).summary_line())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Plan the day once at 8am, then watch reality happen to it.

    This is the control condition for the disruption monitor: a good plan,
    executed blindly, with nobody reacting. Everything the agent adds gets
    measured against this run.
    """
    from fieldpilot.sim import engine as engine_mod
    from fieldpilot.sim import report as report_mod

    scn = scenario_mod.build(seed=args.seed, n_orders=args.orders)
    rules_triage.apply(scn.work_orders, scn.accounts)

    sim = engine_mod.Simulator(scn, seed=args.seed)
    plan = solver.solve(scn.work_orders, scn.resources, time_limit_s=args.time_limit)
    sim.load_plan(plan)

    events = sim.advance(engine_mod.DAY_END_MIN)

    print()
    print(f"Static plan, seed {scn.seed} — {len(scn.work_orders)} orders, "
          f"{len(scn.resources)} technicians, nobody watching")
    print("-" * 78)
    for event in events:
        if args.verbose or event.actionable:
            print("  " + event.line())
    print("-" * 78)
    print(report_mod.build(sim, "static plan").summary_line())
    print()
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    """Four ways of deciding what matters, measured against what actually did.

    Same backlog, same solver, same travel times. The only thing that varies is
    who writes the penalties:

      rules     structured fields only — cannot read the notes at all
      keywords  rules plus a good-faith keyword scan of the notes
      gemini    the model, reading the notes as prose
      oracle    scored directly from the hidden ground truth

    The oracle is not a competitor; it is the ceiling. Without it we would know
    that one method beat another but not whether the gap left on the table was
    trivial or enormous.
    """
    from fieldpilot.agents import triage as llm_triage

    def fresh():
        return scenario_mod.build(seed=args.seed, n_orders=args.orders)

    base = fresh()
    locations: list[Location] = [o.location for o in base.work_orders]
    locations += [r.start_location for r in base.resources]
    shared = TravelMatrix.estimated(locations + [locations[0]])

    def evaluate(scn, label: str):
        plan = solver.solve(
            scn.work_orders, scn.resources, shared, time_limit_s=args.time_limit
        )
        m = metrics.score(plan, scn.work_orders, scn.accounts)
        m.planner = label
        return m

    scn_rules = fresh()
    rules_triage.apply(scn_rules.work_orders, scn_rules.accounts)
    m_rules = evaluate(scn_rules, "rules")

    scn_kw = fresh()
    rules_triage.apply_with_keywords(scn_kw.work_orders, scn_kw.accounts)
    m_kw = evaluate(scn_kw, "keywords")

    scn_llm = fresh()
    result = llm_triage.apply(scn_llm.work_orders, scn_llm.accounts)
    m_llm = evaluate(scn_llm, "gemini")

    scn_oracle = fresh()
    for order in scn_oracle.work_orders:
        order.penalty_cost = max(1, order.true_penalty)
        order.triage_rationale = "ground truth"
    m_oracle = evaluate(scn_oracle, "oracle")

    noted = sum(1 for o in base.work_orders if o.notes)

    print()
    print(f"Who understands the day? — seed {args.seed}, "
          f"{len(base.work_orders)} orders, {noted} with free-text notes")
    print("-" * 92)
    print(result.summary_line())
    print()
    for m in (m_rules, m_kw, m_llm, m_oracle):
        print("  " + m.summary_line())
    print("-" * 92)

    floor = m_rules.true_value_pct
    ceiling = m_oracle.true_value_pct
    headroom = ceiling - floor

    def captured(m) -> str:
        if headroom <= 0.01:
            return "n/a"
        return f"{100.0 * (m.true_value_pct - floor) / headroom:+.0f}% of headroom"

    print(f"headroom over rules   {headroom:+.1f} pts of true value")
    print(f"keywords captured     {captured(m_kw)}")
    print(f"gemini captured       {captured(m_llm)}")
    print()

    if args.ablate:
        # Measuring note influence by diffing gemini against the rules engine
        # was wrong: the two disagree on baselines for every order, noted or
        # not, so ordinary calibration differences looked like note effects.
        #
        # The only honest measurement is to ask the same model the same
        # question twice, once with the notes and once without, and diff its
        # answers against itself. Costs one extra call.
        stripped = fresh()
        for order in stripped.work_orders:
            order.notes = ""
        blind = llm_triage.apply(stripped.work_orders, stripped.accounts)

        by_id = {o.work_order_id: o for o in stripped.work_orders}
        print("Ablation — same model, same backlog, notes removed:")
        print(f"  second call: {blind.summary_line()}")
        print()

        moved_noted = 0
        moved_unnoted = 0
        rows = []
        for order in scn_llm.work_orders:
            twin = by_id[order.work_order_id]
            shift = order.penalty_cost / max(twin.penalty_cost, 1)
            drifted = shift > 1.25 or shift < 0.8
            if order.notes and drifted:
                moved_noted += 1
                rows.append((shift, order))
            elif not order.notes and drifted:
                moved_unnoted += 1

        for shift, order in sorted(rows, key=lambda r: -abs(r[0] - 1)):
            arrow = "UP  " if shift > 1 else "DOWN"
            print(f"  {arrow} x{shift:5.1f}  {order.notes[:62]}")

        print()
        print(f"  orders with a note that moved     {moved_noted}/"
              f"{sum(1 for o in scn_llm.work_orders if o.notes)}")
        print(f"  orders with NO note that moved    {moved_unnoted}/"
              f"{sum(1 for o in scn_llm.work_orders if not o.notes)}"
              "   <- run-to-run noise, not note influence")
        print()

    elif args.routes:
        # Kept, but honestly labelled: this is gemini against the rules engine,
        # which is a different question from what the note did.
        print("Gemini vs rules, on orders that carry a note")
        print("(baseline calibration differs everywhere; use --ablate to isolate"
              " the note's own effect)")
        for order in scn_llm.work_orders:
            if not order.notes:
                continue
            twin = next(
                o for o in scn_rules.work_orders if o.work_order_id == order.work_order_id
            )
            shift = order.penalty_cost / max(twin.penalty_cost, 1)
            if 0.75 < shift < 1.35:
                continue
            arrow = "UP  " if shift > 1 else "DOWN"
            print(f"  {arrow} x{shift:5.1f}  {order.notes[:62]}")
            print(f"           -> {order.triage_rationale[:86]}")
        print()

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fieldpilot")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler in (
        ("compare", cmd_compare),
        ("plan", cmd_plan),
        ("run", cmd_run),
        ("triage", cmd_triage),
    ):
        p = sub.add_parser(name)
        p.add_argument("--seed", type=int, default=42)
        # Triage only means something when the day is oversubscribed enough
        # that jobs have to be sacrificed. At 26 orders everything that matters
        # fits and every method scores identically.
        p.add_argument("--orders", type=int, default=48 if name == "triage" else 26)
        p.add_argument("--time-limit", type=int, default=5)
        p.add_argument("--routes", action="store_true", help="print each technician's day")
        p.add_argument("--verbose", action="store_true", help="print every event, not just actionable ones")
        p.add_argument(
            "--ablate",
            action="store_true",
            help="triage only: score the backlog twice, with and without the notes, "
                 "to isolate what the notes actually changed (costs one extra call)",
        )
        p.set_defaults(func=handler)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
