# FieldPilot

**An autonomous dispatch agent for field service teams.**
Built for the All Things Agentic Hackathon — track: **The Taskmaster**.

---

## Why this exists

I have spent years implementing Dynamics 365 Field Service for clients. The
scheduling engine works. Resource Scheduling Optimization does exactly what it
says: it respects skills, territories, time windows and priorities, and it
returns a mathematically sound plan.

And every single day, dispatchers override it by hand.

Not because the optimiser is wrong. Because the optimiser is answering a
question that was already settled by the time it ran. Someone decided which
jobs matter, how long they will really take, and what to do when the morning
falls apart — and that someone was a person with a phone, reading a WhatsApp
voice note from a building manager and deciding it sounded serious.

FieldPilot is an attempt to automate *that* part.

## The idea in one line

> The language model does not build the route. The language model writes the
> cost function that the solver optimises.

Everything follows from that split:

- **Judgement is a model problem.** Reading a voice note, looking at a photo of
  a leaking boiler, weighing a platinum SLA against a customer who has already
  been bumped twice — none of this is expressible as a constraint, and all of
  it is what actually determines who gets seen first.
- **Routing is a solver problem.** Once the costs exist, arranging visits is
  operations research with fifty years of theory behind it. There is no reason
  to ask a language model to do arithmetic it will do worse.

Triage emits a `penalty_cost` per work order. The solver minimises travel plus
the penalties of whatever it could not fit. That single integer is the whole
interface between the two halves.

## What the system does

| Component | Responsibility |
|---|---|
| **Intake** | Turns raw requests — text, voice notes, photos — into structured work orders with skills, durations, time windows and geocoded addresses. |
| **Triage** | Scores real urgency from SLA tier, waiting time, reschedule history and severity. Writes `penalty_cost` and a human-readable rationale. |
| **Plan engine** | OR-Tools VRPTW with time windows, certifications and drop penalties. Deterministic, no model in the loop. |
| **Disruption monitor** | Runs in the background. Decides whether a delay is worth re-planning for or should be absorbed. |
| **Comms** | Notifies affected customers; escalates to a human only what needs one. |
| **Memory bank** | Learns real service durations and customer patterns, feeding them back into future estimates. |

## Current status

Day 1 of 10. What is implemented and tested today:

- Complete domain model in Field Service vocabulary
- OR-Tools solver with certifications, time windows, per-technician pace and drop penalties
- A baseline dispatcher to measure against
- Reproducible scenario generator
- 32 tests covering scheduling invariants

Intake, the disruption monitor, comms and the memory bank are scaffolded and
land over the following days.

## Try it

```bash
pip install -e ".[dev]"
fieldpilot compare --seed 42 --routes
```

No API key and no cloud account required for this command: travel times fall
back to an offline estimator so the scenario can be run as many times as you
like at zero cost.

### Representative output

```
Scenario seed 42 — 26 work orders, 4 technicians
------------------------------------------------------------------------------
fifo-nearest   served 14/26 (54%)  weighted 66%  travel 347min  penalty 187163  safety missed 1
ortools        served 20/26 (77%)  weighted 85%  travel 388min  penalty 31471  safety missed 0
------------------------------------------------------------------------------
orders served       +6
travel per job      24.8 -> 19.4 min (+22%)
total travel        347 -> 388 min (higher because more jobs get done)
weighted coverage   +19.1 pts
safety jobs missed  1 -> 0
```

## How to read those numbers honestly

A few notes, because a comparison you cannot audit is worth nothing:

- **Total travel goes up, not down.** The optimiser drives further because it
  completes six more jobs. Travel *per completed job* is the number that
  matters to a fleet, and that improves by roughly 20%.
- **"Weighted coverage"** weights each job by the customer's contract tier. A
  planner that serves many trivial jobs and misses the platinum accounts scores
  well on raw counts and badly here. That is intentional.
- **The baseline is longest-waiting-first, assigned to the qualified technician
  who can arrive soonest.** It respects the same certifications, time windows
  and shifts. It is a simple heuristic, and it is what a spreadsheet and a
  phone actually produce — but it is a heuristic, and the gap would narrow
  against a tuned commercial scheduler.
- **The scenario is deliberately oversubscribed.** More work arrives than four
  technicians can finish. If everything fits, prioritisation is not a problem
  worth solving.

## Running the tests

```bash
pytest -q
```

The suite asserts the things that would put an uncertified technician on a gas
job, double-book someone, or silently lose a work order.

## Layout

```
src/fieldpilot/
  domain/      Field Service data model
  planning/    solver, baseline, travel times, metrics
  agents/      triage (rules + model), intake, disruption monitor
  sim/         reproducible scenario and event generation
  memory/      persistent learned facts
  api/         Cloud Run service
```

## Stack

Gemini 3.5 via Vertex AI · Google ADK · Cloud Run · Firestore · Pub/Sub ·
OR-Tools

## Licence

MIT
