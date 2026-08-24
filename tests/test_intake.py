"""Intake tests that need no API key.

The model call cannot be unit tested cheaply, but the contract around it can —
and intake is where a wrong answer is most expensive, because everything
downstream trusts the record it produces.
"""

from __future__ import annotations

import pytest

from fieldpilot.agents import intake as intake_mod
from fieldpilot.agents.intake import IntakeResult, disagreement
from fieldpilot.domain.models import Location, Severity


def _result(**overrides) -> IntakeResult:
    base = dict(
        incident_type_id="boiler-no-heat",
        severity=Severity.OUT_OF_SERVICE,
        required_characteristics=["gas", "hvac"],
        estimated_duration_min=75,
        window_start_min=8 * 60,
        window_end_min=17 * 60 + 30,
        customer_words="no calienta nada desde ayer",
        notes="Boiler producing no heat since yesterday.",
        confidence=0.9,
        needs_human=False,
        reasoning="Customer reports no heat from a gas boiler.",
    )
    base.update(overrides)
    return IntakeResult(**base)


def test_a_classification_becomes_a_usable_work_order() -> None:
    order = _result().to_work_order(
        "wo-001", "acc-001", Location(lat=-34.6, lon=-58.4)
    )
    assert order.incident_type_id == "boiler-no-heat"
    assert order.required_characteristics == ["gas", "hvac"]
    assert order.duration_min == 75
    assert order.severity == Severity.OUT_OF_SERVICE


def test_a_nonsense_duration_cannot_produce_a_zero_length_visit() -> None:
    order = _result(estimated_duration_min=0).to_work_order(
        "wo-001", "acc-001", Location(lat=-34.6, lon=-58.4)
    )
    assert order.duration_min >= 15


def test_disagreement_notices_a_changed_diagnosis() -> None:
    with_photo = _result(incident_type_id="gas-smell", severity=Severity.SAFETY)
    without = _result()
    changes = disagreement(with_photo, without)
    assert any("type" in c for c in changes)
    assert any("severity" in c for c in changes)


def test_disagreement_notices_a_changed_trade() -> None:
    changes = disagreement(
        _result(required_characteristics=["gas", "hvac", "elec"]), _result()
    )
    assert any("skills" in c for c in changes)


def test_disagreement_ignores_trivial_duration_wobble() -> None:
    """A ten-minute difference is noise, not the photograph telling us something."""
    assert disagreement(_result(estimated_duration_min=80), _result()) == []


def test_disagreement_notices_a_real_duration_change() -> None:
    changes = disagreement(_result(estimated_duration_min=150), _result())
    assert any("duration" in c for c in changes)


def test_disagreement_notices_an_escalation_appearing() -> None:
    changes = disagreement(_result(needs_human=True), _result())
    assert any("escalation" in c for c in changes)


def test_identical_classifications_disagree_about_nothing() -> None:
    """This is the result that would mean the photo is decoration."""
    assert disagreement(_result(), _result()) == []


def test_a_missing_file_is_reported_not_raised() -> None:
    outcome = intake_mod.receive(text="hola", image="/nowhere/photo.jpg")
    assert outcome.result is None
    assert outcome.error is not None
    assert "missing file" in outcome.error


def test_intake_never_raises_without_credentials() -> None:
    """An intake failure is a phone call, not a crash."""
    outcome = intake_mod.receive(text="el aire no enfria")
    assert outcome.result is None or outcome.result.incident_type_id
    if outcome.result is None:
        assert outcome.error


def test_empty_input_is_refused() -> None:
    outcome = intake_mod.receive()
    assert outcome.result is None
    assert outcome.error


@pytest.mark.parametrize(
    "phrase",
    [
        "customer_words",
        "needs_human",
        "do not translate it",
        "a photograph carries information",
        "uncertified technician",
        # Found on a real request: adding a photo raised confidence in the
        # diagnosis and silently cleared an escalation that had been raised for
        # a missing address. The address was still missing.
        "these are independent",
        "missing address",
        # Found on a real voice note: the customer offered two disjoint windows
        # and the model has one field to put them in.
        "never silently discard the second window",
    ],
)
def test_the_instruction_keeps_its_guardrails(phrase: str) -> None:
    """These sentences are load-bearing: verbatim customer speech, honest
    escalation, and letting the image change the answer. Losing any of them in
    an edit would be invisible until something went wrong in the field."""
    assert phrase.lower() in intake_mod.INSTRUCTION.lower()


def test_the_incident_vocabulary_matches_the_scenario() -> None:
    """Intake must not invent categories the rest of the system cannot plan."""
    from fieldpilot.sim.scenario import INCIDENT_TYPES

    assert set(intake_mod.KNOWN_INCIDENTS) == {t.incident_type_id for t in INCIDENT_TYPES}


def test_the_skill_vocabulary_matches_the_crew() -> None:
    from fieldpilot.sim.scenario import CREW

    held = {c for _, _, chars, _ in CREW for c in chars}
    assert set(intake_mod.KNOWN_CHARACTERISTICS) >= held


# --------------------------------------------------------------------------
# Geocoding: the last link between a message and a place to drive to
# --------------------------------------------------------------------------


def test_an_empty_address_is_not_a_location() -> None:
    from fieldpilot.planning.geocode import geocode

    result = geocode("")
    assert result.location is None
    assert not result.usable


def test_the_offline_stand_in_is_never_treated_as_a_real_location(monkeypatch) -> None:
    """It exists so the pipeline runs without a paid API. If anything downstream
    ever mistakes it for a geocode, a van gets sent to a hashed coordinate."""
    from fieldpilot.planning.geocode import geocode

    monkeypatch.delenv("FIELDPILOT_MAPS_API_KEY", raising=False)
    result = geocode("Av. Cabildo 2340, CABA")

    assert result.location is not None
    assert result.source == "offline"
    assert not result.usable
    assert "not a real location" in result.line()


def test_the_offline_stand_in_is_deterministic(monkeypatch) -> None:
    from fieldpilot.planning.geocode import geocode

    monkeypatch.delenv("FIELDPILOT_MAPS_API_KEY", raising=False)
    a = geocode("Belgrano 1420")
    b = geocode("Belgrano 1420")
    assert (a.location.lat, a.location.lon) == (b.location.lat, b.location.lon)


def test_different_addresses_get_different_points(monkeypatch) -> None:
    from fieldpilot.planning.geocode import geocode

    monkeypatch.delenv("FIELDPILOT_MAPS_API_KEY", raising=False)
    a = geocode("Belgrano 1420")
    b = geocode("Av. Cabildo 2340")
    assert (a.location.lat, a.location.lon) != (b.location.lat, b.location.lon)


def test_a_point_outside_the_service_area_is_not_dispatchable() -> None:
    """An ambiguous street name resolving to another province looks like a
    perfectly valid coordinate. Distance is the only clue."""
    from fieldpilot.domain.models import Location
    from fieldpilot.planning.geocode import GeocodeResult

    far = GeocodeResult(
        location=Location(lat=-31.42, lon=-64.18),  # Cordoba
        source="maps",
        precision="ROOFTOP",
        in_service_area=False,
    )
    assert not far.usable
    assert "OUTSIDE SERVICE AREA" in far.line()


def test_a_neighbourhood_match_counts_as_vague() -> None:
    """A technician sent to the centroid of a neighbourhood has not been sent
    anywhere."""
    from fieldpilot.domain.models import Location
    from fieldpilot.planning.geocode import GeocodeResult

    rough = GeocodeResult(
        location=Location(lat=-34.60, lon=-58.40),
        source="maps",
        precision="APPROXIMATE",
    )
    assert rough.vague
    assert "imprecise" in rough.line()


def test_a_rooftop_match_inside_the_area_is_dispatchable() -> None:
    from fieldpilot.domain.models import Location
    from fieldpilot.planning.geocode import GeocodeResult

    good = GeocodeResult(
        location=Location(lat=-34.5620, lon=-58.4560),
        source="maps",
        precision="ROOFTOP",
    )
    assert good.usable
    assert not good.vague


def test_the_service_area_covers_buenos_aires_and_not_much_else() -> None:
    from fieldpilot.planning.geocode import _in_service_area

    assert _in_service_area(-34.6037, -58.3816)   # Obelisco
    assert _in_service_area(-34.4708, -58.5130)   # San Isidro
    assert not _in_service_area(-31.4201, -64.1888)   # Cordoba
    assert not _in_service_area(-34.9011, -56.1645)   # Montevideo


def test_intake_asks_for_the_address_verbatim() -> None:
    lowered = intake_mod.INSTRUCTION.lower()
    assert "do not guess, complete or correct it" in lowered
    assert "a geocoder runs on this string" in lowered


def test_incident_weights_match_incident_types():
    """A weight list that drifts out of step with the type list is silent.

    `random.choices` raises only when the lengths differ, so a type added in one
    place and weighted in another quietly reshapes every scenario without
    failing anything.
    """
    from fieldpilot.sim import scenario as scenario_mod

    assert len(scenario_mod.INCIDENT_WEIGHTS) == len(scenario_mod.INCIDENT_TYPES)


def test_both_split_failure_directions_exist():
    """The taxonomy gap that a real intake call exposed, kept closed."""
    from fieldpilot.sim.scenario import INCIDENT_TYPES

    ids = {t.incident_type_id for t in INCIDENT_TYPES}
    assert {"ac-not-cooling", "split-no-heat"} <= ids

    by_id = {t.incident_type_id: t for t in INCIDENT_TYPES}
    # Same equipment, so the same trade goes — the distinction is about the
    # parts the technician packs, not about who is dispatched.
    assert (
        by_id["split-no-heat"].required_characteristics
        == by_id["ac-not-cooling"].required_characteristics
    )


def _report(paired, self_with, trials=5, self_without=None):
    from fieldpilot.agents.intake import AblationReport

    return AblationReport(
        trials=trials,
        paired=paired,
        self_with=self_with,
        self_without=self_without or {},
        cost_usd=0.0,
    )


def test_a_change_below_the_self_noise_is_not_credited_to_the_photo():
    report = _report(paired={"duration": 4}, self_with={"duration": 8}, trials=12)
    assert report.verdict("duration") == "indistinguishable from noise"


def test_a_consistent_change_with_no_self_noise_is_credited():
    report = _report(paired={"skills": 12}, self_with={}, trials=12)
    assert report.verdict("skills") == "changed every trial"


def test_an_unmoved_field_says_so():
    assert _report(paired={}, self_with={}).verdict("type") == "not moved by the photo"


def test_noise_floor_takes_the_worse_of_the_two_arms():
    """Either arm being unstable makes the field unreliable, not just the one
    the photo was added to."""
    report = _report(
        paired={"window": 5}, self_with={}, self_without={"window": 10}, trials=12
    )
    assert report.verdict("window") == "indistinguishable from noise"


def test_five_trials_buys_no_verdict():
    """The bug this rule was rewritten for.

    2/5 against 1/4 is a one-run difference. The old rule called it a result.
    """
    report = _report(paired={"window": 2}, self_with={"window": 1}, trials=5)
    assert "too few trials" in report.verdict("window")


def test_a_thin_margin_is_not_called_even_with_enough_trials():
    report = _report(paired={"skills": 7}, self_with={"skills": 6}, trials=12)
    assert report.verdict("skills") == "too close to the noise to call"


def test_a_wide_margin_with_enough_trials_is_called():
    report = _report(paired={"skills": 11}, self_with={"skills": 1}, trials=12)
    assert report.verdict("skills") == "above the noise floor"


def test_single_trial_report_admits_it_proves_nothing():
    text = "\n".join(_report(paired={"type": 1}, self_with={}, trials=1).lines())
    assert "no noise column" in text


def test_self_inconsistent_fields_are_named_regardless_of_the_photo():
    """The severity finding: nothing to do with the image, and the most
    important thing the report says."""
    report = _report(paired={}, self_with={"severity": 4}, trials=5)
    assert "severity" in report.unstable_fields()
    assert "severity" in "\n".join(report.lines())


def test_changed_fields_agrees_with_disagreement():
    """Two functions read the same differences; they must not drift apart."""
    from fieldpilot.agents.intake import changed_fields, disagreement

    a = _result(estimated_duration_min=150, required_characteristics=["gas"])
    b = _result()
    assert len(changed_fields(a, b)) == len(disagreement(a, b))


def test_severity_is_held_at_the_taxonomy_default_when_intake_goes_lower():
    """Measured reason: the model changed severity on 4 of 4 re-runs of one
    identical request while never changing the incident type."""
    from fieldpilot.domain.models import Severity

    result = _result(incident_type_id="split-no-heat", severity=Severity.DEGRADED)
    settled, note = result.settled_severity()
    assert settled == Severity.OUT_OF_SERVICE
    assert "held at" in note


def test_intake_may_still_raise_severity():
    """A routine call that mentions a burning smell is a safety call, and the
    taxonomy default cannot know that."""
    from fieldpilot.domain.models import Severity

    result = _result(incident_type_id="annual-service", severity=Severity.SAFETY)
    settled, note = result.settled_severity()
    assert settled == Severity.SAFETY
    assert "raised" in note


def test_unknown_incident_type_keeps_the_model_severity():
    """There is no default to fall back to, so overriding would invent one."""
    from fieldpilot.domain.models import Severity

    result = _result(incident_type_id="unknown", severity=Severity.DEGRADED)
    settled, note = result.settled_severity()
    assert settled == Severity.DEGRADED
    assert note == ""


def test_the_work_order_records_that_severity_was_overridden():
    from fieldpilot.domain.models import Location, Severity

    result = _result(incident_type_id="split-no-heat", severity=Severity.DEGRADED)
    order = result.to_work_order("wo-1", "acc-1", Location(lat=0.0, lon=0.0, address="x"))
    assert order.severity == Severity.OUT_OF_SERVICE
    assert "held at" in order.notes


def test_skills_come_from_the_incident_type_not_the_model():
    """The instability that replicated: hvac vs hvac+elec on the same input.

    Three technicians hold hvac; one holds hvac+elec. Letting an unstable field
    decide that is letting a coin flip decide who can take the job.
    """
    result = _result(
        incident_type_id="split-no-heat", required_characteristics=["hvac", "elec"]
    )
    chars, note = result.dispatch_characteristics()
    assert chars == ["hvac"]
    assert "proposed" in note


def test_matching_skills_produce_no_note():
    result = _result(incident_type_id="split-no-heat", required_characteristics=["hvac"])
    chars, note = result.dispatch_characteristics()
    assert chars == ["hvac"]
    assert note == ""


def test_unknown_type_keeps_the_model_skills():
    result = _result(incident_type_id="unknown", required_characteristics=["gas"])
    assert result.dispatch_characteristics()[0] == ["gas"]


def test_dispatch_comparison_absorbs_a_wobble_the_raw_comparison_sees():
    """The claim the architecture rests on, as a test.

    Two results that disagree on skills and severity produce the same work
    order, so the disagreement never reaches a van.
    """
    from fieldpilot.agents.intake import changed_dispatch_fields, changed_fields
    from fieldpilot.domain.models import Severity

    a = _result(
        incident_type_id="split-no-heat",
        required_characteristics=["hvac", "elec"],
        severity=Severity.DEGRADED,
    )
    b = _result(
        incident_type_id="split-no-heat",
        required_characteristics=["hvac"],
        severity=Severity.OUT_OF_SERVICE,
    )
    assert changed_fields(a, b) == {"skills", "severity"}
    assert changed_dispatch_fields(a, b) == set()


def test_both_overrides_are_recorded_on_the_work_order():
    from fieldpilot.domain.models import Location, Severity

    result = _result(
        incident_type_id="split-no-heat",
        required_characteristics=["hvac", "elec"],
        severity=Severity.DEGRADED,
    )
    order = result.to_work_order("wo-1", "acc-1", Location(lat=0.0, lon=0.0, address="x"))
    assert order.required_characteristics == ["hvac"]
    assert order.severity == Severity.OUT_OF_SERVICE
    assert "held at" in order.notes and "proposed" in order.notes
