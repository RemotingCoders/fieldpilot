"""Domain model for field service dispatch.

Vocabulary follows Dynamics 365 Field Service so the model is legible to anyone
who has worked in the field service world: work orders carry incident types and
resource requirements, technicians are bookable resources with characteristics,
and an assignment is a booking.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from math import asin, cos, radians, sin, sqrt

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class BookingStatus(str, Enum):
    """Lifecycle of a single technician visit."""

    UNSCHEDULED = "unscheduled"
    SCHEDULED = "scheduled"
    TRAVELLING = "travelling"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELED = "canceled"


class SlaTier(str, Enum):
    """Contractual service level attached to an account."""

    PLATINUM = "platinum"
    GOLD = "gold"
    SILVER = "silver"
    NONE = "none"


class Severity(str, Enum):
    """How dangerous the reported condition is, independent of contract."""

    SAFETY = "safety"          # gas leak, exposed wiring, flooding
    OUT_OF_SERVICE = "out_of_service"
    DEGRADED = "degraded"
    COSMETIC = "cosmetic"


# --------------------------------------------------------------------------
# Geography
# --------------------------------------------------------------------------


class Location(BaseModel):
    """A point on the map. Latitude and longitude in decimal degrees."""

    lat: float
    lon: float
    address: str = ""

    def haversine_km(self, other: "Location") -> float:
        """Great-circle distance in kilometres.

        Used as the offline fallback for travel time so the whole system runs
        without a Maps API key. Real travel times come from the Routes API and
        are cached per city.
        """
        r = 6371.0
        d_lat = radians(other.lat - self.lat)
        d_lon = radians(other.lon - self.lon)
        a = (
            sin(d_lat / 2) ** 2
            + cos(radians(self.lat)) * cos(radians(other.lat)) * sin(d_lon / 2) ** 2
        )
        return 2 * r * asin(sqrt(a))


class Territory(BaseModel):
    """A dispatch area. Technicians are normally kept inside their territory."""

    territory_id: str
    name: str


# --------------------------------------------------------------------------
# Skills and problem types
# --------------------------------------------------------------------------


class Characteristic(BaseModel):
    """A skill or certification a technician can hold and a job can require."""

    characteristic_id: str
    name: str


class IncidentType(BaseModel):
    """A named class of problem with its default duration and skill needs.

    In real deployments this table is the single most valuable thing an
    organisation owns: it encodes years of learning about how long work
    actually takes.
    """

    incident_type_id: str
    name: str
    default_duration_min: int
    required_characteristics: list[str] = Field(default_factory=list)
    default_severity: Severity = Severity.DEGRADED


# --------------------------------------------------------------------------
# Customer side
# --------------------------------------------------------------------------


class Account(BaseModel):
    """The customer. Carries the contract facts that drive real urgency."""

    account_id: str
    name: str
    sla_tier: SlaTier = SlaTier.NONE
    annual_value_usd: float = 0.0
    location: Location


class WorkOrder(BaseModel):
    """One job to be done at one place.

    `penalty_cost` is the bridge between judgement and mathematics: the triage
    agent writes it, the solver optimises against it. A high penalty means
    "dropping this order is expensive", which is how a language model's reading
    of a situation becomes something a solver can actually minimise.
    """

    work_order_id: str
    account_id: str
    incident_type_id: str
    location: Location

    # Time window, in minutes from midnight local time.
    window_start_min: int = 8 * 60
    window_end_min: int = 18 * 60

    duration_min: int = 60
    required_characteristics: list[str] = Field(default_factory=list)
    territory_id: str | None = None

    severity: Severity = Severity.DEGRADED
    reported_on: date | None = None
    days_waiting: int = 0
    reschedule_count: int = 0

    # What the call taker typed. Unstructured, often empty, and frequently the
    # only place the deciding fact lives.
    notes: str = ""

    # Written by triage. Higher means more expensive to leave unserved.
    penalty_cost: int = 1_000
    triage_rationale: str = ""

    # Ground truth for evaluation only. The scenario generator sets this; no
    # triage implementation may read it, and nothing in the planning path uses
    # it. It exists so that competing triage methods can be scored on how much
    # genuine urgency their plan delivered.
    true_penalty: int = 0

    status: BookingStatus = BookingStatus.UNSCHEDULED


# --------------------------------------------------------------------------
# Supply side
# --------------------------------------------------------------------------


class BookableResource(BaseModel):
    """A technician available for scheduling on a given day."""

    resource_id: str
    name: str
    characteristics: list[str] = Field(default_factory=list)
    territory_id: str | None = None
    start_location: Location

    # Shift, in minutes from midnight local time.
    shift_start_min: int = 8 * 60
    shift_end_min: int = 18 * 60

    # Learned multiplier on estimated durations. The memory bank moves this
    # away from 1.0 as real completion times come in.
    duration_factor: float = 1.0

    def can_serve(self, order: WorkOrder) -> bool:
        """True when this technician holds every characteristic the job needs."""
        if order.territory_id and self.territory_id:
            if order.territory_id != self.territory_id:
                return False
        return all(c in self.characteristics for c in order.required_characteristics)


class Booking(BaseModel):
    """A technician assigned to a work order at a specific time."""

    booking_id: str
    work_order_id: str
    resource_id: str
    arrival_min: int
    departure_min: int
    travel_min: int = 0
    status: BookingStatus = BookingStatus.SCHEDULED


class Plan(BaseModel):
    """The output of a planner: who goes where, and what could not be covered."""

    bookings: list[Booking] = Field(default_factory=list)
    unserved_work_order_ids: list[str] = Field(default_factory=list)
    planner: str = "unknown"
    solve_ms: int = 0

    def bookings_for(self, resource_id: str) -> list[Booking]:
        """This technician's route, in visit order."""
        return sorted(
            (b for b in self.bookings if b.resource_id == resource_id),
            key=lambda b: b.arrival_min,
        )
