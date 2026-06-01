"""Policy client tests.

Synthetic fixtures only — no deployment-specific patterns.
"""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

import httpx
import pytest

from mcp_relay.policy import (
    Decision,
    EvaluateRequest,
    EvaluateResponse,
    HttpPolicyClient,
    NoopPolicy,
    PolicyClientError,
    PolicyDenied,
    build_policy_client,
)
from mcp_relay.policy.middleware import (
    _flatten_for_evaluation,
    enforce_policy,
)


# --- NoopPolicy -------------------------------------------------------------


async def test_noop_policy_always_passes():
    client = NoopPolicy()
    resp = await client.evaluate(
        EvaluateRequest(tool_name="anything", fields={"a": "b"})
    )
    assert resp.decision is Decision.PASS
    assert resp.policy_matched is False


# --- HttpPolicyClient with mocked transport --------------------------------


def _mock_transport(handler):
    return httpx.MockTransport(handler)


async def test_http_client_happy_pass():
    audit_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["tool_name"] == "plane-mcp__create_work_item"
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(
            200,
            json={
                "decision": "pass",
                "reasons": [],
                "audit_id": str(audit_id),
                "policy_matched": False,
            },
        )

    transport = _mock_transport(handler)
    async with httpx.AsyncClient(transport=transport) as inner:
        client = HttpPolicyClient(
            base_url="http://policy.test",
            token="test-token",
            client=inner,
        )
        resp = await client.evaluate(
            EvaluateRequest(tool_name="plane-mcp__create_work_item", fields={"body": "hi"})
        )
    assert resp.decision is Decision.PASS
    assert resp.audit_id == audit_id


async def test_http_client_reject():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "decision": "reject",
                "reasons": ["rule:test_label", "judge:restricted"],
                "audit_id": str(uuid4()),
                "policy_matched": True,
            },
        )

    transport = _mock_transport(handler)
    async with httpx.AsyncClient(transport=transport) as inner:
        client = HttpPolicyClient("http://policy.test", "t", client=inner)
        resp = await client.evaluate(
            EvaluateRequest(tool_name="x", fields={"body": "y"})
        )
    assert resp.decision is Decision.REJECT
    assert "rule:test_label" in resp.reasons


async def test_http_client_redact():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "decision": "redact",
                "reasons": ["rule:r"],
                "redacted_fields": {"arguments.body": "[redacted]"},
                "audit_id": str(uuid4()),
                "policy_matched": True,
            },
        )

    transport = _mock_transport(handler)
    async with httpx.AsyncClient(transport=transport) as inner:
        client = HttpPolicyClient("http://policy.test", "t", client=inner)
        resp = await client.evaluate(
            EvaluateRequest(tool_name="x", fields={"body": "y"})
        )
    assert resp.decision is Decision.REDACT
    assert resp.redacted_fields == {"arguments.body": "[redacted]"}


async def test_http_client_5xx_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    transport = _mock_transport(handler)
    async with httpx.AsyncClient(transport=transport) as inner:
        client = HttpPolicyClient("http://policy.test", "t", client=inner)
        with pytest.raises(PolicyClientError):
            await client.evaluate(EvaluateRequest(tool_name="x"))


async def test_http_client_auth_failure_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    transport = _mock_transport(handler)
    async with httpx.AsyncClient(transport=transport) as inner:
        client = HttpPolicyClient("http://policy.test", "t", client=inner)
        with pytest.raises(PolicyClientError, match="auth failed"):
            await client.evaluate(EvaluateRequest(tool_name="x"))


async def test_http_client_malformed_response_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"weird": "shape"})

    transport = _mock_transport(handler)
    async with httpx.AsyncClient(transport=transport) as inner:
        client = HttpPolicyClient("http://policy.test", "t", client=inner)
        with pytest.raises(PolicyClientError, match="invalid"):
            await client.evaluate(EvaluateRequest(tool_name="x"))


async def test_http_client_network_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    transport = _mock_transport(handler)
    async with httpx.AsyncClient(transport=transport) as inner:
        client = HttpPolicyClient("http://policy.test", "t", client=inner)
        with pytest.raises(PolicyClientError):
            await client.evaluate(EvaluateRequest(tool_name="x"))


# --- build_policy_client --------------------------------------------------


def test_build_default_is_noop(monkeypatch):
    monkeypatch.delenv("POLICY_BACKEND", raising=False)
    client = build_policy_client()
    assert isinstance(client, NoopPolicy)


def test_build_explicit_noop(monkeypatch):
    monkeypatch.setenv("POLICY_BACKEND", "noop")
    client = build_policy_client()
    assert isinstance(client, NoopPolicy)


def test_build_http(monkeypatch):
    monkeypatch.setenv("POLICY_BACKEND", "http")
    monkeypatch.setenv("POLICY_API_URL", "http://policy.test")
    monkeypatch.setenv("POLICY_API_TOKEN", "tok")
    client = build_policy_client()
    assert isinstance(client, HttpPolicyClient)


def test_build_http_missing_url_raises(monkeypatch):
    monkeypatch.setenv("POLICY_BACKEND", "http")
    monkeypatch.delenv("POLICY_API_URL", raising=False)
    monkeypatch.setenv("POLICY_API_TOKEN", "tok")
    with pytest.raises(PolicyClientError):
        build_policy_client()


def test_build_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("POLICY_BACKEND", "made-up")
    with pytest.raises(PolicyClientError):
        build_policy_client()


# --- middleware -----------------------------------------------------------


def test_flatten_handles_strings_ints_dicts():
    out = _flatten_for_evaluation(
        {"name": "hi", "count": 42, "nested": {"k": "v"}, "missing": None}
    )
    assert out["name"] == "hi"
    assert out["arguments.name"] == "hi"
    assert out["count"] == "42"
    assert "missing" not in out
    # Nested gets JSON-serialized so policy can match if it wants to
    assert "k" in out["nested"]


async def test_enforce_passes_arguments_on_pass():
    client = NoopPolicy()
    args = {"body": "ship it"}
    result = await enforce_policy(client, tool_name="x__y", arguments=args)
    assert result == args


async def test_enforce_raises_on_reject():
    class RejectingClient:
        async def evaluate(self, req):
            return EvaluateResponse(
                decision=Decision.REJECT,
                reasons=["rule:test"],
                policy_matched=True,
            )

    with pytest.raises(PolicyDenied) as exc:
        await enforce_policy(
            RejectingClient(), tool_name="x", arguments={"body": "y"}
        )
    assert "rule:test" in exc.value.reasons


async def test_enforce_applies_redactions():
    class RedactingClient:
        async def evaluate(self, req):
            return EvaluateResponse(
                decision=Decision.REDACT,
                reasons=["rule:r"],
                redacted_fields={"arguments.body": "[redacted]"},
                policy_matched=True,
            )

    result = await enforce_policy(
        RedactingClient(),
        tool_name="x",
        arguments={"body": "original content"},
    )
    assert result["body"] == "[redacted]"


async def test_enforce_fail_closed_by_default(monkeypatch):
    class FailingClient:
        async def evaluate(self, req):
            raise PolicyClientError("backend down")

    monkeypatch.delenv("POLICY_FAIL_OPEN", raising=False)
    with pytest.raises(PolicyDenied) as exc:
        await enforce_policy(
            FailingClient(), tool_name="x", arguments={"body": "y"}
        )
    assert any("fail_closed" in r for r in exc.value.reasons)


async def test_enforce_fail_open_when_configured(monkeypatch):
    class FailingClient:
        async def evaluate(self, req):
            raise PolicyClientError("backend down")

    monkeypatch.setenv("POLICY_FAIL_OPEN", "true")
    args = {"body": "y"}
    result = await enforce_policy(
        FailingClient(), tool_name="x", arguments=args
    )
    assert result == args


# --- span observability -----------------------------------------------------


async def test_enforce_records_decision_on_span():
    """The gate annotates the active span so it's auditable in traces."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    class PassClient:
        async def evaluate(self, req):
            return EvaluateResponse(
                decision=Decision.PASS, reasons=["local:allow"], policy_matched=True
            )

    with tracer.start_as_current_span("tools/call"):
        await enforce_policy(
            PassClient(), tool_name="infra-mcp__do", arguments={"id": "1"}
        )

    (span,) = exporter.get_finished_spans()
    assert span.attributes["policy.tool"] == "infra-mcp__do"
    assert span.attributes["policy.decision"] == "pass"
    assert span.attributes["policy.matched"] is True
    assert span.attributes["policy.reasons"] == "local:allow"
