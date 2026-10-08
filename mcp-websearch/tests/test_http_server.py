from __future__ import annotations

import inspect

from starlette.testclient import TestClient

import http_server


def test_http_server_binding_is_loopback_constant():
    assert http_server.MCP_BIND_HOST == "127.0.0.1"
    source = inspect.getsource(http_server.main)
    assert "host=MCP_BIND_HOST" in source
    assert 'host="0.0.0.0"' not in source


def test_loopback_authority_validation():
    for value in ("127.0.0.1:8889", "localhost:8889", "[::1]:8889", "LOCALHOST"):
        assert http_server._loopback_authority(value)
    for value in (
        "example.com:8889",
        "localhost.example:8889",
        "127.0.0.2:8889",
        "user@localhost:8889",
        "localhost:invalid",
        "",
    ):
        assert not http_server._loopback_authority(value)


def test_loopback_origin_validation():
    for value in ("http://127.0.0.1:8889", "https://localhost:8889", "http://[::1]:8889"):
        assert http_server._loopback_origin(value)
    for value in (
        "https://example.com",
        "https://localhost.example",
        "null",
        "file://localhost",
        "http://user@localhost:8889",
        "http://localhost:8889/path",
        "http://localhost:8889?query=secret",
    ):
        assert not http_server._loopback_origin(value)


def test_ui_redirect_and_assets_are_served_with_security_headers():
    with TestClient(http_server.build_app(), base_url="http://127.0.0.1:8889") as client:
        redirect = client.get("/", follow_redirects=False)
        page = client.get("/ui")
        styles = client.get("/ui/styles.css")
        script = client.get("/ui/app.js")
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/ui"
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    assert "Local Search" in page.text
    assert styles.status_code == 200
    assert styles.headers["content-type"].startswith("text/css")
    assert styles.headers["cache-control"] == "no-store"
    assert styles.headers["x-content-type-options"] == "nosniff"
    assert script.status_code == 200
    assert script.headers["cache-control"] == "no-store"
    assert script.headers["x-content-type-options"] == "nosniff"
    # Relative URLs let the directory serve the UI under a path prefix.
    assert 'href="ui/styles.css"' in page.text
    assert 'src="ui/app.js"' in page.text
    assert "health: 'health'" in script.text
    assert "fetch('/" not in script.text
    assert "innerHTML" not in script.text


def test_ui_is_covered_by_loopback_guard():
    with TestClient(http_server.build_app(), base_url="http://127.0.0.1:8889") as client:
        bad_host = client.get("/ui", headers={"host": "attacker.example"})
        bad_origin = client.get("/ui/app.js", headers={"origin": "https://attacker.example"})
    assert bad_host.status_code == 421
    assert bad_origin.status_code == 403


def test_app_accepts_loopback_host_without_origin():
    with TestClient(http_server.build_app(), base_url="http://127.0.0.1:8889") as client:
        response = client.get("/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_app_rejects_non_loopback_host_and_origin():
    with TestClient(http_server.build_app(), base_url="http://127.0.0.1:8889") as client:
        bad_host = client.get("/live", headers={"host": "attacker.example"})
        bad_origin = client.get("/live", headers={"origin": "https://attacker.example"})
        good_origin = client.get("/live", headers={"origin": "http://localhost:8889"})
    assert bad_host.status_code == 421
    assert bad_origin.status_code == 403
    assert good_origin.status_code == 200


def test_mcp_route_is_covered_by_loopback_guard():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    with TestClient(http_server.build_app(), base_url="http://127.0.0.1:8889") as client:
        bad_host = client.post("/mcp", json=payload, headers={"host": "attacker.example"})
        bad_origin = client.post(
            "/mcp", json=payload, headers={"origin": "https://attacker.example"}
        )
    assert bad_host.status_code == 421
    assert bad_origin.status_code == 403
