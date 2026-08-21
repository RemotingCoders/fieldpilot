"""A reproducible day of field service work.

Everything here is driven by an explicit seed. The same seed produces the same
day every time, which is what makes it possible to rehearse the demo, to
compare planners honestly, and to record an unedited live run knowing what is
about to happen.

The incident types and durations are modelled on residential and light
commercial HVAC, gas and plumbing work: the jobs are short, the skills are
narrow and certified, and the day is far more fragile than a delivery route.
"""

from __future__ import annotations

import random

from pydantic import BaseModel

from fieldpilot.domain.models import (
    Account,
    BookableResource,
    Characteristic,
    IncidentType,
    Location,
    Severity,
    SlaTier,
    Territory,
    WorkOrder,
)

# Roughly the built-up area of Buenos Aires.
LAT_RANGE = (-34.68, -34.55)
LON_RANGE = (-58.50, -58.36)

CHARACTERISTICS = [
    Characteristic(characteristic_id="gas", name="Gas certified"),
    Characteristic(characteristic_id="hvac", name="HVAC"),
    Characteristic(characteristic_id="elec", name="Electrical"),
    Characteristic(characteristic_id="plumb", name="Plumbing"),
]

INCIDENT_TYPES = [
    IncidentType(
        incident_type_id="gas-smell",
        name="Reported gas smell",
        default_duration_min=45,
        required_characteristics=["gas"],
        default_severity=Severity.SAFETY,
    ),
    IncidentType(
        incident_type_id="boiler-no-heat",
        name="Boiler producing no heat",
        default_duration_min=75,
        required_characteristics=["gas", "hvac"],
        default_severity=Severity.OUT_OF_SERVICE,
    ),
    IncidentType(
        incident_type_id="boiler-leak",
        name="Boiler leaking water",
        default_duration_min=60,
        required_characteristics=["plumb", "hvac"],
        default_severity=Severity.OUT_OF_SERVICE,
    ),
    IncidentType(
        incident_type_id="ac-not-cooling",
        name="Air conditioning not cooling",
        default_duration_min=60,
        required_characteristics=["hvac"],
        default_severity=Severity.DEGRADED,
    ),
    IncidentType(
        incident_type_id="thermostat",
        name="Thermostat replacement",
        default_duration_min=30,
        required_characteristics=["elec"],
        default_severity=Severity.DEGRADED,
    ),
    IncidentType(
        incident_type_id="annual-service",
        name="Annual maintenance visit",
        default_duration_min=90,
        required_characteristics=["hvac"],
        default_severity=Severity.COSMETIC,
    ),
    IncidentType(
        incident_type_id="water-heater-install",
        name="Water heater installation",
        default_duration_min=150,
        required_characteristics=["gas", "plumb"],
        default_severity=Severity.DEGRADED,
    ),
]

TERRITORIES = [
    Territory(territory_id="north", name="Zona Norte"),
    Territory(territory_id="south", name="Zona Sur"),
]

# Technician crews. Note that gas certification is scarce, which is what makes
# the assignment problem interesting: the safety jobs can only go to two people.
CREW = [
    ("res-01", "Ana Pereyra", ["gas", "hvac", "plumb"], 1.00),
    ("res-02", "Bruno Diaz", ["hvac", "elec"], 1.15),
    ("res-03", "Carla Nunez", ["gas", "plumb"], 0.95),
    ("res-04", "Diego Rossi", ["hvac"], 1.05),
]

ACCOUNT_NAMES = [
    "Edificio Belgrano 1420", "Clinica San Justo", "Hotel Palermo Soho",
    "Colegio Santa Rita", "Panaderia La Espiga", "Torre Catalinas",
    "Residencia Los Olivos", "Gimnasio Nucleo", "Supermercado Dia a Dia",
    "Estudio Contable Vidal", "Cafe Notable", "Geriatrico El Refugio",
    "Imprenta Salguero", "Veterinaria Central", "Coworking Distrito",
    "Lavadero Aguas Claras", "Farmacia del Puerto", "Escuela Tecnica 12",
]


class Scenario(BaseModel):
    """One planning problem: a fleet, a backlog, and the accounts behind it."""

    seed: int
    accounts: dict[str, Account]
    resources: list[BookableResource]
    work_orders: list[WorkOrder]
    incident_types: dict[str, IncidentType]

    def account_for(self, order: WorkOrder) -> Account:
        return self.accounts[order.account_id]


def _random_location(rng: random.Random, address: str) -> Location:
    return Location(
        lat=rng.uniform(*LAT_RANGE),
        lon=rng.uniform(*LON_RANGE),
        address=address,
    )


def build(seed: int = 42, n_orders: int = 26) -> Scenario:
    """Generate a day that is oversubscribed on purpose.

    More work arrives than four technicians can complete. That is the normal
    state of a field service operation, and it is the only condition under
    which the question "who do we go to first" actually has stakes.
    """
    rng = random.Random(seed)
    incident_types = {t.incident_type_id: t for t in INCIDENT_TYPES}

    accounts: dict[str, Account] = {}
    for i in range(n_orders):
        name = ACCOUNT_NAMES[i % len(ACCOUNT_NAMES)]
        account_id = f"acc-{i:03d}"
        tier = rng.choices(
            [SlaTier.PLATINUM, SlaTier.GOLD, SlaTier.SILVER, SlaTier.NONE],
            weights=[1, 3, 4, 4],
        )[0]
        accounts[account_id] = Account(
            account_id=account_id,
            name=name,
            sla_tier=tier,
            annual_value_usd=round(rng.uniform(500, 40_000), 2),
            location=_random_location(rng, f"{name}, Buenos Aires"),
        )

    resources: list[BookableResource] = []
    for idx, (rid, name, chars, factor) in enumerate(CREW):
        resources.append(
            BookableResource(
                resource_id=rid,
                name=name,
                characteristics=chars,
                territory_id=None,  # single territory day; kept for the model's sake
                start_location=_random_location(rng, f"{name} home base"),
                shift_start_min=8 * 60,
                shift_end_min=17 * 60 + 30,
                duration_factor=factor,
            )
        )

    work_orders: list[WorkOrder] = []
    for i in range(n_orders):
        account = accounts[f"acc-{i:03d}"]
        incident = rng.choices(
            INCIDENT_TYPES,
            weights=[1, 5, 3, 5, 4, 6, 2],  # routine work dominates, as it does in reality
        )[0]

        # Most jobs are open all day; some customers can only be there for part of it.
        if rng.random() < 0.35:
            start = rng.choice([8, 9, 10, 13, 14, 15]) * 60
            window = (start, start + rng.choice([120, 180, 240]))
        else:
            window = (8 * 60, 17 * 60 + 30)

        jitter = rng.choice([-15, 0, 0, 0, 15, 30])

        work_orders.append(
            WorkOrder(
                work_order_id=f"wo-{i:03d}",
                account_id=account.account_id,
                incident_type_id=incident.incident_type_id,
                location=account.location,
                window_start_min=window[0],
                window_end_min=min(window[1], 17 * 60 + 30),
                duration_min=max(20, incident.default_duration_min + jitter),
                required_characteristics=list(incident.required_characteristics),
                severity=incident.default_severity,
                days_waiting=rng.choices([0, 1, 2, 3, 5, 9], weights=[6, 4, 3, 2, 1, 1])[0],
                reschedule_count=rng.choices([0, 1, 2], weights=[8, 2, 1])[0],
            )
        )

    return Scenario(
        seed=seed,
        accounts=accounts,
        resources=resources,
        work_orders=work_orders,
        incident_types=incident_types,
    )
