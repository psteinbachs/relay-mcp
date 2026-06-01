"""Principal identity for multi-tenant (shared) mode.

Optional. With **no** principals configured the relay is a single-tenant
(1:1) gateway and never inspects caller identity — fully backward
compatible. Configure ``PRINCIPALS_FILE`` to run one relay for many
tenants: each caller bearer token resolves to a :class:`Principal` that
gates the tool surface (``servers``) and feeds the policy backend
(resource scope keyed by ``principal.id``).

See ``docs/multi-tenancy.md``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class PrincipalsError(RuntimeError):
    """Raised when the principals config can't be loaded/validated.

    Raised at startup (in :func:`build_principal_registry`) so a bad
    config fails the relay fast rather than at first request.
    """


class Principal(BaseModel):
    """An identity plus what it's allowed to reach.

    ``servers=None`` means "all registered servers" (a full-surface
    principal, e.g. a trusted operator). A list narrows the visible/
    callable surface to those server names — composed with each
    registration's own ``tool_allowlist`` (the registration bounds what
    *any* caller may reach; the principal narrows it per tenant).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tokens: list[str] = Field(default_factory=list)
    servers: Optional[list[str]] = None

    def allows_server(self, server_name: str) -> bool:
        return self.servers is None or server_name in self.servers


class PrincipalsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principals: list[Principal] = Field(default_factory=list)


class PrincipalRegistry:
    """Resolves bearer tokens to principals. Safe for concurrent reads."""

    def __init__(self, principals: list[Principal]):
        self._principals = principals
        self._by_token: dict[str, Principal] = {}
        for p in principals:
            for t in p.tokens:
                self._by_token[t] = p

    @property
    def enabled(self) -> bool:
        """True iff principals are configured (i.e. shared mode is on)."""
        return bool(self._principals)

    def resolve(self, token: Optional[str]) -> Optional[Principal]:
        """Return the principal for ``token``, or None.

        Exact-match O(1) lookup. This is on the per-call hot path and
        must scale to many tenants, so it does not do a linear
        constant-time scan: bearer tokens are high-entropy secrets, and
        a dict lookup keyed by the full token does not leak the token
        the way comparing a single known secret against attacker input
        would. Use long random tokens (the validator enforces presence,
        not entropy — that's the operator's job)."""
        if not token:
            return None
        return self._by_token.get(token)


def load_principals(path: str) -> list[Principal]:
    """Load + validate principals from a YAML/JSON file.

    Token values undergo environment-variable expansion (``${VAR}``), so
    secrets live in the environment, not the file. Raises
    :class:`PrincipalsError` on a missing file, bad shape, empty/dup
    ids, or a token shared across principals.
    """
    try:
        text = Path(path).read_text()
    except OSError as e:
        raise PrincipalsError(f"cannot read PRINCIPALS_FILE {path!r}: {e}") from e

    try:
        import yaml

        data = yaml.safe_load(text)
    except yaml.YAMLError as e:  # type: ignore[name-defined]
        raise PrincipalsError(f"principals file {path!r} is not valid YAML/JSON: {e}") from e

    try:
        cfg = PrincipalsConfig.model_validate(data or {})
    except ValueError as e:
        raise PrincipalsError(f"principals file {path!r} failed validation: {e}") from e

    seen_ids: set[str] = set()
    seen_tokens: dict[str, str] = {}
    for p in cfg.principals:
        if not p.id.strip():
            raise PrincipalsError("principal id must be non-empty")
        if p.id in seen_ids:
            raise PrincipalsError(f"duplicate principal id: {p.id!r}")
        seen_ids.add(p.id)
        p.tokens = [os.path.expandvars(t) for t in p.tokens]
        if not p.tokens:
            raise PrincipalsError(f"principal {p.id!r} has no tokens")
        for t in p.tokens:
            if not t or t.startswith("${"):
                raise PrincipalsError(
                    f"principal {p.id!r} has an empty/unexpanded token "
                    "(is the env var set?)"
                )
            if t in seen_tokens:
                raise PrincipalsError(
                    f"token shared between principals {seen_tokens[t]!r} and {p.id!r}"
                )
            seen_tokens[t] = p.id
    return cfg.principals


def build_principal_registry() -> PrincipalRegistry:
    """Construct the registry from ``PRINCIPALS_FILE``. Unset ⇒ disabled
    (1:1 mode, no identity checks)."""
    path = os.getenv("PRINCIPALS_FILE", "").strip()
    if not path:
        return PrincipalRegistry([])
    return PrincipalRegistry(load_principals(path))
