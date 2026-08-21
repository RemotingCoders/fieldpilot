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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fieldpilot")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler in (("compare", cmd_compare), ("plan", cmd_plan)):
        p = sub.add_parser(name)
        p.add_argument("--seed", type=int, default=42)
        p.add_argument("--orders", type=int, default=26)
        p.add_argument("--time-limit", type=int, default=5)
        p.add_argument("--routes", action="store_true", help="print each technician's day")
        p.set_defaults(func=handler)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
