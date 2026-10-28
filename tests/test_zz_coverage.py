"""Definition of done: every API endpoint has a smoke test and an OpenAPI example (or no body)."""
import pytest

from mizan.api.app import create_app
from mizan.config import Settings
from tests.helpers import HIT

# exercised outside Env.call (raw client / TestClient) or intentionally untested at HTTP level
EXEMPT = {("GET", "/api/v1/health"), ("POST", "/api/v1/auth/login"), ("GET", "/api/v1/ws"), ("GET", "/api/v1/docs"),
          ("GET", "/api/v1/openapi.json")}


def _routes(app):
    return [(m_.upper(), path) for path, ops in app.openapi()["paths"].items() for m_ in ops
            if path.startswith("/api/v1") and m_.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE")]


def test_every_route_is_smoke_tested(tmp_path):
    if len(HIT) < 60:
        pytest.skip("coverage is only meaningful when the full suite runs")
    app = create_app(Settings(data_dir=tmp_path))
    missing = sorted(set(_routes(app)) - HIT - EXEMPT)
    assert not missing, f"routes without a smoke test: {missing}"


def test_request_bodies_have_openapi_examples(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    spec = app.openapi()
    schemas = spec["components"]["schemas"]
    lacking = []
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            body = op.get("requestBody", {}).get("content", {}).get("application/json")
            if not body:
                continue
            ref = body["schema"].get("$ref") or next((x["$ref"] for x in body["schema"].get("anyOf", []) if "$ref" in x), None)
            sch = schemas[ref.split("/")[-1]]
            required = sch.get("required", [])
            if required and not sch.get("examples"):
                lacking.append(f"{method.upper()} {path} ({ref.split('/')[-1]})")
    assert not lacking, lacking
