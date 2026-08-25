"""The HTTP face of the dispatcher, for Cloud Run.

This file exists because the Dockerfile promised it. The image's CMD has
pointed at `fieldpilot.api.main:app` since day one, and until today there was
no such module — the container built cleanly and then died on startup, which
is the worst kind of deploy bug: invisible until deploy day.

The service is deliberately thin. Every piece of judgement lives in the
modules the CLI already exercises — intake, taxonomy, solver, escalation — and
this layer only translates HTTP into calls to them. Nothing here is allowed to
have an opinion; if a behaviour cannot be reached from the CLI, it does not
belong in the API either, because then the demo and the deployment would be
two different systems.

**One deliberate mechanical choice.** Endpoints are plain `def`, not
`async def`. The agent modules wrap their model calls in `asyncio.run()`,
which raises `RuntimeError` if called from a running event loop — so an
`async def` endpoint would crash the moment it touched intake or triage.
FastAPI runs sync endpoints in a worker thread, where there is no running
loop and `asyncio.run()` is legal. Declaring these `async` would read as more
modern and would break everything.

**State.** There is none. The geocode cache and the duration memory are disk
files that Cloud Run treats as per-instance and disposable, and both are
designed to be worthless-safe: a cold cache re-pays the Geocoding API, an
empty memory returns factor 1.0 and the plan falls back to nominal estimates.
The service can scale to zero, scale out, or be replaced mid-day without any
instance holding anything another instance needs.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from fieldpilot.config import describe, load_env

load_env()

from fastapi import FastAPI  # noqa: E402

app = FastAPI(
    title="FieldPilot",
    description=(
        "The language model does not build the route. It writes the cost "
        "function the solver optimises."
    ),
    version="0.1.0",
)


@app.get("/health")
@app.get("/healthz")
def health() -> dict:
    """Liveness plus configuration, secrets reported as present or absent.

    Served under two names for one annoying reason: `/healthz` is a reserved
    URL on Cloud Run — Google's frontend intercepts it and returns 404 without
    the request ever reaching this container. It worked locally, 404ed in
    production, and the other routes were fine, which is exactly the shape of
    bug that eats an evening. `/health` is the public name; `/healthz` stays
    because it works everywhere else and muscle memory types it.
    """
    return {"ok": True, "config": describe().split("\n")}


class IntakeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.post("/intake")
def intake(request: IntakeRequest) -> dict:
    """One customer message in, one dispatchable work order out.

    The response separates what the model said from what will actually be
    dispatched, because they are allowed to differ — the taxonomy overrides
    severity downward-never and skills always — and hiding that difference
    would hide the architecture.
    """
    from fieldpilot.agents import escalation as escalation_mod
    from fieldpilot.agents import intake as intake_mod
    from fieldpilot.domain.models import Location

    outcome = intake_mod.receive(text=request.text)
    queue = escalation_mod.Queue.build(escalation_mod.from_intake(outcome))

    if outcome.result is None:
        return {
            "ok": False,
            "error": outcome.error,
            "escalations": [e.line() for e in queue.items],
        }

    result = outcome.result
    severity, severity_note = result.settled_severity()
    skills, skills_note = result.dispatch_characteristics()

    order = result.to_work_order(
        work_order_id="wo-api",
        account_id="acc-api",
        location=(
            outcome.geocode.location
            if outcome.geocode is not None and outcome.geocode.usable
            else Location(lat=0.0, lon=0.0, address=result.address)
        ),
    )

    return {
        "ok": True,
        "model_said": {
            "incident_type_id": result.incident_type_id,
            "severity": result.severity.value,
            "skills": result.required_characteristics,
            "confidence": result.confidence,
            "reasoning": result.reasoning,
        },
        "will_dispatch": {
            "incident_type_id": order.incident_type_id,
            "severity": severity.value,
            "skills": skills,
            "duration_min": order.duration_min,
            "window": [order.window_start_min, order.window_end_min],
            "overrides": [n for n in (severity_note, skills_note) if n],
        },
        "geocode": outcome.geocode.line() if outcome.geocode else None,
        "dispatchable": outcome.geocode.usable if outcome.geocode else False,
        "needs_human": result.needs_human,
        "escalations": [e.line() for e in queue.items],
        "estimated_usd": round(outcome.estimated_usd, 6),
    }


@app.get("/compare")
def compare(seed: int = 42, orders: int = 26, solution_limit: int = 30) -> dict:
    """Both planners on the same day. The demo, as an endpoint."""
    from fieldpilot.agents import rules_triage
    from fieldpilot.domain.models import Location
    from fieldpilot.planning import baseline, metrics, solver
    from fieldpilot.planning.travel import TravelMatrix
    from fieldpilot.sim import scenario as scenario_mod

    orders = max(4, min(orders, 80))
    scn = scenario_mod.build(seed=seed, n_orders=orders)
    rules_triage.apply(scn.work_orders, scn.accounts)

    locations: list[Location] = [o.location for o in scn.work_orders]
    locations += [r.start_location for r in scn.resources]
    shared = TravelMatrix.estimated(locations + [locations[0]])

    naive = baseline.dispatch(scn.work_orders, scn.resources, shared)
    optimised = solver.solve(
        scn.work_orders, scn.resources, shared,
        time_limit_s=10, solution_limit=solution_limit,
    )

    def row(plan, label):
        scored = metrics.score(plan, scn.work_orders, scn.accounts)
        return {
            "planner": label,
            "served": scored.orders_served,
            "of": scored.orders_total,
            "weighted_coverage_pct": round(scored.weighted_coverage_pct, 1),
            "true_value_pct": round(scored.true_value_pct, 1),
            "travel_min": scored.travel_minutes,
        }

    return {
        "seed": scn.seed,
        "reproducible": optimised.reproducible,
        "results": [row(naive, "fifo-nearest"), row(optimised, "ortools")],
    }
