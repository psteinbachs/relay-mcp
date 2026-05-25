"""Tests that the relay forwards the MCP ``_meta`` request-params
field verbatim to the upstream CallTool.

``_meta`` is reserved by the MCP spec as a loose object for private
extensions — auth identity, trace ids, etc. — under reverse-DNS
namespaced keys. The relay treats it as opaque passthrough: it MUST
land on ``session.call_tool(..., meta=meta)`` unmodified, and a
``None`` value MUST collapse to the SDK's default ("no _meta on the
wire") rather than serialize as an explicit null.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

import mcp_relay.main as main_module


class _RecordingSession:
    """Minimal ``ClientSession`` stand-in that records every kwarg
    passed to ``call_tool``. Returns a dummy result whose ``content``
    is an empty list so the relay's response shaping doesn't break.
    """

    def __init__(self) -> None:
        self.last_meta_kwarg: Any = "<unset>"
        self.last_args: dict | None = None
        self.last_tool: str | None = None

    async def __aenter__(self) -> "_RecordingSession":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def initialize(self) -> None:
        return None

    async def call_tool(self, tool_name: str, arguments: dict, meta: Any = None):
        self.last_tool = tool_name
        self.last_args = arguments
        self.last_meta_kwarg = meta

        class _Result:
            content: list = []

        return _Result()


@asynccontextmanager
async def _fake_sse_client(_url: str):
    yield (object(), object())


def _patch_transports(monkeypatch: pytest.MonkeyPatch, session: _RecordingSession) -> None:
    monkeypatch.setattr(main_module, "sse_client", _fake_sse_client)

    def _fake_client_session(_read, _write):
        return session

    monkeypatch.setattr(main_module, "ClientSession", _fake_client_session)


@pytest.mark.asyncio
async def test_execute_tool_on_server_forwards_meta_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _RecordingSession()
    _patch_transports(monkeypatch, session)

    meta = {"io.example.private/identity": {"canonicalId": "user-abc"}}
    await main_module.execute_tool_on_server(
        {"url": "sse://upstream/", "transport": "sse"},
        "do_thing",
        {"x": 1},
        meta=meta,
    )

    assert session.last_tool == "do_thing"
    assert session.last_args == {"x": 1}
    # Identity passes byte-for-byte, no defensive deep copy / mutation.
    assert session.last_meta_kwarg is meta


@pytest.mark.asyncio
async def test_execute_tool_on_server_omits_meta_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """meta=None must reach call_tool as None (the SDK default), not
    as an empty dict — sending {} would put an explicit ``_meta: {}``
    on the wire and risk upstream servers tripping on it.
    """
    session = _RecordingSession()
    _patch_transports(monkeypatch, session)

    await main_module.execute_tool_on_server(
        {"url": "sse://upstream/", "transport": "sse"},
        "do_thing",
        {"x": 1},
    )

    assert session.last_meta_kwarg is None


@pytest.mark.asyncio
async def test_execute_tool_on_server_meta_kwarg_default_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit ``meta=None`` and omitting the kwarg must behave the
    same way. Locks the keyword default so a future refactor cannot
    silently flip it to ``{}``."""
    session = _RecordingSession()
    _patch_transports(monkeypatch, session)

    await main_module.execute_tool_on_server(
        {"url": "sse://upstream/", "transport": "sse"},
        "do_thing",
        {"x": 1},
        meta=None,
    )

    assert session.last_meta_kwarg is None
