"""Command line entry points.

`python -m fieldpilot.cli compare` runs the same simulated day through both
planners and prints the difference. This is the command the demo video shows.
"""

from __future__ import annotations

import argparse
import sys
import textwrap

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
        scn.work_orders, scn.resources, shared,
        time_limit_s=args.time_limit,
        solution_limit=args.solution_limit,
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
    if not optimised.reproducible:
        print()
        print("note: solved against a wall clock, so this plan is not guaranteed to")
        print("      repeat on a differently loaded machine. Add --solution-limit 30")
        print("      for a result that does.")
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


def cmd_watch(args: argparse.Namespace) -> int:
    """Does watching the day help? It depends on who is doing the triage.

    An earlier version compared "watched" against "unwatched" and found the
    monitor made things *worse*. The cause was not the monitor: it was
    re-planning against penalties written by a rules engine that cannot read
    the notes. Four re-plans applied that poor judgement four times instead of
    once. So the experiment is a 2x2 — triage quality and monitoring are not
    independent.

    On repeated seeds: the day-level disruptions are fixed by the seed, but
    per-visit outcomes are drawn as each visit begins, so configurations that
    make different choices diverge into different trajectories. That is
    unavoidable in an intervention study, and it means a single seed cannot
    support a number. Run several.
    """
    from fieldpilot.agents import monitor as monitor_mod
    from fieldpilot.agents import triage as llm_triage
    from fieldpilot.sim import engine as engine_mod
    from fieldpilot.sim import orchestrator
    from fieldpilot.sim import report as report_mod

    def rules_fn(orders, accounts):
        rules_triage.apply(orders, accounts)

    def gemini_fn(orders, accounts):
        llm_triage.apply(orders, accounts)

    configs = [
        ("rules triage, unwatched", rules_fn, monitor_mod.NoMonitor),
        ("rules triage, watched", rules_fn, monitor_mod.RulesMonitor),
    ]
    if not args.rules_only:
        configs += [
            ("gemini triage, unwatched", gemini_fn, monitor_mod.NoMonitor),
            ("gemini triage, watched", gemini_fn, monitor_mod.GeminiMonitor),
        ]

    seeds = [args.seed + i for i in range(max(1, args.seeds))]
    tally: dict[str, list] = {label: [] for label, _, _ in configs}
    last_run = None

    print()
    print(f"Triage quality x monitoring — {len(seeds)} seed(s) from {args.seed}, "
          f"{args.orders} orders")
    print("-" * 100)

    for seed in seeds:
        for label, triage_fn, monitor_cls in configs:
            scn = scenario_mod.build(seed=seed, n_orders=args.orders)
            triage_fn(scn.work_orders, scn.accounts)

            sim = engine_mod.Simulator(scn, seed=seed)
            sim.load_plan(
                solver.solve(scn.work_orders, scn.resources, time_limit_s=args.time_limit)
            )

            mon = monitor_cls()
            log = orchestrator.run_day(
                sim, mon, time_limit_s=args.time_limit, triage_fn=triage_fn
            )
            day = report_mod.build(sim, label)
            tally[label].append(day)
            last_run = (label, mon, log)

        if len(seeds) > 1:
            print(f"  seed {seed} done")

    def mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    def spread(values: list[float]) -> float:
        """Half the range. With a handful of seeds this is more honest than a
        standard deviation, which implies more samples than we have."""
        return (max(values) - min(values)) / 2 if len(values) > 1 else 0.0

    floor_label = configs[0][0]
    floor_days = tally[floor_label]

    print("-" * 100)
    print("  Absolute (day-to-day variance dominates; read the paired table below)")
    for label, _, _ in configs:
        days = tally[label]
        values = [d.true_value_pct for d in days]
        band = f" +/-{spread(values):4.1f}" if len(seeds) > 1 else ""
        print(
            f"    {label:<26} true value {mean(values):5.1f}%{band}  "
            f"jobs {mean([d.jobs_completed for d in days]):4.1f}  "
            f"late {mean([d.total_lateness_min for d in days]):5.0f}min  "
            f"safety {sum(d.safety_completed for d in days)}/"
            f"{sum(d.safety_total for d in days)}"
        )

    if len(seeds) > 1:
        # Each seed runs every configuration on the same scenario, so the
        # difficulty of that particular day cancels out of a per-seed
        # difference. Comparing means would drown a real effect in variance
        # that both sides share.
        print()
        print("  Paired against the same seed's baseline")
        deltas: dict[str, list[float]] = {}
        for label, _, _ in configs[1:]:
            per_seed = [
                after.true_value_pct - before.true_value_pct
                for before, after in zip(floor_days, tally[label])
            ]
            deltas[label] = per_seed
            wins = sum(1 for d in per_seed if d > 0)
            print(
                f"    {label:<26} {mean(per_seed):+5.1f} pts  "
                f"+/-{spread(per_seed):4.1f}  "
                f"better on {wins}/{len(per_seed)} seeds"
            )

        if len(configs) == 4:
            parts = mean(deltas[configs[1][0]]) + mean(deltas[configs[2][0]])
            both = mean(deltas[configs[3][0]])
            print()
            print(f"    monitoring alone + triage alone      {parts:+5.1f} pts")
            print(f"    both together                        {both:+5.1f} pts")
            print(f"    interaction, the non-additive part   {both - parts:+5.1f} pts")
    print()

    if args.verbose and last_run:
        label, _, log = last_run
        print(f"Timeline — {label}, seed {seeds[-1]}")
        for row in log.timeline():
            print(row)
        print()

    return 0


SAMPLE_REQUESTS = [
    "buenas, soy del edificio de belgrano 1420. el portero dice que en el "
    "subsuelo hay olor raro cerca de la caldera desde ayer a la tarde. hay dos "
    "familias con chicos en el primer piso. pueden venir hoy?",

    "hola! el aire del local no enfria nada desde el jueves. abrimos de 10 a 19 "
    "pero el tecnico puede venir cualquier dia, total el calor ya nos hizo "
    "perder la semana",

    "te escribo por lo del service anual de la caldera, no hay apuro ninguno, "
    "cuando tengan un hueco. el depto esta vacio hasta noviembre igual",

    "URGENTE se esta filtrando agua del termotanque y esta llegando abajo del "
    "tablero de luz del pasillo. corte la llave general por las dudas",

    "buen dia, vengo llamando hace una semana. es la tercera vez que mandan a "
    "alguien por el mismo ruido y sigue igual. ya me estoy cansando la verdad",

    "hola necesito que vengan a ver una cosa del calefon. estoy a la mañana "
    "nomas, despues de las 13 no hay nadie",
]


def cmd_intake(args: argparse.Namespace) -> int:
    """Turn a real customer request into a work order.

    With --ablate, classify twice — once with the photograph and once without —
    and report what the picture actually changed. If it changes nothing, the
    image is decoration and this says so.
    """
    from fieldpilot.agents import intake as intake_mod

    texts = [args.text] if args.text else []
    if args.sample:
        texts = [SAMPLE_REQUESTS[args.sample - 1]]
    elif not texts and not args.image and not args.audio:
        texts = SAMPLE_REQUESTS

    print()
    for text in texts or [""]:
        if text:
            print(f'  "{text[:96]}{"..." if len(text) > 96 else ""}"')

        outcome = intake_mod.receive(
            text=text, image=args.image, audio=args.audio
        )
        print("  -> " + outcome.summary_line())
        if outcome.result:
            # The reasoning is the most useful thing intake produces, especially
            # when it disagrees with itself. Never truncate it.
            for line in textwrap.wrap(outcome.result.reasoning, width=88):
                print(f"     {line}")
            if outcome.result.customer_words:
                print("     heard:")
                for line in textwrap.wrap(outcome.result.customer_words, width=84):
                    print(f'       "{line}"')
            if outcome.result.address:
                print(f"     address: {outcome.result.address}")
            if outcome.geocode is not None:
                print(f"     geocode: {outcome.geocode.line()}")
            if outcome.result.needs_human:
                print(f"     ESCALATED — confidence {outcome.result.confidence:.2f}, "
                      "a person should look at this before it is dispatched")

        if args.ablate and args.image and outcome.result and args.repeat > 1:
            report = intake_mod.ablation_study(
                text=text, image=args.image, audio=args.audio, repeat=args.repeat
            )
            print()
            for line in report.lines():
                print("     " + line)
            print()
            print("     A field only counts as moved by the photo if it moves more")
            print("     often than the model moves it on its own.")

        elif args.ablate and args.image and outcome.result:
            blind = intake_mod.receive(text=text, audio=args.audio)
            if blind.result:
                changes = intake_mod.disagreement(outcome.result, blind.result)
                print()
                print(f"     without the photo: {blind.summary_line()}")
                for line in textwrap.wrap(blind.result.reasoning, width=84):
                    print(f"       {line}")
                print()
                if changes:
                    print(f"     the photo changed: {', '.join(changes)}")
                else:
                    print("     the photo changed nothing — it is decoration here")
                drop = blind.result.confidence - outcome.result.confidence
                if drop > 0.25:
                    print(
                        f"     note: confidence fell {blind.result.confidence:.2f} -> "
                        f"{outcome.result.confidence:.2f} when the photo was added. "
                        "That usually means the inputs describe different problems, "
                        "not that the photo was unhelpful."
                    )
        print()

    return 0


def main(argv: list[str] | None = None) -> int:
    # Load .env before anything reads its configuration. Every module uses
    # os.getenv, so this has to happen first or a missing key degrades silently
    # into a fallback that looks like it is working.
    from fieldpilot.config import load_env, quiet_known_noise

    load_env()
    quiet_known_noise()

    parser = argparse.ArgumentParser(prog="fieldpilot")
    # Not required, because `fieldpilot --config` is a legitimate invocation
    # with no subcommand. The missing-command case is handled below, where it
    # can print help instead of an argparse error about a positional.
    sub = parser.add_subparsers(dest="command")

    for name, handler in (
        ("compare", cmd_compare),
        ("plan", cmd_plan),
        ("run", cmd_run),
        ("triage", cmd_triage),
        ("watch", cmd_watch),
        ("intake", cmd_intake),
    ):
        p = sub.add_parser(name)
        p.add_argument("--seed", type=int, default=42)
        p.add_argument(
            "--seeds",
            type=int,
            default=1,
            help="watch only: repeat the experiment over N consecutive seeds and "
                 "average, because one seed cannot support a claim",
        )
        # Triage only means something when the day is oversubscribed enough
        # that jobs have to be sacrificed. At 26 orders everything that matters
        # fits and every method scores identically.
        p.add_argument("--orders", type=int, default=48 if name == "triage" else 26)
        p.add_argument("--time-limit", type=int, default=5)
        p.add_argument(
            "--solution-limit",
            type=int,
            default=None,
            help="stop after N improving solutions instead of after --time-limit "
                 "seconds. Wall-clock solving is not reproducible; this is. Use it "
                 "for anything recorded, published, or compared.",
        )
        p.add_argument("--text", default="", help="intake only: the customer's message")
        p.add_argument("--image", default=None, help="intake only: photo of the equipment")
        p.add_argument("--audio", default=None, help="intake only: voice note")
        p.add_argument("--sample", type=int, default=0, help="intake only: use sample request N")
        p.add_argument("--routes", action="store_true", help="print each technician's day")
        p.add_argument("--verbose", action="store_true", help="print every event, not just actionable ones")
        p.add_argument(
            "--rules-only",
            action="store_true",
            help="watch only: skip the model entirely, for a free offline run",
        )
        p.add_argument(
            "--repeat",
            type=int,
            default=1,
            help="intake only: with --ablate, run the paired comparison N times and "
                 "report how often the photo moved each field against how often the "
                 "model moved it unprompted (costs 2N calls)",
        )
        p.add_argument(
            "--ablate",
            action="store_true",
            help="triage only: score the backlog twice, with and without the notes, "
                 "to isolate what the notes actually changed (costs one extra call)",
        )
        p.set_defaults(func=handler)

    parser.add_argument(
        "--config",
        action="store_true",
        help="print how this process is configured and exit",
    )

    args = parser.parse_args(argv)

    if getattr(args, "config", False):
        from fieldpilot.config import describe

        print(describe())
        return 0

    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
