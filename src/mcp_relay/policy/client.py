"""Policy client implementations.

A :class:`PolicyClient` evaluates a proposed tool call and either
returns an :class:`EvaluateResponse` or raises :class:`PolicyDenied`.
Callers decide whether to short-circuit (raise) or transform the
call (redact); typical relay usage is "raise on reject, swap fields
on redact, forward on pass".

The default :class:`NoopPolicy` makes the policy module a strict
opt-in: deployments that don't configure a policy backend see no
change in relay behavior.
"""

import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx

from .models import Decision, EvaluateRequest, EvaluateResponse

logger = logging.getLogger("relay-mcp.policy")


class PolicyClientError(RuntimeError):
    """Raised when the policy client cannot get a usable response.

    Distinct from :class:`PolicyDenied` (which is a successful
    evaluation that returned ``reject``). The relay should map this
    to fail-closed/fail-open per its configuration.
    """


class PolicyClient(ABC):
    """Abstract policy backend.

    Implementations should be safe to call concurrently. The relay
    may call ``evaluate`` from many request handlers in parallel.
    """

    @abstractmethod
    async def evaluate(self, req: EvaluateRequest) -> EvaluateResponse:
        """Evaluate a proposed tool call.

        Returns a structured decision. Implementations should NOT
        raise :class:`PolicyDenied` themselves — they return a
        response with ``decision=reject``. The relay's hook is
        responsible for translating that into the per-call action
        (raise vs swap fields vs forward).
        """


class NoopPolicy(PolicyClient):
    """Always passes. The default; preserves backward compatibility."""

    async def evaluate(self, req: EvaluateRequest) -> EvaluateResponse:
        return EvaluateResponse(
            decision=Decision.PASS,
            reasons=["noop"],
            policy_matched=False,
        )


class HttpPolicyClient(PolicyClient):
    """Calls a remote policy service over HTTP.

    Endpoint: ``POST {base_url}/v1/evaluate`` with a bearer token.
    The service must return the wire format defined in
    :mod:`mcp_relay.policy.models`.

    Failures (network, 5xx, malformed response) raise
    :class:`PolicyClientError`. Network timeouts are bounded by
    ``timeout``; the default is intentionally short — policy
    evaluation is on the hot path for tool calls.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 5.0,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._client = client  # injected for tests

    async def evaluate(self, req: EvaluateRequest) -> EvaluateResponse:
        url = f"{self._base_url}/v1/evaluate"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        body = req.model_dump()
        try:
            if self._client is not None:
                resp = await self._client.post(
                    url, json=body, headers=headers, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise PolicyClientError(f"policy API request failed: {e}") from e

        if resp.status_code >= 500:
            raise PolicyClientError(
                f"policy API returned {resp.status_code}: {resp.text[:200]}"
            )
        if resp.status_code == 401 or resp.status_code == 403:
            raise PolicyClientError(
                f"policy API auth failed ({resp.status_code}); "
                "check POLICY_API_TOKEN"
            )
        if resp.status_code != 200:
            raise PolicyClientError(
                f"policy API returned {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return EvaluateResponse.model_validate(resp.json())
        except (ValueError, TypeError) as e:
            raise PolicyClientError(f"policy API response invalid: {e}") from e


# --- env-driven construction ------------------------------------------------


def build_policy_client() -> PolicyClient:
    """Construct a policy client from environment variables.

    Environment variables (all optional; default is no-op):

    - ``POLICY_BACKEND`` — ``noop`` (default), ``http``
    - ``POLICY_API_URL`` — base URL for ``http`` backend
    - ``POLICY_API_TOKEN`` — bearer token for ``http`` backend
    - ``POLICY_TIMEOUT_SECONDS`` — request timeout (default ``5.0``)

    Raises :class:`PolicyClientError` if ``http`` is selected but
    required fields are missing.
    """
    backend = os.getenv("POLICY_BACKEND", "noop").lower()
    if backend == "noop" or backend == "":
        return NoopPolicy()
    if backend == "http":
        url = os.getenv("POLICY_API_URL", "").strip()
        token = os.getenv("POLICY_API_TOKEN", "").strip()
        if not url or not token:
            raise PolicyClientError(
                "POLICY_BACKEND=http requires POLICY_API_URL and POLICY_API_TOKEN"
            )
        timeout = float(os.getenv("POLICY_TIMEOUT_SECONDS", "5.0"))
        return HttpPolicyClient(base_url=url, token=token, timeout=timeout)
    raise PolicyClientError(f"unknown POLICY_BACKEND: {backend!r}")
