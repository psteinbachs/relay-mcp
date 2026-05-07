"""Tests for the Resources API.

Resources are MCP's read-only counterpart to tools. The relay
synthesizes ``gateway://capabilities`` from its tool cache + server
registry; agents read it once instead of paraphrasing
``discover_tools`` queries to enumerate what's available.
"""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_relay import main as relay_main
from mcp_relay.db import Database


@pytest.fixture
async def populated_db(tmp_path, monkeypatch):
    """A relay configured with two upstream servers — one healthy
    with a small tool cache, one with a blocklist that should hide
    a tool from the capabilities snapshot."""
    db_path = tmp_path / "resources.db"
    test_db = Database(database_url=f"sqlite:///{db_path}")
    await test_db.connect()
    await test_db.init_schema()

    await test_db.create_server(
        name="weather",
        url="http://example/sse",
        description="Weather lookups",
    )
    # Synthetic 'healthy' status so the snapshot reflects realistic
    # canary output (the test bypasses the real probe loop).
    await test_db.update_server_status("weather", "healthy", tools_count=2)

    await test_db.create_server(
        name="fastmail",
        url="http://example/sse",
        description="Email surface",
        tool_blocklist=["send_email"],
    )
    await test_db.update_server_status("fastmail", "healthy", tools_count=2)

    monkeypatch.setattr(relay_main, "db", test_db)
    monkeypatch.setitem(
        relay_main.tool_cache,
        "weather",
        [
            {"name": "get_forecast", "description": "Forecast", "inputSchema": {}},
            {"name": "get_current", "description": "Current", "inputSchema": {}},
        ],
    )
    monkeypatch.setitem(
        relay_main.tool_cache,
        "fastmail",
        [
            {"name": "read_email", "description": "Read", "inputSchema": {}},
            {"name": "send_email", "description": "Blocked", "inputSchema": {}},
        ],
    )
    relay_main._invalidate_filter_cache()

    yield test_db

    relay_main.tool_cache.pop("weather", None)
    relay_main.tool_cache.pop("fastmail", None)
    relay_main._invalidate_filter_cache()
    await test_db.close()


@pytest.fixture
async def client(populated_db):
    transport = ASGITransport(app=relay_main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# -- HTTP API --


async def test_list_resources_returns_gateway_capabilities(client):
    """``gateway://capabilities`` must always be present so any agent
    has at least one resource to read."""
    response = await client.get("/api/resources")
    assert response.status_code == 200
    body = response.json()
    uris = [r["uri"] for r in body["resources"]]
    assert "gateway://capabilities" in uris


async def test_list_resources_uses_camelcase_mime_type(client):
    """The list response must use the MCP-spec ``mimeType`` casing
    so MCP clients can consume the same payload across HTTP and the
    MCP-protocol path without a remap step."""
    response = await client.get("/api/resources")
    body = response.json()
    cap = next(r for r in body["resources"] if r["uri"] == "gateway://capabilities")
    assert cap["mimeType"] == "application/json"
    assert cap["server"] == "gateway"
    assert "mime_type" not in cap


async def test_read_capabilities_returns_server_inventory(client):
    """The capabilities snapshot must list every registered server
    with status, tool count, and a tool-name sample — that's the
    payload that lets an agent skip a discover_tools spiral."""
    response = await client.post(
        "/api/resources/read",
        json={"uri": "gateway://capabilities"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["server"] == "gateway"
    assert payload["uri"] == "gateway://capabilities"

    contents = payload["contents"]
    assert len(contents) == 1
    body = json.loads(contents[0]["text"])

    server_names = {s["server"] for s in body["servers"]}
    assert server_names == {"weather", "fastmail"}

    weather = next(s for s in body["servers"] if s["server"] == "weather")
    assert weather["tool_count"] == 2
    assert set(weather["sample_tools"]) == {"get_forecast", "get_current"}
    assert weather["tool_prefix"] == "weather__"


async def test_read_capabilities_applies_blocklist(client):
    """A tool hidden by a server's blocklist must not appear in the
    capabilities snapshot — otherwise agents see something they
    can't actually invoke and thrash on the inevitable failure."""
    response = await client.post(
        "/api/resources/read",
        json={"uri": "gateway://capabilities"},
    )
    body = json.loads(response.json()["contents"][0]["text"])
    fastmail = next(s for s in body["servers"] if s["server"] == "fastmail")
    assert fastmail["tool_count"] == 1
    assert fastmail["sample_tools"] == ["read_email"]
    assert "send_email" not in fastmail["sample_tools"]


async def test_read_capabilities_summary_matches_servers(client):
    """The top-level summary must reconcile with the per-server
    counts — drift here indicates a filtering bug between the
    summary stage and the per-server stage."""
    response = await client.post(
        "/api/resources/read",
        json={"uri": "gateway://capabilities"},
    )
    body = json.loads(response.json()["contents"][0]["text"])
    expected_total = sum(s["tool_count"] for s in body["servers"])
    assert body["summary"]["total_servers"] == len(body["servers"])
    assert body["summary"]["total_tools"] == expected_total


async def test_read_capabilities_sample_is_deterministic(client):
    """sample_tools must be alphabetical so an agent caching the
    snapshot gets stable output across requests — otherwise the
    snapshot's diff churn is noisy."""
    r1 = await client.post(
        "/api/resources/read", json={"uri": "gateway://capabilities"}
    )
    r2 = await client.post(
        "/api/resources/read", json={"uri": "gateway://capabilities"}
    )
    b1 = json.loads(r1.json()["contents"][0]["text"])
    b2 = json.loads(r2.json()["contents"][0]["text"])
    for s1, s2 in zip(b1["servers"], b2["servers"]):
        assert s1["sample_tools"] == s2["sample_tools"]
        assert s1["sample_tools"] == sorted(s1["sample_tools"])


async def test_read_resource_unknown_uri_returns_404(client):
    response = await client.post(
        "/api/resources/read",
        json={"uri": "obsidian://capabilities"},
    )
    assert response.status_code == 404


async def test_read_resource_missing_uri_returns_400(client):
    response = await client.post("/api/resources/read", json={})
    assert response.status_code == 400


# -- MCP-protocol path --


async def test_initialize_advertises_resources_capability(populated_db):
    """``initialize`` must advertise ``resources`` under capabilities
    so MCP clients (Claude Code, etc.) know they can call
    ``resources/list``. Without this, a spec-compliant client will
    skip resources entirely."""
    session = relay_main.MCPSession(session_id=uuid.uuid4())
    response = await relay_main._handle_mcp_request(
        session,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    caps = response["result"]["capabilities"]
    assert "resources" in caps
    assert caps["resources"] == {"subscribe": False, "listChanged": False}
    # Existing tools capability must not regress.
    assert "tools" in caps


async def test_resources_list_via_mcp_protocol(populated_db):
    session = relay_main.MCPSession(session_id=uuid.uuid4())
    response = await relay_main._handle_mcp_request(
        session,
        {"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}},
    )
    resources = response["result"]["resources"]
    uris = [r["uri"] for r in resources]
    assert "gateway://capabilities" in uris
    cap = next(r for r in resources if r["uri"] == "gateway://capabilities")
    # MCP protocol uses camelCase for mimeType.
    assert cap["mimeType"] == "application/json"


async def test_resources_read_via_mcp_protocol(populated_db):
    session = relay_main.MCPSession(session_id=uuid.uuid4())
    response = await relay_main._handle_mcp_request(
        session,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": "gateway://capabilities"},
        },
    )
    contents = response["result"]["contents"]
    assert len(contents) == 1
    assert contents[0]["uri"] == "gateway://capabilities"
    body = json.loads(contents[0]["text"])
    assert "servers" in body and "summary" in body


async def test_resources_read_unknown_uri_returns_jsonrpc_error(populated_db):
    session = relay_main.MCPSession(session_id=uuid.uuid4())
    response = await relay_main._handle_mcp_request(
        session,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/read",
            "params": {"uri": "nope://nowhere"},
        },
    )
    assert "error" in response
    assert response["error"]["code"] == -32603
    assert "Resource not found" in response["error"]["message"]


async def test_resources_read_missing_uri_returns_error(populated_db):
    session = relay_main.MCPSession(session_id=uuid.uuid4())
    response = await relay_main._handle_mcp_request(
        session,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "resources/read",
            "params": {},
        },
    )
    assert "error" in response
    assert "uri is required" in response["error"]["message"]


async def test_resources_read_with_null_params_returns_clean_error(populated_db):
    """JSON-RPC clients may send ``"params": null`` explicitly.
    The dispatcher must collapse that to ``{}`` before downstream
    ``params.get(...)`` runs, otherwise the agent sees an opaque
    AttributeError instead of a recoverable 'uri is required'.
    """
    session = relay_main.MCPSession(session_id=uuid.uuid4())
    response = await relay_main._handle_mcp_request(
        session,
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "resources/read",
            "params": None,
        },
    )
    assert "error" in response
    assert "uri is required" in response["error"]["message"]
    # Must not be the AttributeError surfaced as a generic internal
    # message — the cleanup is what makes this recoverable.
    assert "NoneType" not in response["error"]["message"]


# -- Snapshot completeness: zero-tool / un-cached servers --


async def test_capabilities_includes_registered_server_without_cached_tools(
    tmp_path, monkeypatch
):
    """A server registered in the DB but not yet probed (or whose
    discovery legitimately returned zero tools) must still appear in
    the snapshot. Hiding it would silently under-report the surface
    relative to /api/servers and make summary.total_servers drift.
    """
    db_path = tmp_path / "empty.db"
    test_db = Database(database_url=f"sqlite:///{db_path}")
    await test_db.connect()
    await test_db.init_schema()

    await test_db.create_server(
        name="never_probed",
        url="http://example/sse",
        description="Just registered; discovery hasn't run yet",
    )
    monkeypatch.setattr(relay_main, "db", test_db)
    # Crucially: do NOT seed tool_cache for this server.
    relay_main._invalidate_filter_cache()

    transport = ASGITransport(app=relay_main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.post(
            "/api/resources/read", json={"uri": "gateway://capabilities"}
        )
        assert response.status_code == 200
        body = json.loads(response.json()["contents"][0]["text"])
        names = [s["server"] for s in body["servers"]]
        assert "never_probed" in names
        entry = next(s for s in body["servers"] if s["server"] == "never_probed")
        assert entry["tool_count"] == 0
        assert entry["sample_tools"] == []
        assert entry["description"] == "Just registered; discovery hasn't run yet"
        # summary.total_servers must reflect the registered set, not
        # the cache subset.
        assert body["summary"]["total_servers"] >= 1

    relay_main._invalidate_filter_cache()
    await test_db.close()
