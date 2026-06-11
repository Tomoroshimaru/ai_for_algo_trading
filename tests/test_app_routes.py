"""Smoke tests for the operator console (frontend) routes and JSON APIs.

Guards the backend<->frontend connectique: every page must render without a
server error, the always-available views must return 200, and the JSON APIs
must stay well-formed. Designed to pass both locally (with materialized
artifacts) and in CI (empty warehouse -> graceful fallbacks).
"""
import pathlib
import sys

import pytest

# the app package lives at the repo root (not under src/); make it importable
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

pytest.importorskip("fastapi", reason="frontend extra not installed")
pytest.importorskip("httpx", reason="httpx needed for TestClient")

from starlette.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

# routes that must always render (no dependency on a populated universe)
CORE_ROUTES = ["/", "/health", "/observability", "/scenario", "/risk", "/qc"]
# routes that need a universe/surface; tolerate 404/503 on an empty warehouse
DATA_ROUTES = ["/surfaces", "/surface3d"]
API_ROUTES = ["/api/risk/SPY", "/api/scenario/SPY", "/api/observability", "/api/freshness"]


@pytest.mark.parametrize("route", CORE_ROUTES)
def test_core_routes_ok(route):
    r = client.get(route)
    assert r.status_code == 200, f"{route} -> {r.status_code}"
    assert r.text, f"{route} returned an empty body"


@pytest.mark.parametrize("route", CORE_ROUTES + DATA_ROUTES)
def test_no_server_error(route):
    # a 500 means broken connectique (provider/template crash), never acceptable
    assert client.get(route).status_code < 500, f"{route} raised a server error"


@pytest.mark.parametrize("route", DATA_ROUTES)
def test_data_routes_render_or_degrade(route):
    assert client.get(route).status_code in (200, 404, 503), route


@pytest.mark.parametrize("route", API_ROUTES)
def test_apis_return_json(route):
    r = client.get(route)
    assert r.status_code in (200, 404), f"{route} -> {r.status_code}"
    if r.status_code == 200:
        assert isinstance(r.json(), dict), f"{route} did not return a JSON object"


def test_control_tower_lists_every_view():
    body = client.get("/").text
    for label in ("Connectivity", "Universe", "Observability", "QC",
                  "Vol Surface", "Scenario", "Risk"):
        assert label in body, f"Control Tower missing tile: {label}"


def test_freshness_api_shape():
    d = client.get("/api/freshness").json()
    assert d["mode"] in ("LIVE", "REPLAY", "DEMO", "UNKNOWN")
    assert d["overall"] in ("fresh", "stale", "missing")
    assert d["n_total"] >= 1 and len(d["items"]) == d["n_total"]


def test_freshness_bar_on_every_page():
    for route in ("/", "/risk", "/scenario", "/qc"):
        body = client.get(route).text
        assert 'id="freshness"' in body, f"{route} missing freshness bar"
        assert "/api/freshness" in body, f"{route} missing freshness poll"
