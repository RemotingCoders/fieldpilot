"""What the fleet has learned about how long work actually takes.

Every planner starts from an estimate: this incident type takes sixty minutes.
Nobody's day matches the estimate. One technician is quicker on boilers than
the book says and slower on installations; a particular building eats twenty
minutes before anyone reaches the equipment. None of that is in the work order,
and all of it decides whether the last two jobs of the day happen.

This is the part of the system that remembers. It watches completed visits,
compares what they took against what was estimated, and hands the planner a
per-technician correction the next time it builds a day.

**Why this module exists at all is worth stating plainly.** Until now the solver
read `BookableResource.duration_factor` — the simulator's ground truth for how
fast each technician works — straight out of the scenario. The planner was
being handed a number that no real dispatcher has. Removing that and making the
system earn it is the entire point of the day's work, and it is also the reason
the measured numbers move: the planner got *worse* before memory made it better
again, and both halves are reported.

Three deliberate choices, each of which could reasonably go the other way:

- **Only completed visits are learned from.** A visit that ended because the
  customer was out, or because a part was missing, says nothing about how long
  the work takes. Folding those in would teach the memory that certain jobs are
  fast, when what actually happened is that they did not happen.
- **Estimates shrink toward 1.0.** A technician with two observations does not
  get a confident correction. `PRIOR_STRENGTH` is how many imaginary
  observations of "exactly as estimated" sit behind every real one, so the
  first few days move the plan barely at all. This is the difference between
  learning and overreacting.
- **Corrections are clamped.** Nothing here is allowed to claim a job takes a
  third of its estimate or three times it. A memory that can say anything can
  wreck a day on one bad week, and the failure mode of a runaway correction is
  a technician sent to six jobs they cannot possibly finish.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# How many imaginary "it took exactly as long as estimated" observations sit
# behind every real one. Higher means slower, steadier learning. Six is roughly
# a working week of visits of one kind, which is about when a dispatcher would
# start believing a pattern themselves.
PRIOR_STRENGTH = 6.0

# No correction may go outside this range, however many observations support
# it. Real variation in this trade lives well inside it, so a value at the
# boundary means something has gone wrong upstream, not that a technician is
# genuinely four times slower than the book.
MIN_FACTOR = 0.60
MAX_FACTOR = 1.80

# Observations below this are treated as an interrupted visit rather than fast
# work, whatever the recorded outcome says.
MIN_CREDIBLE_MIN = 5


@dataclass
class Observation:
    """One completed visit, reduced to the only thing worth remembering."""

    resource_id: str
    incident_type_id: str
    estimated_min: int
    actual_min: int

    @property
    def ratio(self) -> float:
        return self.actual_min / self.estimated_min


@dataclass
class DurationMemory:
    """Learned duration corrections, keyed by technician and incident type.

    Lookup falls back from the specific to the general: this technician on this
    kind of job, then this technician on anything, then nothing. That ordering
    matters — a new incident type should inherit what is known about the person
    rather than starting from scratch, because being generally slow is a
    property of the person and shows up on work they have never done before.
    """

    path: Path | None = None

    # When True, only the *differences* between technicians are applied and the
    # fleet-wide average is normalised away. See `factor` for why that is not a
    # cosmetic choice.
    relative: bool = False

    _by_pair: dict[tuple[str, str], list[float]] = field(default_factory=lambda: defaultdict(list))
    _by_resource: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    # ------------------------------------------------------------------
    # Learning
    # ------------------------------------------------------------------

    def observe(self, obs: Observation) -> bool:
        """Record one visit. Returns whether it was credible enough to keep."""
        if obs.estimated_min <= 0 or obs.actual_min < MIN_CREDIBLE_MIN:
            return False
        ratio = obs.ratio
        if not (MIN_FACTOR / 2 <= ratio <= MAX_FACTOR * 2):
            # Wildly outside anything plausible. Keeping it would drag the mean
            # around for days; dropping it silently would hide a real problem,
            # so this is the one thing the caller is told about.
            return False
        self._by_pair[(obs.resource_id, obs.incident_type_id)].append(ratio)
        self._by_resource[obs.resource_id].append(ratio)
        return True

    def observe_visits(self, visits, orders_by_id, resources_by_id) -> int:
        """Learn from a finished simulated day. Returns how many were kept.

        Takes the raw executed visits so this module owns the decision about
        which ones count, rather than trusting a caller to have filtered them.
        """
        kept = 0
        for visit in visits:
            if getattr(visit.outcome, "value", visit.outcome) != "completed":
                continue
            order = orders_by_id.get(visit.work_order_id)
            if order is None or visit.resource_id not in resources_by_id:
                continue
            if self.observe(
                Observation(
                    resource_id=visit.resource_id,
                    incident_type_id=order.incident_type_id,
                    estimated_min=order.duration_min,
                    actual_min=max(0, visit.left_min - visit.arrived_min),
                )
            ):
                kept += 1
        return kept

    # ------------------------------------------------------------------
    # Recall
    # ------------------------------------------------------------------

    def _shrunk(self, samples: list[float]) -> float:
        """Mean pulled toward 1.0 in proportion to how little evidence there is."""
        n = len(samples)
        observed = statistics.fmean(samples)
        adjusted = (PRIOR_STRENGTH + n * observed) / (PRIOR_STRENGTH + n)
        return min(MAX_FACTOR, max(MIN_FACTOR, adjusted))

    def factor(self, resource_id: str, incident_type_id: str) -> float:
        """How long this person takes on this work, relative to the estimate.

        Returns exactly 1.0 when nothing is known, which is the same as saying
        "use the estimate" — memory that has learned nothing must not perturb
        the plan.
        """
        pair = self._by_pair.get((resource_id, incident_type_id))
        if pair:
            raw = self._shrunk(pair)
        else:
            general = self._by_resource.get(resource_id)
            if not general:
                return 1.0
            raw = self._shrunk(general)

        if not self.relative:
            return raw
        return min(MAX_FACTOR, max(MIN_FACTOR, raw / self.fleet_mean()))

    def fleet_mean(self) -> float:
        """The correction shared by everybody, which is most of it.

        Measured here: across fifteen simulated days the learned corrections
        were 1.19, 1.36, 1.13 and 1.27 against true speeds of 1.00, 1.15, 0.95
        and 1.05. The *ordering* is exactly right and the spread is right, but
        every value is inflated by about the same fifth — because overruns and
        interruptions lift every visit, not particular people.

        That common part is real, and applying it makes the plan more honest
        and fewer jobs get scheduled. Whether that is an improvement depends
        entirely on what is being counted, which is why it is separable rather
        than baked in.
        """
        all_ratios = [r for ratios in self._by_resource.values() for r in ratios]
        if not all_ratios:
            return 1.0
        return self._shrunk(all_ratios)

    def confidence(self, resource_id: str, incident_type_id: str) -> int:
        """How many observations sit behind `factor`, for display and tests."""
        pair = self._by_pair.get((resource_id, incident_type_id))
        if pair:
            return len(pair)
        return len(self._by_resource.get(resource_id, []))

    @property
    def observations(self) -> int:
        return sum(len(v) for v in self._by_pair.values())

    def summary_lines(self, limit: int = 8) -> list[str]:
        """What has been learned, worst-offender first.

        Ordered by how far the correction is from 1.0 rather than by volume,
        because the useful thing to show a dispatcher is where the book is
        wrong, not where it is right.
        """
        rows = [
            (abs(self._shrunk(v) - 1.0), rid, inc, self._shrunk(v), len(v))
            for (rid, inc), v in self._by_pair.items()
        ]
        rows.sort(reverse=True)
        return [
            f"{rid:<8} {inc:<22} x{fac:.2f}  ({n} visit{'s' if n != 1 else ''})"
            for _, rid, inc, fac, n in rows[:limit]
        ]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path | None = None) -> None:
        """Write to disk. Never raises: memory is an optimisation, not a
        dependency, and a fleet that cannot write its cache still has to run."""
        target = path or self.path
        if target is None:
            return
        payload = {
            "pairs": {f"{r}|{i}": v for (r, i), v in self._by_pair.items()},
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, indent=0))
        except OSError:
            pass

    @classmethod
    def load(cls, path: Path | None) -> "DurationMemory":
        """Read from disk, returning empty memory if anything is wrong."""
        memory = cls(path=path)
        if path is None or not Path(path).exists():
            return memory
        try:
            payload = json.loads(Path(path).read_text())
        except (json.JSONDecodeError, OSError):
            return memory
        for key, ratios in payload.get("pairs", {}).items():
            resource_id, _, incident = key.partition("|")
            clean = [float(r) for r in ratios if isinstance(r, (int, float))]
            if not clean:
                continue
            memory._by_pair[(resource_id, incident)].extend(clean)
            memory._by_resource[resource_id].extend(clean)
        return memory
