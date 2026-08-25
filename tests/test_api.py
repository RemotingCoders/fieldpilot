"""The service layer must be a translation, not a second system."""

from __future__ import annotations

from fastapi.testclient import TestClient

from fieldpilot.api.main import app

client = TestClient(app)


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
