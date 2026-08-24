"""Turning an address into a point the solver can route to.

The last link between intake and planning. Everything upstream can be perfect
and the visit still fails if the van goes to the wrong street.

Two backends behind one interface, the same shape as travel times:

- **Google Geocoding API**, used only when `FIELDPILOT_MAPS_API_KEY` is set.
  Results are cached to disk, because the same building calls twice and there
  is no reason to pay twice.
- **An offline stand-in** otherwise, which maps an address deterministically to
  a point inside the city. It is not a geocoder and does not pretend to be —
  it exists so the whole pipeline runs, and every result it produces is
  labelled `offline` so nothing downstream can mistake it for a real location.

One rule that matters operationally: **a geocode outside the service area is an
escalation, not a dispatch.** An ambiguous street name that resolves to the
wrong province looks like a perfectly valid coordinate, and the only sign
anything is wrong is that it is four hundred kilometres away.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from fieldpilot.domain.models import Location

# Greater Buenos Aires, generously drawn. A result outside this is either a
# different city or a bad match, and either way a person should look at it.
SERVICE_AREA = {
    "lat_min": -34.95,
    "lat_max": -34.35,
    "lon_min": -58.90,
    "lon_max": -58.15,
}

CACHE_PATH = Path(os.getenv("FIELDPILOT_GEOCODE_CACHE", ".cache/geocode.json"))
ENDPOINT = "https://maps.googleapis.com/maps/api/geocode/json"


@dataclass
class GeocodeResult:
    location: Location | None
    source: str                 # "maps", "cache", or "offline"
    formatted_address: str = ""
    precision: str = ""         # ROOFTOP, RANGE_INTERPOLATED, GEOMETRIC_CENTER, APPROXIMATE
    in_service_area: bool = True
    error: str | None = None

    @property
    def usable(self) -> bool:
        """Whether a van can be sent on this."""
        return (
            self.location is not None
            and self.error is None
            and self.in_service_area
            and self.source != "offline"
        )

    @property
    def vague(self) -> bool:
        """True when the match is a neighbourhood rather than a door.

        APPROXIMATE on a service call usually means the street number was
        missing or wrong, and a technician sent to the centroid of a
        neighbourhood has not been sent anywhere.
        """
        return self.precision in {"APPROXIMATE", "GEOMETRIC_CENTER"}

    def line(self) -> str:
        if self.error:
            return f"geocode failed ({self.source}): {self.error[:70]}"
        if self.location is None:
            return f"no location ({self.source})"
        flags = []
        if not self.in_service_area:
            flags.append("OUTSIDE SERVICE AREA")
        if self.vague:
            flags.append("imprecise")
        if self.source == "offline":
            flags.append("offline stand-in, not a real location")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        return (
            f"{self.location.lat:.5f},{self.location.lon:.5f}  "
            f"{self.precision or self.source}  {self.formatted_address[:52]}{suffix}"
        )


def _in_service_area(lat: float, lon: float) -> bool:
    return (
        SERVICE_AREA["lat_min"] <= lat <= SERVICE_AREA["lat_max"]
        and SERVICE_AREA["lon_min"] <= lon <= SERVICE_AREA["lon_max"]
    )


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache, indent=0))
    except OSError:
        pass  # a cache that cannot be written is a slow day, not a failure


def _offline(address: str) -> GeocodeResult:
    """A stable pretend point inside the city, derived from the address text.

    Deterministic so scenarios stay reproducible, and always labelled so it can
    never be mistaken for a geocode.
    """
    digest = hashlib.sha256(address.encode("utf-8")).digest()
    lat_frac = int.from_bytes(digest[0:4], "big") / 0xFFFFFFFF
    lon_frac = int.from_bytes(digest[4:8], "big") / 0xFFFFFFFF
    return GeocodeResult(
        location=Location(
            lat=-34.68 + lat_frac * 0.13,
            lon=-58.50 + lon_frac * 0.14,
            address=address,
        ),
        source="offline",
        formatted_address=address,
        precision="",
    )


def geocode(address: str, use_cache: bool = True) -> GeocodeResult:
    """Resolve one address. Never raises."""
    address = (address or "").strip()
    if not address:
        return GeocodeResult(location=None, source="none", error="no address given")

    api_key = os.getenv("FIELDPILOT_MAPS_API_KEY")
    if not api_key:
        return _offline(address)

    cache = _load_cache() if use_cache else {}
    if address in cache:
        hit = cache[address]
        return GeocodeResult(
            location=Location(lat=hit["lat"], lon=hit["lon"], address=address),
            source="cache",
            formatted_address=hit.get("formatted", address),
            precision=hit.get("precision", ""),
            in_service_area=_in_service_area(hit["lat"], hit["lon"]),
        )

    query = urllib.parse.urlencode(
        {
            "address": address,
            "key": api_key,
            # Bias towards Argentina so a bare street name does not resolve to
            # the same street name in Spain.
            "region": "ar",
            "components": "country:AR",
        }
    )

    try:
        with urllib.request.urlopen(f"{ENDPOINT}?{query}", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - a failed lookup is a phone call
        return GeocodeResult(
            location=None, source="maps", error=f"{type(exc).__name__}: {exc}"
        )

    status = payload.get("status")
    if status != "OK" or not payload.get("results"):
        return GeocodeResult(
            location=None,
            source="maps",
            error=payload.get("error_message") or f"status {status}",
        )

    best = payload["results"][0]
    point = best["geometry"]["location"]
    lat, lon = float(point["lat"]), float(point["lng"])
    precision = best["geometry"].get("location_type", "")
    formatted = best.get("formatted_address", address)

    if use_cache:
        cache[address] = {
            "lat": lat,
            "lon": lon,
            "formatted": formatted,
            "precision": precision,
        }
        _save_cache(cache)

    return GeocodeResult(
        location=Location(lat=lat, lon=lon, address=formatted),
        source="maps",
        formatted_address=formatted,
        precision=precision,
        in_service_area=_in_service_area(lat, lon),
    )
