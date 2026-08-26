"""Command line entry points.

`python -m fieldpilot.cli compare` runs the same simulated day through both
planners and prints the difference. This is the command the demo video shows.
"""

from __future__ import annotations

import argparse
import sys
import textwrap

from fieldpilot.agents import rules_triage
from fieldpilot.agents.monitor import RulesMonitor
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



def cmd_memory(args: argparse.Namespace) -> int:
    """Does remembering how long work really takes actually help?

    Three arms on identical days: a planner with no memory, one that learns
    from completed visits, and one handed the simulator's true per-technician
    speeds. The third is not a competitor — it is the ceiling, and without it
    the other two numbers cannot be read. It is the same device used for
    triage, for the same reason: knowing one method beat another is useless
    without knowing whether the gap left was worth chasing.
    """
    from statistics import fmean, pstdev

    from fieldpilot.memory.durations import DurationMemory
    from fieldpilot.sim import report as report_mod
    from fieldpilot.sim.engine import Simulator
    from fieldpilot.sim.orchestrator import run_day

    true_speed = {rid: f for rid, _, _, f in scenario_mod.CREW}

    class _Oracle:
        def factor(self, resource_id: str, incident_type_id: str) -> float:
            return true_speed.get(resource_id, 1.0)

    def run_arm(seed: int, arm: str) -> list[float]:
        memory = (
            DurationMemory() if arm == "learned"
            else _Oracle() if arm == "oracle"
            else None
        )
        daily = []
        for day in range(args.days):
            scn = scenario_mod.build(seed=seed * 100 + day)
            rules_triage.apply(scn.work_orders, scn.accounts)
            sim = Simulator(scn, seed=seed * 100 + day)
            sim.load_plan(solver.solve(
                scn.work_orders, scn.resources,
                time_limit_s=args.time_limit,
                solution_limit=args.solution_limit,
                memory=memory,
            ))
            run_day(
                sim, RulesMonitor(),
                time_limit_s=args.time_limit,
                solution_limit=args.solution_limit,
            )
            daily.append(report_mod.build(sim, arm).true_value_pct)
            if arm == "learned":
                memory.observe_visits(
                    sim.executed,
                    {o.work_order_id: o for o in scn.work_orders},
                    {r.resource_id: r for r in scn.resources},
                )
        return daily

    seeds = list(range(args.seed, args.seed + args.seeds))
    arms = ("none", "learned", "oracle")
    results: dict[str, list[float]] = {a: [] for a in arms}

    print()
    print(f"{args.days} consecutive days per seed, {len(seeds)} seeds. "
          "Day 1 is identical in every arm — memory has seen nothing yet — "
          "so it is excluded.")
    print("-" * 78)

    for seed in seeds:
        for arm in arms:
            results[arm].append(fmean(run_arm(seed, arm)[1:]))
        print(f"seed {seed}:  " + "   ".join(
            f"{a} {results[a][-1]:5.1f}%" for a in arms
        ), flush=True)

    print("-" * 78)
    baseline = results["none"]
    for arm in ("learned", "oracle"):
        deltas = [b - a for a, b in zip(baseline, results[arm])]
        print(
            f"{arm:<8} paired {fmean(deltas):+5.1f} pts  "
            f"spread ±{pstdev(deltas):.1f}  "
            f"better on {sum(1 for d in deltas if d > 0)}/{len(deltas)} seeds"
        )
    print()
    print("Read the oracle row first. It is the most this knowledge can be worth,")
    print("and if it is inside its own spread then nothing below it can be trusted")
    print("to be real either — including the learned row.")
    return 0



def cmd_comms(args: argparse.Namespace) -> int:
    """Run a day, then tell the customers whose day it changed.

    The number worth watching is not the prose. It is how many drafts had to be
    thrown away, and why. A model writing customer notifications is only safe
    if something downstream is willing to refuse it, and this reports how often
    that refusal fires.
    """
    from fieldpilot.agents import comms, escalation
    from fieldpilot.agents.comms import Notification, NotificationKind
    from fieldpilot.sim.engine import Simulator
    from fieldpilot.sim.orchestrator import run_day

    scn = scenario_mod.build(seed=args.seed, n_orders=args.orders)
    rules_triage.apply(scn.work_orders, scn.accounts)
    sim = Simulator(scn, seed=args.seed)
    plan = solver.solve(
        scn.work_orders, scn.resources,
        time_limit_s=args.time_limit, solution_limit=args.solution_limit,
    )
    sim.load_plan(plan)
    log = run_day(
        sim, RulesMonitor(),
        time_limit_s=args.time_limit, solution_limit=args.solution_limit,
    )

    booked = {b.work_order_id: b for b in plan.bookings}
    by_id = {o.work_order_id: o for o in scn.work_orders}
    resources = {r.resource_id: r for r in scn.resources}
    served = {v.work_order_id for v in sim.executed}

    notifications: list[Notification] = []
    for visit in sim.executed:
        original = booked.get(visit.work_order_id)
        if original is None:
            continue
        drift = visit.arrived_min - original.arrival_min
        if drift < 20:
            continue
        order = by_id.get(visit.work_order_id)
        account = scn.accounts.get(order.account_id) if order else None
        notifications.append(Notification(
            kind=NotificationKind.RUNNING_LATE,
            customer_name=getattr(account, "name", "Customer"),
            work_order_id=visit.work_order_id,
            technician_name=getattr(resources.get(visit.resource_id), "name", ""),
            original_time=_hhmm(original.arrival_min),
            new_time=_hhmm(visit.arrived_min),
            reason="An earlier visit ran longer than expected.",
            options=[],
        ))

    for order in scn.work_orders:
        if order.work_order_id in served or order.work_order_id not in booked:
            continue
        account = scn.accounts.get(order.account_id)
        notifications.append(Notification(
            kind=NotificationKind.NOT_TODAY,
            customer_name=getattr(account, "name", "Customer"),
            work_order_id=order.work_order_id,
            original_time=_hhmm(booked[order.work_order_id].arrival_min),
            reason="An emergency took priority today.",
            options=["We will call in the morning to book the next slot."],
        ))

    notifications = notifications[: args.limit]

    print()
    print(f"Seed {scn.seed} — {len(log.replans)} re-plans, "
          f"{len(notifications)} customers to notify")
    print("-" * 78)

    drafts = []
    if args.templates_only:
        print("(--templates-only: no model calls, the deterministic floor only)")
        print()
        for note in notifications:
            print(f"  {note.work_order_id}  {note.template()}")
        print()
        return 0

    for note in notifications:
        result = comms.draft(note)
        drafts.append(result)
        print(f"  {note.work_order_id}  {result.line()}")
    print("-" * 78)

    rejected = [d for d in drafts if d.violations]
    cost = sum(d.estimated_usd for d in drafts)
    print(f"drafted {len(drafts)}   rejected {len(rejected)}   ~${cost:.4f}")
    if rejected:
        reasons: dict[str, int] = {}
        for d in rejected:
            for v in d.violations:
                key = v.split(":")[0]
                reasons[key] = reasons.get(key, 0) + 1
        print("why: " + ", ".join(f"{k} x{n}" for k, n in sorted(reasons.items())))
        print("Every rejected draft was replaced by the template, so no customer")
        print("received any of it. The count is an operations signal, not an incident.")

    unserved = [by_id[i] for i in plan.unserved_work_order_ids if i in by_id]
    queue = escalation.Queue.build(
        escalation.from_unserved(unserved, scn.accounts),
        escalation.from_visits(sim.executed, scn.accounts),
        escalation.from_comms(drafts),
    )
    print()
    print("Needs a person before tomorrow")
    print("-" * 78)
    for line in queue.lines():
        print(line)
    print()
    return 0



DEMO_REQUEST = (
    "URGENTE se esta filtrando agua del termotanque y esta llegando abajo del "
    "tablero de luz del pasillo. corte la llave general por las dudas. "
    "Estamos en Av. Cabildo 2340 piso 3, CABA. Hay alguien todo el dia."
)


def cmd_demo(args: argparse.Namespace) -> int:
    """The whole system, one command, one unbroken take.

    Built for the submission video, whose rules require an unedited live
    execution. Everything here is the same code every other command runs —
    nothing demo-only decides anything — and the pacing flag only inserts
    pauses between sections so a viewer can read; it never changes a result.

    `--offline` is the rehearsal switch: no model calls, no cost, rules and
    templates end to end. Rehearse offline as many times as it takes, film
    online once.
    """
    import time as time_mod

    # The demo is, by definition, a recorded and published run — exactly the
    # kind the --solution-limit help text says must never solve against the
    # wall clock. Without this default, the filmed take printed
    # `reproducible: False` under a narration about reproducibility. Every
    # other command still defaults to the clock and says so.
    if args.solution_limit is None:
        args.solution_limit = 30

    from fieldpilot.agents import comms as comms_mod
    from fieldpilot.agents import escalation as escalation_mod
    from fieldpilot.agents import intake as intake_mod
    from fieldpilot.agents import triage as llm_triage
    from fieldpilot.agents.comms import Notification, NotificationKind
    from fieldpilot.agents.monitor import RulesMonitor
    from fieldpilot.sim import report as report_mod
    from fieldpilot.sim.engine import Simulator
    from fieldpilot.sim.orchestrator import run_day

    total_usd = 0.0

    def pause() -> None:
        if args.pace > 0:
            time_mod.sleep(args.pace)

    def section(title: str) -> None:
        pause()
        print()
        print("─" * 78)
        print(f"  {title}")
        print("─" * 78)

    from fieldpilot.config import describe

    print()
    print("FIELDPILOT — the model does not build the route; it writes the cost")
    print("function the solver optimises.")
    print()
    for line in describe().split("\n"):
        print(f"  {line}")
    if args.offline:
        print("  MODE: offline rehearsal — no model calls, rules and templates only")

    # ------------------------------------------------------------------
    # 1. A customer calls
    # ------------------------------------------------------------------
    if not args.offline:
        section("1 · A CUSTOMER CALLS — Gemini reads it, the taxonomy rules on it")
        print()
        print(f'  "{DEMO_REQUEST}"')
        print()
        outcome = intake_mod.receive(text=DEMO_REQUEST)
        total_usd += outcome.estimated_usd
        print("  -> " + outcome.summary_line())
        if outcome.result is not None:
            for line in textwrap.wrap(outcome.result.reasoning, width=72):
                print(f"     {line}")
            if outcome.geocode is not None:
                print(f"     geocode: {outcome.geocode.line()}")
            severity, note = outcome.result.settled_severity()
            if note:
                print(f"     OVERRIDE: {note}")
        for item in escalation_mod.from_intake(outcome):
            print(f"     {item.line()}")

    # ------------------------------------------------------------------
    # 2. The morning backlog
    # ------------------------------------------------------------------
    section(f"2 · THE MORNING BACKLOG — {args.orders} work orders, one triage call")
    scn = scenario_mod.build(seed=args.seed, n_orders=args.orders)
    if args.offline:
        rules_triage.apply(scn.work_orders, scn.accounts)
        print()
        print("  scored by the rules engine (offline)")
    else:
        result = llm_triage.apply(scn.work_orders, scn.accounts)
        total_usd += result.estimated_usd
        print()
        print(f"  {result.summary_line()}")

    ranked = sorted(scn.work_orders, key=lambda o: o.penalty_cost, reverse=True)
    print()
    print("  what it costs to leave a job undone today — top of the list:")
    for order in ranked[:3]:
        note = f'  note: "{order.notes[:44]}..."' if order.notes else ""
        print(f"    {order.work_order_id}  penalty {order.penalty_cost:>7,}  "
              f"{order.incident_type_id:<22}{note}")

    # ------------------------------------------------------------------
    # 3. The plan
    # ------------------------------------------------------------------
    section("3 · THE PLAN — OR-Tools minimises travel plus the cost of what it drops")
    locations = [o.location for o in scn.work_orders]
    locations += [r.start_location for r in scn.resources]
    shared = TravelMatrix.estimated(locations + [locations[0]])

    naive = baseline.dispatch(scn.work_orders, scn.resources, shared)
    plan = solver.solve(
        scn.work_orders, scn.resources, shared,
        time_limit_s=args.time_limit, solution_limit=args.solution_limit,
    )
    m_naive = metrics.score(naive, scn.work_orders, scn.accounts)
    m_plan = metrics.score(plan, scn.work_orders, scn.accounts)
    print()
    print(f"  {m_naive.summary_line()}")
    print(f"  {m_plan.summary_line()}")
    print(f"  reproducible: {plan.reproducible} "
          "(solution-limited, so this exact plan repeats on any machine)")

    # ------------------------------------------------------------------
    # 4. The day happens anyway
    # ------------------------------------------------------------------
    section("4 · THE DAY HAPPENS ANYWAY — breakdowns, overruns, emergencies, re-plans")
    sim = Simulator(scn, seed=args.seed)
    sim.load_plan(plan)
    log = run_day(
        sim, RulesMonitor(),
        time_limit_s=args.time_limit, solution_limit=args.solution_limit,
    )
    print()
    lines = log.timeline()
    shown = lines if len(lines) <= 14 else lines[:14]
    for line in shown:
        pause() if args.pace > 0 else None
        print(f"  {line}")
    if len(lines) > len(shown):
        print(f"  ... {len(lines) - len(shown)} more events")
    print()
    print(f"  re-plans: {log.replan_count}   disruptions absorbed: {log.absorbed}")

    # ------------------------------------------------------------------
    # 5. Telling the customers
    # ------------------------------------------------------------------
    section("5 · TELLING THE CUSTOMERS — drafted by Gemini, verified before sending")
    booked = {b.work_order_id: b for b in plan.bookings}
    late = [
        v for v in sim.executed
        if v.work_order_id in booked
        and v.arrived_min - booked[v.work_order_id].arrival_min >= 20
    ][:2]
    drafts = []
    print()
    for visit in late:
        order = next(o for o in scn.work_orders if o.work_order_id == visit.work_order_id)
        account = scn.accounts.get(order.account_id)
        note = Notification(
            kind=NotificationKind.RUNNING_LATE,
            customer_name=getattr(account, "name", "Customer"),
            work_order_id=visit.work_order_id,
            original_time=_hhmm(booked[visit.work_order_id].arrival_min),
            new_time=_hhmm(visit.arrived_min),
            reason="An earlier visit ran long.",
        )
        if args.offline:
            print(f"  [template] {note.template()}")
        else:
            draft = comms_mod.draft(note)
            drafts.append(draft)
            total_usd += draft.estimated_usd
            print(f"  {draft.line()}")
    if not late:
        print("  nobody drifted more than 20 minutes today — no messages owed")

    # ------------------------------------------------------------------
    # 6. What needs a person
    # ------------------------------------------------------------------
    section("6 · WHAT NEEDS A PERSON TONIGHT — the queue no model can talk out of firing")
    # End-of-day state, not the 8am plan. The first version of this act read
    # `plan.unserved_work_order_ids` off the morning plan — so an emergency
    # that arrived at 09:52 and was never served could not appear, and the
    # queue said "nothing needs a person tonight" under a scorecard showing an
    # unserved safety call. The queue's one job is to catch what everything
    # upstream got wrong; it was reading the version of the day from before
    # anything had gone wrong. "Unserved" here means the same thing it means
    # on the scorecard: known by end of day, not completed, not canceled — a
    # gas call where the technician arrived and nobody answered is *attended*
    # in the log and still a gas call nobody resolved.
    from fieldpilot.sim.engine import Outcome
    from fieldpilot.sim.events import EventKind

    completed_ids = {
        v.work_order_id for v in sim.executed if v.outcome == Outcome.COMPLETED
    }
    canceled_ids = {
        e.work_order_id for e in sim.events if e.kind == EventKind.ORDER_CANCELED
    }
    known = list(scn.work_orders) + [
        o for arrives, o, _ in sim._urgent if arrives <= sim.now_min
    ]
    unserved = [
        o for o in known
        if o.work_order_id not in completed_ids
        and o.work_order_id not in canceled_ids
    ]
    queue = escalation_mod.Queue.build(
        escalation_mod.from_unserved(unserved, sim.accounts),
        escalation_mod.from_visits(sim.executed, sim.accounts),
        escalation_mod.from_comms(drafts),
    )
    print()
    for line in queue.lines():
        print(f"  {line}")

    # ------------------------------------------------------------------
    # 7. Scorecard
    # ------------------------------------------------------------------
    section("7 · SCORECARD")
    report = report_mod.build(sim, "fieldpilot")
    print()
    print(f"  {report.summary_line()}")
    print()
    if not args.offline:
        print(f"  model spend for everything you just watched: ~${total_usd:.4f}")
    print("  every number above is reproducible: same seed, same plan, same day.")
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
        ("memory", cmd_memory),
        ("comms", cmd_comms),
        ("demo", cmd_demo),
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
        p.add_argument(
            "--days",
            type=int,
            default=12,
            help="memory only: consecutive days per seed. Memory needs days the "
                 "way the triage experiment needs seeds.",
        )
        p.add_argument(
            "--pace", type=float, default=0.0,
            help="demo only: seconds to pause between sections so a viewer can "
                 "read. Changes nothing but timing.",
        )
        p.add_argument(
            "--offline", action="store_true",
            help="demo only: rehearsal mode — no model calls, no cost, rules "
                 "and templates end to end",
        )
        p.add_argument(
            "--limit", type=int, default=6,
            help="comms only: how many notifications to draft",
        )
        p.add_argument(
            "--templates-only", action="store_true",
            help="comms only: skip the model and show the deterministic floor",
        )
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
