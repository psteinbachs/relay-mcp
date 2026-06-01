"""Principal registry tests. Synthetic fixtures only."""

from __future__ import annotations

import pytest

from mcp_relay.principals import (
    Principal,
    PrincipalRegistry,
    PrincipalsError,
    build_principal_registry,
    load_principals,
)


# --- Principal ---------------------------------------------------------------


def test_allows_server_none_means_all():
    p = Principal(id="op", tokens=["t"], servers=None)
    assert p.allows_server("anything") is True


def test_allows_server_list_narrows():
    p = Principal(id="a", tokens=["t"], servers=["dns-mcp"])
    assert p.allows_server("dns-mcp") is True
    assert p.allows_server("compute-mcp") is False


# --- PrincipalRegistry -------------------------------------------------------


def test_registry_disabled_when_empty():
    reg = PrincipalRegistry([])
    assert reg.enabled is False
    assert reg.resolve("anything") is None


def test_registry_resolves_token():
    a = Principal(id="a", tokens=["tok-a"], servers=["dns-mcp"])
    b = Principal(id="b", tokens=["tok-b1", "tok-b2"])
    reg = PrincipalRegistry([a, b])
    assert reg.enabled is True
    assert reg.resolve("tok-a").id == "a"
    assert reg.resolve("tok-b1").id == "b"
    assert reg.resolve("tok-b2").id == "b"  # rotation: multiple tokens
    assert reg.resolve("nope") is None
    assert reg.resolve(None) is None
    assert reg.resolve("") is None


# --- load_principals ---------------------------------------------------------


def _write(tmp_path, text):
    p = tmp_path / "principals.yaml"
    p.write_text(text)
    return str(p)


def test_load_valid_with_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("TENANT_A_TOKEN", "secret-a")
    path = _write(
        tmp_path,
        "principals:\n"
        "  - id: tenant-a\n"
        "    tokens: ['${TENANT_A_TOKEN}']\n"
        "    servers: ['dns-mcp']\n"
        "  - id: operator\n"
        "    tokens: ['op-tok']\n",
    )
    ps = load_principals(path)
    assert ps[0].id == "tenant-a"
    assert ps[0].tokens == ["secret-a"]  # expanded
    assert ps[0].servers == ["dns-mcp"]
    assert ps[1].servers is None  # full surface


def test_load_rejects_duplicate_id(tmp_path):
    path = _write(
        tmp_path,
        "principals:\n"
        "  - id: a\n    tokens: ['x']\n"
        "  - id: a\n    tokens: ['y']\n",
    )
    with pytest.raises(PrincipalsError, match="duplicate principal id"):
        load_principals(path)


def test_load_rejects_shared_token(tmp_path):
    path = _write(
        tmp_path,
        "principals:\n"
        "  - id: a\n    tokens: ['same']\n"
        "  - id: b\n    tokens: ['same']\n",
    )
    with pytest.raises(PrincipalsError, match="shared between principals"):
        load_principals(path)


def test_load_rejects_unexpanded_token(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_TOK", raising=False)
    path = _write(
        tmp_path,
        "principals:\n  - id: a\n    tokens: ['${MISSING_TOK}']\n",
    )
    with pytest.raises(PrincipalsError, match="empty/unexpanded token"):
        load_principals(path)


def test_load_rejects_no_tokens(tmp_path):
    path = _write(tmp_path, "principals:\n  - id: a\n    tokens: []\n")
    with pytest.raises(PrincipalsError, match="no tokens"):
        load_principals(path)


def test_load_missing_file_raises():
    with pytest.raises(PrincipalsError, match="cannot read"):
        load_principals("/nonexistent/principals.yaml")


# --- build_principal_registry ------------------------------------------------


def test_build_disabled_without_env(monkeypatch):
    monkeypatch.delenv("PRINCIPALS_FILE", raising=False)
    reg = build_principal_registry()
    assert reg.enabled is False


def test_build_enabled_with_env(tmp_path, monkeypatch):
    path = _write(tmp_path, "principals:\n  - id: a\n    tokens: ['x']\n")
    monkeypatch.setenv("PRINCIPALS_FILE", path)
    reg = build_principal_registry()
    assert reg.enabled is True
    assert reg.resolve("x").id == "a"


# --- _resolve_principal (request glue, fail-closed) -------------------------


class _StubRequest:
    def __init__(self, authorization=None):
        self.headers = {} if authorization is None else {"authorization": authorization}


def test_resolve_principal_disabled_ignores_header(monkeypatch):
    import mcp_relay.main as main_mod

    monkeypatch.setattr(main_mod, "principal_registry", PrincipalRegistry([]))
    # 1:1 mode: even a bogus header is ignored, returns None.
    assert main_mod._resolve_principal(_StubRequest("Bearer whatever")) is None


def test_resolve_principal_valid_bearer(monkeypatch):
    import mcp_relay.main as main_mod

    reg = PrincipalRegistry([Principal(id="a", tokens=["tok-a"], servers=["dns-mcp"])])
    monkeypatch.setattr(main_mod, "principal_registry", reg)
    p = main_mod._resolve_principal(_StubRequest("Bearer tok-a"))
    assert p.id == "a"


def test_resolve_principal_unknown_token_401(monkeypatch):
    import mcp_relay.main as main_mod
    from fastapi import HTTPException

    reg = PrincipalRegistry([Principal(id="a", tokens=["tok-a"])])
    monkeypatch.setattr(main_mod, "principal_registry", reg)
    with pytest.raises(HTTPException) as exc:
        main_mod._resolve_principal(_StubRequest("Bearer wrong"))
    assert exc.value.status_code == 401


def test_resolve_principal_missing_header_401(monkeypatch):
    import mcp_relay.main as main_mod
    from fastapi import HTTPException

    reg = PrincipalRegistry([Principal(id="a", tokens=["tok-a"])])
    monkeypatch.setattr(main_mod, "principal_registry", reg)
    with pytest.raises(HTTPException) as exc:
        main_mod._resolve_principal(_StubRequest())
    assert exc.value.status_code == 401


def test_resolve_principal_tolerates_extra_whitespace(monkeypatch):
    import mcp_relay.main as main_mod

    reg = PrincipalRegistry([Principal(id="a", tokens=["tok-a"])])
    monkeypatch.setattr(main_mod, "principal_registry", reg)
    # collapsed whitespace + non-canonical casing still resolves
    assert main_mod._resolve_principal(_StubRequest("bearer   tok-a")).id == "a"


@pytest.mark.parametrize("header", ["Bearer", "Basic tok-a", "tok-a", "Bearer a b"])
def test_resolve_principal_malformed_header_401(monkeypatch, header):
    import mcp_relay.main as main_mod
    from fastapi import HTTPException

    reg = PrincipalRegistry([Principal(id="a", tokens=["tok-a"])])
    monkeypatch.setattr(main_mod, "principal_registry", reg)
    with pytest.raises(HTTPException) as exc:
        main_mod._resolve_principal(_StubRequest(header))
    assert exc.value.status_code == 401
