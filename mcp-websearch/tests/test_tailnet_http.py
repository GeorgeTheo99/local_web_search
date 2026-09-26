"""The optional Serve backend never grants the local diagnostic policy."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import http_server

HOST = "search.tail1234.ts.net"
PAYLOAD = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest.fixture
def client():
    with TestClient(
        http_server.build_app(tailnet_host=HOST),
        base_url=f"https://{HOST}", client=("127.0.0.1", 12345),
    ) as client:
        yield client


def test_tailnet_stateless_json_tools_list(client):
    response = client.post("/mcp", json=PAYLOAD, headers={"accept": "application/json"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in response.headers
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert {"web_search", "web_fetch"} <= names


def test_tailnet_direct_tool_call_without_initialization(client):
    # Invalid input avoids network/provider use but exercises real MCP dispatch.
    response = client.post("/mcp", headers={"accept": "application/json"}, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "web_fetch", "arguments": {"url": "http://127.0.0.1"}},
    })
    assert response.status_code == 200
    assert response.json()["id"] == 2
    assert "content" in response.json()["result"]


@pytest.mark.parametrize("path", ["/", "/ui", "/health", "/ready", "/live", "/stats", "/mcp/", "/mcp?x=1"])
def test_tailnet_no_diagnostics_or_extra_paths(client, path):
    assert client.post(path, json=PAYLOAD, follow_redirects=False).status_code == 404


@pytest.mark.parametrize("host", ["localhost:8889", "127.0.0.1:8890", "attacker.example", HOST + ":80", HOST + ".", HOST + "@localhost"])
def test_tailnet_no_local_host_bypass(client, host):
    assert client.post("/mcp", json=PAYLOAD, headers={"host": host}).status_code == 421
    assert client.get("/health", headers={"host": host}).status_code == 421


@pytest.mark.parametrize("origin", ["http://" + HOST, "https://attacker.example", "http://localhost:8889", "null", "https://" + HOST + "/"])
def test_tailnet_origin_denied(client, origin):
    assert client.post("/mcp", json=PAYLOAD, headers={"origin": origin}).status_code == 403


@pytest.mark.parametrize("suffix", ["", ":443"])
def test_tailnet_same_origin_accepted(client, suffix):
    response = client.post("/mcp", json=PAYLOAD, headers={
        "host": HOST + suffix, "origin": "https://" + HOST + suffix,
        "accept": "application/json",
    })
    assert response.status_code == 200


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "DELETE", "PUT", "PATCH"])
def test_tailnet_only_post(client, method):
    assert client.request(method, "/mcp", follow_redirects=False).status_code == 405


def test_tailnet_duplicate_headers_fail_closed(client):
    assert client.post("/mcp", json=PAYLOAD, headers=[("host", HOST), ("host", "localhost")]).status_code == 421
    assert client.post("/mcp", json=PAYLOAD, headers=[("origin", "https://" + HOST)] * 2).status_code == 403


def test_tailnet_cannot_trust_forwarded_headers(client):
    response = client.post("/mcp", json=PAYLOAD, headers={
        "host": "localhost", "x-forwarded-host": HOST,
        "x-forwarded-for": "127.0.0.1", "tailscale-user-login": "trusted@example.com",
    })
    assert response.status_code == 421


def test_tailnet_requires_actual_local_peer():
    with TestClient(http_server.build_app(tailnet_host=HOST), base_url=f"https://{HOST}", client=("100.64.0.1", 123)) as client:
        assert client.post("/mcp", json=PAYLOAD, headers={"x-forwarded-for": "127.0.0.1"}).status_code == 403


def test_default_local_app_still_rejects_tailnet_host():
    with TestClient(http_server.build_app(), base_url=f"https://{HOST}") as client:
        assert client.post("/mcp", json=PAYLOAD).status_code == 421


@pytest.mark.parametrize("host", ["", "*.tail123.ts.net", "https://search.tail123.ts.net", "search.tail123.ts.net:443", "search.example.com", "search.tail123.ts.net\n", "Search.tail123.ts.net", "-bad.tail123.ts.net", "a" * 64 + ".tail123.ts.net"])
def test_invalid_configuration_fails_at_startup(host):
    with pytest.raises(ValueError, match="MCP_TAILNET_HOST"):
        http_server.build_app(tailnet_host=host)
