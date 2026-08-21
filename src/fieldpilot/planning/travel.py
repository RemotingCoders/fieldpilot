"""Travel time between locations.

Two implementations behind one interface. The offline estimator keeps the whole
system runnable with no API key and no cost, which is what makes it possible to
rehearse the demo scenario dozens of times. The Routes API matrix is fetched
once per city and cached, so real travel times cost a handful of requests rather
than one per re-plan.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fieldpilot.domain.models import Location

# Average city driving speed, km/h. Deliberately pessimistic: field technicians
# park, walk, and carry equipment.
DEFAULT_SPEED_KMH = 22.0

# Fixed overhead per visit for parking and reaching the door, in minutes.
ACCESS_OVERHEAD_MIN = 6


class TravelMatrix:
    """Travel times in whole minutes between an ordered list of locations."""

    def __init__(self, minutes: list[list[int]]) -> None:
        self._minutes = minutes

    def __call__(self, i: int, j: int) -> int:
        return self._minutes[i][j]

    @property
    def size(self) -> int:
        return len(self._minutes)

    def to_json(self) -> str:
        return json.dumps(self._minutes)

    @classmethod
    def estimated(
        cls,
        locations: list[Location],
        speed_kmh: float = DEFAULT_SPEED_KMH,
        access_overhead_min: int = ACCESS_OVERHEAD_MIN,
    ) -> "TravelMatrix":
        """Straight-line distance converted to driving minutes.

        Good enough to plan against and completely free. The absolute numbers
        are optimistic versus real road networks, but both the baseline and the
        agent are measured with the same estimator, so the comparison holds.
        """
        n = len(locations)
        minutes = [[0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                km = locations[i].haversine_km(locations[j])
                minutes[i][j] = int(round(km / speed_kmh * 60)) + access_overhead_min
        return cls(minutes)

    @classmethod
    def from_cache(cls, path: str | Path) -> "TravelMatrix | None":
        """Load a previously fetched matrix, or None when there is no cache."""
        p = Path(path)
        if not p.exists():
            return None
        return cls(json.loads(p.read_text()))

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json())


def build_matrix(locations: list[Location], cache_path: str | Path | None = None) -> TravelMatrix:
    """Return a travel matrix, preferring cache, then Routes API, then estimate.

    The Routes API path is only taken when FIELDPILOT_MAPS_API_KEY is set. That
    keeps `make demo` free by default and makes the paid path an explicit
    opt-in rather than an accident.
    """
    if cache_path:
        cached = TravelMatrix.from_cache(cache_path)
        if cached and cached.size == len(locations):
            return cached

    if os.getenv("FIELDPILOT_MAPS_API_KEY"):
        # Deliberately not implemented on day 1. The estimator is the default
        # so nothing about the demo depends on a billed API being reachable.
        raise NotImplementedError(
            "Routes API matrix is not wired yet. Unset FIELDPILOT_MAPS_API_KEY "
            "to use the offline estimator."
        )

    matrix = TravelMatrix.estimated(locations)
    if cache_path:
        matrix.save(cache_path)
    return matrix
