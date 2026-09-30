"""Parity with Olla v0.0.28, checked against responses captured from a live instance
(tests/fixtures/olla_v0.0.28_contract.json). llm-head may add fields, never drop them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import cluster

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "olla_v0.0.28_contract.json").read_text())


def keyset(obj, prefix=""):
    """Every key path in a JSON value; list items are represented by their first element."""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(prefix + k)
            out |= keyset(v, prefix + k + ".")
    elif isinstance(obj, list) and obj:
        out |= keyset(obj[0], prefix + "[].")
    return out


# Olla-internal fields with no llm-head equivalent. Nothing we know of reads them.
NOT_APPLICABLE = {
    "GET /internal/process": "Go runtime stats (goroutines, GC); llm-head reports Python process stats instead",
    "GET /version": "build/go_version/capabilities_experimental describe Olla's Go build",
}

SHAPES = [
    "GET /internal/health",
    "GET /internal/status",
    "GET /internal/status/endpoints",
    "GET /internal/status/models",
    "GET /olla/models",
    "GET /olla/ollama/api/tags",
    "GET /olla/ollama/v1/models",
]


@pytest.mark.parametrize("key", SHAPES)
async def test_json_shape_is_a_superset_of_olla(key):
    method, path = key.split(" ", 1)
    olla = FIXTURE[key]
    async with cluster() as (c, head, fakes, log):
        # Put some traffic through so per-endpoint stats are populated like the capture.
        await c.post("/olla/ollama/api/generate", json={"model": "llama3.2:3b", "prompt": "x", "stream": False})
        r = await c.request(method, path)
    assert r.status_code == olla["status"]
    # models_by_family is keyed by family name: data, not schema.
    missing = {k for k in keyset(olla["json"]) - keyset(r.json()) if not k.startswith("models_by_family.")}
    assert not missing, f"{key} is missing Olla fields: {sorted(missing)}"


@pytest.mark.parametrize("key", SHAPES)
async def test_content_type_matches(key):
    method, path = key.split(" ", 1)
    async with cluster() as (c, head, fakes, log):
        r = await c.request(method, path)
    assert r.headers["content-type"].split(";")[0] == FIXTURE[key]["headers"]["Content-Type"].split(";")[0]


async def test_cors_preflight_matches_olla():
    olla = FIXTURE["OPTIONS preflight"]
    async with cluster() as (c, head, fakes, log):
        r = await c.options("/olla/ollama/api/chat", headers={
            "Origin": "http://example.com", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
    assert r.status_code == olla["status"] == 204
    for h in ("Access-Control-Allow-Origin", "Access-Control-Allow-Methods", "Access-Control-Allow-Headers",
              "Access-Control-Max-Age", "Vary"):
        assert r.headers[h] == olla["headers"][h], h


async def test_cors_on_simple_request_matches_olla():
    olla = FIXTURE["GET with Origin"]["headers"]
    async with cluster() as (c, head, fakes, log):
        r = await c.get("/olla/ollama/api/tags", headers={"Origin": "http://example.com"})
    assert r.headers["access-control-allow-origin"] == olla["Access-Control-Allow-Origin"]
    exposed = {h.strip() for h in r.headers["access-control-expose-headers"].split(",")}
    for h in ("X-Olla-Request-Id", "X-Olla-Endpoint", "X-Olla-Backend-Type", "X-Olla-Model",
              "X-Olla-Response-Time", "X-Olla-Routing-Strategy", "X-Olla-Routing-Decision",
              "X-Olla-Routing-Reason"):
        assert h in exposed


@pytest.mark.parametrize("key,path", [
    ("POST generate nonstream", "/olla/ollama/api/generate"),
    ("POST chat nonstream", "/olla/ollama/api/chat"),
])
async def test_proxied_response_headers_match_olla(key, path):
    olla = FIXTURE[key]
    async with cluster() as (c, head, fakes, log):
        r = await c.post(path, json=olla["request"])
    assert r.status_code == 200
    for h in ("X-Olla-Endpoint", "X-Olla-Backend-Type", "X-Olla-Model", "X-Olla-Request-Id",
              "X-Olla-Response-Time", "X-Olla-Routing-Decision", "X-Olla-Routing-Reason",
              "X-Olla-Routing-Strategy", "X-Ratelimit-Limit", "X-Ratelimit-Remaining", "X-Ratelimit-Reset",
              "Via", "X-Served-By"):
        assert h.lower() in r.headers, h
    assert r.headers["x-olla-model"] == olla["headers"]["X-Olla-Model"]
    assert r.headers["x-olla-backend-type"] == "ollama"
    assert r.headers["x-ratelimit-limit"] == olla["headers"]["X-Ratelimit-Limit"]


async def test_routing_headers_absent_without_model():
    """Olla only sets X-Olla-Model and X-Olla-Routing-* when the request named a model."""
    async with cluster() as (c, head, fakes, log):
        r = await c.get("/olla/ollama/api/ps")
    assert r.status_code == 200 and "x-olla-endpoint" in r.headers
    assert "x-olla-model" not in r.headers and "x-olla-routing-reason" not in r.headers


async def test_unknown_route_is_go_404():
    olla = FIXTURE["GET /nonexistent"]
    async with cluster() as (c, head, fakes, log):
        r = await c.get("/nonexistent")
    assert (r.status_code, r.text) == (404, olla["text"])


async def test_unknown_model_error_matches_olla():
    olla = FIXTURE["POST unknown model"]
    async with cluster() as (c, head, fakes, log):
        r = await c.post("/olla/ollama/api/generate", json=olla["request"])
    assert r.status_code == olla["status"] == 404
    assert r.text == "No ollama endpoints available\n"
    assert int(olla["headers"]["Content-Length"]) == len(r.content)


async def test_invalid_json_is_passed_to_the_backend():
    async with cluster() as (c, head, fakes, log):
        r = await c.post("/olla/ollama/api/generate", content=b"{not json",
                         headers={"content-type": "application/json"})
    assert r.status_code == FIXTURE["POST bad json"]["status"] == 400
    assert "error" in r.json()


@pytest.mark.parametrize("path", ["api/pull", "api/delete", "api/create", "api/copy", "api/push"])
async def test_model_management_is_refused_like_olla(path):
    async with cluster() as (c, head, fakes, log):
        r = await c.post(f"/olla/ollama/{path}", json={"model": "llama3.2:3b"})
    assert (r.status_code, r.text) == (501, "model management operations not supported by proxy\n")


async def test_rate_limit_429_matches_olla():
    over = {"server": {"rate_limits": {"per_ip_requests_per_minute": 2, "burst_size": 2}}}
    async with cluster(head_overrides=over) as (c, head, fakes, log):
        codes = [(await c.get("/olla/ollama/api/version")).status_code for _ in range(3)]
        r = await c.get("/olla/ollama/api/version")
        # /internal and locally answered routes are never limited.
        assert (await c.get("/internal/health")).status_code == 200
        assert (await c.get("/olla/ollama/api/tags")).status_code == 200
    assert codes[:2] == [200, 200] and r.status_code == 429
    assert r.text == "Too Many Requests\n"
    assert int(r.headers["retry-after"]) >= 1
    assert "x-olla-request-id" not in r.headers


async def test_oversized_body_is_413():
    over = {"server": {"request_limits": {"max_body_size": 100}}}
    async with cluster(head_overrides=over) as (c, head, fakes, log):
        r = await c.post("/olla/ollama/api/generate", content=b"x" * 500)
    assert (r.status_code, r.text) == (413, "Request body too large\n")


async def test_client_request_id_is_reused():
    async with cluster() as (c, head, fakes, log):
        r = await c.post("/olla/ollama/api/generate", headers={"X-Request-ID": "trace-123"},
                         json={"model": "llama3.2:3b", "prompt": "x", "stream": False})
    assert r.headers["x-olla-request-id"] == "trace-123"


async def test_access_log_fields_match_olla():
    olla_line = json.loads(FIXTURE["_log_samples"]["Access log"])
    async with cluster() as (c, head, fakes, log):
        await c.post("/olla/ollama/api/generate", json={"model": "llama3.2:3b", "prompt": "x", "stream": False})
    ours = [r for r in log.find("Access log") if r["path"] == "/api/generate"][-1]
    missing = set(olla_line) - set(ours) - {"level"}
    assert not missing, missing


@pytest.mark.parametrize("msg", ["Request completed", "Request dispatching", "Request received"])
async def test_request_log_fields_match_olla(msg):
    olla_line = json.loads(FIXTURE["_log_samples"][msg])
    async with cluster() as (c, head, fakes, log):
        await c.post("/olla/ollama/api/generate", json={"model": "llama3.2:3b", "prompt": "x", "stream": False})
    ours = [r for r in log.find(msg) if r.get("model")][-1]
    # "timestamp" is added by EventLog when writing; "compatible_endpoints" is an Olla internal.
    missing = set(olla_line) - set(ours) - {"level", "timestamp", "compatible_endpoints"}
    assert not missing, missing
