"""The service layer must be a translation, not a second system."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fieldpilot.api.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _a_developer_laptop(monkeypatch):
    """The suite's default is local: no key configured, not on Cloud Run.

    A key in the developer's own .env must not turn every intake test into a
    401, and a K_SERVICE leaking in from a Cloud Run shell must not turn them
    into a 503. Tests that want either state set it themselves.
    """
    monkeypatch.delenv("FIELDPILOT_API_KEY", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)


@pytest.fixture
def offline_intake(monkeypatch):
    """Intake that never reaches Gemini: the door is under test, not the room."""
    from fieldpilot.agents import intake as intake_mod

    def _fake_receive(text="", image=None, audio=None, **kwargs):
        class _O:
            result = None
            error = "offline test"
            geocode = None
            estimated_usd = 0.0
            used_image = image is not None
            used_audio = audio is not None
        return _O()

    monkeypatch.setattr(intake_mod, "receive", _fake_receive)


def test_health_reports_config_but_never_a_secret(monkeypatch):
    monkeypatch.setenv("FIELDPILOT_MAPS_API_KEY", "AIzaSy-should-not-appear")
    body = client.get("/health").json()
    assert body["ok"] is True
    assert "AIzaSy-should-not-appear" not in str(body)


def test_healthz_still_answers_for_everywhere_that_is_not_cloud_run():
    """Cloud Run's frontend reserves /healthz and 404s it before the container.
    Locally and on any other host it must keep working, because it is what
    everyone types first."""
    assert client.get("/healthz").json()["ok"] is True


def test_compare_runs_offline_and_reproducibly():
    """The demo endpoint must work with no credentials and no network,
    because that is how a judge will first poke it."""
    body = client.get("/compare", params={"seed": 42, "orders": 20}).json()
    assert body["reproducible"] is True
    labels = [r["planner"] for r in body["results"]]
    assert labels == ["fifo-nearest", "ortools"]


def test_compare_clamps_orders_rather_than_solving_a_monster():
    body = client.get("/compare", params={"seed": 42, "orders": 5000}).json()
    assert body["results"][0]["of"] <= 80


def test_intake_without_credentials_degrades_instead_of_500ing():
    """This endpoint is sync `def` on purpose: the agents call asyncio.run(),
    which would blow up inside an async endpoint's event loop. This test walks
    that exact path — if someone 'modernises' the endpoint to async def, this
    is the test that catches it."""
    response = client.post("/intake", json={"text": "no anda la caldera"})
    assert response.status_code == 200
    body = response.json()
    # Without cloud credentials intake reports failure and escalates —
    # gracefully, with the reason attached, never as a bare 500.
    if not body["ok"]:
        assert body["escalations"]
    else:
        assert body["will_dispatch"]["severity"] in {
            "safety", "out_of_service", "degraded", "cosmetic"
        }


def test_intake_rejects_an_empty_message_at_the_door():
    assert client.post("/intake", json={"text": ""}).status_code == 422


# ----------------------------------------------------------------------
# The multimodal endpoint
# ----------------------------------------------------------------------

def test_multimodal_rejects_a_completely_empty_request():
    body = client.post("/intake/multimodal", data={"text": ""}).json()
    assert body["ok"] is False
    assert "at least one" in body["error"]


def test_multimodal_rejects_an_unsupported_file_type():
    """An unauthenticated upload endpoint that accepts anything is an
    invitation. Content types are an allowlist, not a blocklist."""
    body = client.post(
        "/intake/multimodal",
        data={"text": "no anda la caldera"},
        files={"image": ("x.pdf", b"%PDF-fake", "application/pdf")},
    ).json()
    assert body["ok"] is False
    assert "unsupported image type" in body["error"]


def test_multimodal_rejects_an_oversized_image():
    from fieldpilot.api import main as api_main

    big = b"x" * (api_main.MAX_IMAGE_BYTES + 1)
    body = client.post(
        "/intake/multimodal",
        data={"text": "hola"},
        files={"image": ("big.jpg", big, "image/jpeg")},
    ).json()
    assert body["ok"] is False
    assert "too large" in body["error"]


def test_multimodal_spools_and_always_cleans_up(monkeypatch, tmp_path):
    """The temp file must be gone whether intake succeeds or degrades."""
    import glob
    import tempfile

    from fieldpilot.agents import intake as intake_mod

    seen_paths = {}

    def _fake_receive(text="", image=None, audio=None, **kwargs):
        seen_paths["image"] = image
        class _O:
            result = None
            error = "offline test"
            geocode = None
            estimated_usd = 0.0
            used_image = image is not None
            used_audio = False
        return _O()

    monkeypatch.setattr(intake_mod, "receive", _fake_receive)
    body = client.post(
        "/intake/multimodal",
        data={"text": "hola"},
        files={"image": ("foto.jpg", b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg")},
    ).json()

    assert body["ok"] is False  # degraded outcome from the fake, not a 500
    assert seen_paths["image"] is not None
    import os
    assert not os.path.exists(seen_paths["image"])  # cleaned up


def test_text_only_multimodal_matches_plain_intake_shape(monkeypatch):
    """Two doors, one room: both endpoints must produce the same response
    shape, because they run the same code."""
    r1 = client.post("/intake", json={"text": "no anda la caldera"}).json()
    r2 = client.post("/intake/multimodal", data={"text": "no anda la caldera"}).json()
    assert set(r1.keys()) - {"inputs_used"} == set(r2.keys()) - {"inputs_used"}


# ----------------------------------------------------------------------
# Who may spend money
# ----------------------------------------------------------------------

MSG = {"text": "no anda la caldera"}


def test_intake_needs_the_key_once_one_is_configured(monkeypatch, offline_intake):
    """The URL is public; the endpoints that cost money are not."""
    monkeypatch.setenv("FIELDPILOT_API_KEY", "judges-only")
    assert client.post("/intake", json=MSG).status_code == 401
    wrong = client.post("/intake", json=MSG, headers={"X-API-Key": "wrong"})
    assert wrong.status_code == 401
    right = client.post("/intake", json=MSG, headers={"X-API-Key": "judges-only"})
    assert right.status_code == 200


def test_multimodal_is_behind_the_same_door(monkeypatch, offline_intake):
    monkeypatch.setenv("FIELDPILOT_API_KEY", "judges-only")
    assert client.post("/intake/multimodal", data={"text": "hola"}).status_code == 401
    ok = client.post(
        "/intake/multimodal", data={"text": "hola"},
        headers={"X-API-Key": "judges-only"},
    )
    assert ok.status_code == 200


def test_the_free_endpoints_never_ask_for_a_key(monkeypatch):
    monkeypatch.setenv("FIELDPILOT_API_KEY", "judges-only")
    assert client.get("/health").status_code == 200
    assert client.get("/compare", params={"seed": 42, "orders": 20}).status_code == 200
    assert client.get("/docs").status_code == 200


def test_cloud_run_without_a_key_fails_closed(monkeypatch, offline_intake):
    """A secret that did not reach the container is a misconfiguration, and
    the honest answer for a misconfigured paid endpoint is closed, not open."""
    monkeypatch.setenv("K_SERVICE", "fieldpilot")
    response = client.post("/intake", json=MSG)
    assert response.status_code == 503
    assert "FIELDPILOT_API_KEY" in response.json()["detail"]


def test_a_trailing_newline_in_the_secret_locks_nobody_out(monkeypatch, offline_intake):
    """`openssl rand | gcloud secrets create` stores the newline too; the key a
    judge pastes has none. Both sides are stripped so that is not a 401."""
    monkeypatch.setenv("FIELDPILOT_API_KEY", "judges-only\n")
    ok = client.post("/intake", json=MSG, headers={"X-API-Key": "judges-only"})
    assert ok.status_code == 200


def test_docs_declare_the_scheme_so_authorize_appears():
    """The browser-only flow depends on Swagger rendering an Authorize button,
    which depends on the OpenAPI document declaring the scheme — on the two
    paid routes and on nothing else."""
    spec = client.get("/openapi.json").json()
    schemes = spec["components"]["securitySchemes"]
    assert any(s.get("name") == "X-API-Key" for s in schemes.values())
    assert spec["paths"]["/intake"]["post"].get("security")
    assert spec["paths"]["/intake/multimodal"]["post"].get("security")
    assert not spec["paths"]["/compare"]["get"].get("security")
    assert not spec["paths"]["/health"]["get"].get("security")


def test_health_reports_the_key_as_present_but_never_its_value(monkeypatch):
    monkeypatch.setenv("FIELDPILOT_API_KEY", "judges-only-secret-value")
    text = str(client.get("/health").json())
    assert "judges-only-secret-value" not in text
    assert "api key=set" in text
