"""Tests for per-server tool allow/block list filtering."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from mcp_relay.db import Database
from mcp_relay.main import _is_tool_allowed, _parse_tool_list
from mcp_relay.models import MCPServerCreate, MCPServerUpdate


# -- pure-function semantics --


def test_is_tool_allowed_no_lists_passes_everything():
    assert _is_tool_allowed("send_email", None, None) is True
    assert _is_tool_allowed("any_tool_name", None, None) is True


def test_is_tool_allowed_allowlist_admits_only_listed():
    allow = ["read_email", "list_mailboxes"]
    assert _is_tool_allowed("read_email", allow, None) is True
    assert _is_tool_allowed("list_mailboxes", allow, None) is True
    assert _is_tool_allowed("send_email", allow, None) is False


def test_is_tool_allowed_blocklist_rejects_listed_passes_rest():
    block = ["send_email", "send_draft"]
    assert _is_tool_allowed("send_email", None, block) is False
    assert _is_tool_allowed("send_draft", None, block) is False
    assert _is_tool_allowed("read_email", None, block) is True


def test_is_tool_allowed_both_lists_intersection():
    """Both lists set: tool must be in allowlist AND not in blocklist.
    Use case: allowlist your intended set, blocklist defends against
    a future upstream MCP adding a dangerous tool that happens to share
    a name with one you'd allowlisted earlier."""
    allow = ["read_email", "send_email", "list_mailboxes"]
    block = ["send_email"]
    assert _is_tool_allowed("read_email", allow, block) is True
    assert _is_tool_allowed("list_mailboxes", allow, block) is True
    assert _is_tool_allowed("send_email", allow, block) is False
    assert _is_tool_allowed("delete_mailbox", allow, block) is False


def test_is_tool_allowed_empty_allowlist_blocks_everything():
    """Explicit empty list is an explicit choice — distinct from None."""
    assert _is_tool_allowed("read_email", [], None) is False


def test_is_tool_allowed_empty_blocklist_blocks_nothing():
    assert _is_tool_allowed("send_email", None, []) is True


# -- _parse_tool_list parsing --


def test_parse_tool_list_handles_json_string():
    assert _parse_tool_list('["a", "b"]') == ["a", "b"]


def test_parse_tool_list_passes_through_lists():
    assert _parse_tool_list(["a", "b"]) == ["a", "b"]


def test_parse_tool_list_returns_none_for_null():
    assert _parse_tool_list(None) is None


def test_parse_tool_list_returns_none_for_garbage():
    assert _parse_tool_list("not json") is None
    assert _parse_tool_list('{"not": "a list"}') is None
    assert _parse_tool_list(42) is None


def test_parse_tool_list_preserves_empty_list():
    """Distinction between None (no filter) and [] (filter set, empty)
    must round-trip through JSON storage."""
    assert _parse_tool_list("[]") == []


# -- Pydantic validator on MCPServerCreate / MCPServerUpdate --


def test_create_validator_strips_whitespace():
    body = MCPServerCreate(
        name="srv",
        url="http://localhost:8000/sse",
        tool_allowlist=["  read_email  ", "list_emails\n"],
    )
    assert body.tool_allowlist == ["read_email", "list_emails"]


def test_create_validator_drops_empty_entries():
    """Empty / whitespace-only entries can never match a real tool —
    silently keeping them would create dead filter slots that confuse
    operators reading the saved config back."""
    body = MCPServerCreate(
        name="srv",
        url="http://localhost:8000/sse",
        tool_blocklist=["send_email", "", "   ", "send_draft"],
    )
    assert body.tool_blocklist == ["send_email", "send_draft"]


def test_create_validator_dedupes_in_order():
    body = MCPServerCreate(
        name="srv",
        url="http://localhost:8000/sse",
        tool_allowlist=["a", "b", "a", "c", "b"],
    )
    assert body.tool_allowlist == ["a", "b", "c"]


def test_create_validator_rejects_non_strings():
    with pytest.raises(ValidationError):
        MCPServerCreate(
            name="srv",
            url="http://localhost:8000/sse",
            tool_allowlist=["valid", 42],
        )


def test_create_validator_preserves_explicit_empty_list():
    """[] after normalization must stay [], not become None — empty
    allowlist is the 'block everything' explicit choice."""
    body = MCPServerCreate(
        name="srv",
        url="http://localhost:8000/sse",
        tool_allowlist=[],
    )
    assert body.tool_allowlist == []


def test_update_validator_applies_same_rules():
    body = MCPServerUpdate(tool_blocklist=["  send_email  ", "send_email"])
    assert body.tool_blocklist == ["send_email"]


def test_parse_tool_list_returns_none_for_non_array_json():
    """JSON storage may end up with a dict or string due to manual
    editing or migration bugs; we must fall through to no-filter
    (with a logged warning) rather than raising."""
    assert _parse_tool_list('{"send_email": true}') is None
    assert _parse_tool_list('"send_email"') is None
    assert _parse_tool_list("123") is None


# -- JSON-RPC policy-denied error code (-32001) --


async def test_blocked_tool_call_returns_policy_denied_code(tmp_path, monkeypatch):
    """A tools/call against a blocklisted tool must return JSON-RPC
    error code -32001 (policy denied), NOT -32603 (internal error).
    Clients use this code to distinguish 'try a different tool' from
    'something is broken on the server'.
    """
    from mcp_relay import main as relay_main

    # Stand up a real DB so _server_filters reads a true row.
    db_path = tmp_path / "policy.db"
    test_db = Database(database_url=f"sqlite:///{db_path}")
    await test_db.connect()
    await test_db.init_schema()
    await test_db.create_server(
        name="fastmail",
        url="http://example/sse",
        tool_blocklist=["send_email"],
    )

    # Wire the test DB into the module globals so _handle_mcp_request
    # consults our isolated state.
    monkeypatch.setattr(relay_main, "db", test_db)
    monkeypatch.setitem(
        relay_main.tool_cache,
        "fastmail",
        [{"name": "send_email", "description": "block me", "inputSchema": {}}],
    )
    relay_main._invalidate_filter_cache()

    session = relay_main.MCPSession(session_id=__import__("uuid").uuid4())
    response = await relay_main._handle_mcp_request(
        session,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "fastmail__send_email", "arguments": {}},
        },
    )

    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == relay_main.JSONRPC_POLICY_DENIED_CODE
    assert "allowlist or is blocked" in response["error"]["message"]

    # Cleanup module globals so other tests don't see leaked state.
    relay_main.tool_cache.pop("fastmail", None)
    relay_main._invalidate_filter_cache()
    await test_db.close()


# -- DB persistence + migration --


@pytest.fixture
async def db(tmp_path):
    """A fresh SQLite DB instance for each test."""
    db_path = tmp_path / "test.db"
    instance = Database(database_url=f"sqlite:///{db_path}")
    await instance.connect()
    await instance.init_schema()
    yield instance
    await instance.close()


async def test_create_server_persists_allowlist(db):
    row = await db.create_server(
        name="srv1",
        url="http://localhost:8001/sse",
        tool_allowlist=["read_a", "read_b"],
    )
    assert row is not None
    parsed = _parse_tool_list(row["tool_allowlist"])
    assert parsed == ["read_a", "read_b"]
    assert _parse_tool_list(row["tool_blocklist"]) is None


async def test_create_server_persists_blocklist(db):
    row = await db.create_server(
        name="srv2",
        url="http://localhost:8002/sse",
        tool_blocklist=["send_email", "send_draft"],
    )
    assert _parse_tool_list(row["tool_blocklist"]) == [
        "send_email", "send_draft",
    ]
    assert _parse_tool_list(row["tool_allowlist"]) is None


async def test_create_server_persists_both_lists(db):
    row = await db.create_server(
        name="srv3",
        url="http://localhost:8003/sse",
        tool_allowlist=["a", "b"],
        tool_blocklist=["c"],
    )
    assert _parse_tool_list(row["tool_allowlist"]) == ["a", "b"]
    assert _parse_tool_list(row["tool_blocklist"]) == ["c"]


async def test_create_server_no_lists_stores_null(db):
    """Default registration (no filtering) must leave the columns
    NULL, not empty-string or empty-array — we use NULL as the
    'no filter' sentinel."""
    row = await db.create_server(
        name="srv4",
        url="http://localhost:8004/sse",
    )
    assert row["tool_allowlist"] is None
    assert row["tool_blocklist"] is None


async def test_update_server_replaces_allowlist(db):
    await db.create_server(
        name="srv5",
        url="http://localhost:8005/sse",
        tool_allowlist=["old_tool"],
    )
    updated = await db.update_server(
        name="srv5",
        tool_allowlist=["new_tool_a", "new_tool_b"],
    )
    assert _parse_tool_list(updated["tool_allowlist"]) == [
        "new_tool_a", "new_tool_b",
    ]


async def test_update_server_clears_allowlist_when_flagged(db):
    """update_server's clear_tool_allowlist=True nukes an existing
    allowlist (returns to no-filter behavior). Distinct from passing
    tool_allowlist=None which means 'leave unchanged'."""
    await db.create_server(
        name="srv6",
        url="http://localhost:8006/sse",
        tool_allowlist=["x"],
    )
    updated = await db.update_server(
        name="srv6",
        clear_tool_allowlist=True,
    )
    assert updated["tool_allowlist"] is None


async def test_update_server_passing_none_leaves_unchanged(db):
    """Calling update with tool_allowlist=None must NOT clear an
    existing allowlist — None means 'don't touch this field'.
    Otherwise routine description-only updates would silently drop
    safety filtering."""
    await db.create_server(
        name="srv7",
        url="http://localhost:8007/sse",
        tool_allowlist=["keepme"],
    )
    updated = await db.update_server(
        name="srv7",
        description="just a description tweak",
    )
    assert _parse_tool_list(updated["tool_allowlist"]) == ["keepme"]


async def test_update_server_can_clear_blocklist_independently(db):
    """clear_tool_blocklist must work without touching the allowlist."""
    await db.create_server(
        name="srv8",
        url="http://localhost:8008/sse",
        tool_allowlist=["a"],
        tool_blocklist=["b"],
    )
    updated = await db.update_server(
        name="srv8",
        clear_tool_blocklist=True,
    )
    assert _parse_tool_list(updated["tool_allowlist"]) == ["a"]
    assert updated["tool_blocklist"] is None


async def test_init_schema_idempotent_on_existing_db(tmp_path):
    """ALTER TABLE ADD COLUMN path: a DB initialized once (with the
    new schema) and then re-initialized must not error or duplicate
    columns. This is the live-deploy upgrade path."""
    db_path = tmp_path / "rerun.db"
    db1 = Database(database_url=f"sqlite:///{db_path}")
    await db1.connect()
    await db1.init_schema()
    await db1.create_server(
        name="pre_upgrade",
        url="http://localhost:9000/sse",
        tool_allowlist=["only_this"],
    )
    await db1.close()

    # Re-open and re-init — emulates a fresh process boot post-deploy.
    db2 = Database(database_url=f"sqlite:///{db_path}")
    await db2.connect()
    await db2.init_schema()
    row = await db2.get_server("pre_upgrade")
    assert _parse_tool_list(row["tool_allowlist"]) == ["only_this"]
    await db2.close()


async def test_init_schema_migrates_legacy_db_without_allowlist_columns(
    tmp_path,
):
    """Older deploys created mcp_servers without tool_allowlist /
    tool_blocklist columns. init_schema must add the columns on the
    next boot without losing existing rows."""
    db_path = tmp_path / "legacy.db"

    # Manually create the pre-feature schema so we can verify migration.
    import aiosqlite
    legacy_schema = """
    CREATE TABLE mcp_servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        url TEXT NOT NULL,
        transport TEXT NOT NULL DEFAULT 'sse',
        description TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        status TEXT DEFAULT 'unknown',
        tools_count INTEGER DEFAULT 0,
        last_seen TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT,
        auth_config TEXT
    );
    """
    async with aiosqlite.connect(str(db_path)) as legacy_conn:
        await legacy_conn.executescript(legacy_schema)
        await legacy_conn.execute(
            "INSERT INTO mcp_servers (name, url, transport, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("oldsrv", "http://localhost:8000/sse", "sse", "2026-01-01T00:00:00"),
        )
        await legacy_conn.commit()

    # Now boot the upgraded DB layer against it.
    db_new = Database(database_url=f"sqlite:///{db_path}")
    await db_new.connect()
    await db_new.init_schema()

    # Existing row preserved + new columns NULL by default.
    row = await db_new.get_server("oldsrv")
    assert row is not None
    assert row["tool_allowlist"] is None
    assert row["tool_blocklist"] is None

    # Updates can now set the new columns.
    updated = await db_new.update_server(
        name="oldsrv",
        tool_blocklist=["dangerous_tool"],
    )
    assert _parse_tool_list(updated["tool_blocklist"]) == ["dangerous_tool"]
    await db_new.close()
