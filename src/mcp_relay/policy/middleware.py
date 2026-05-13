"""Integration helpers for using the policy client in the tool-call path.

This module is the place the rest of the relay codebase touches.
Hook into the existing ``tools/call`` handler at the same point the
allow/block check runs, just before forwarding to the backend MCP.

Typical hook (pseudocode)::

    from mcp_relay.policy.middleware import enforce_policy
    from mcp_relay.policy import PolicyDenied

    arguments = await enforce_policy(
        policy_client,
        tool_name=f"{server_name}__{actual_tool}",
        arguments=arguments,
    )
    # ... continue to execute_tool_on_server

The helper does three things:

1. Asks the policy client to evaluate the proposed call.
2. On ``reject``, raises :class:`~mcp_relay.policy.PolicyDenied` so
   the existing error-mapping path turns it into a JSON-RPC policy
   denial.
3. On ``redact``, returns a modified arguments dict with the
   redacted fields swapped in. Caller forwards with the redacted
   arguments; the audit log on the policy server records the
   redaction.

Configuration: ``POLICY_FAIL_OPEN=true`` makes transient policy
client errors return ``pass`` (the relay forwards unfiltered).
Default is fail-closed.
"""

import logging
import os
from typing import Any
from uuid import UUID

from .client import PolicyClient, PolicyClientError
from .models import Decision, EvaluateRequest, EvaluateResponse, PolicyDenied

logger = logging.getLogger("relay-mcp.policy")


def _policy_fail_open() -> bool:
    return os.getenv("POLICY_FAIL_OPEN", "false").lower() in ("true", "1", "yes")


def _flatten_for_evaluation(arguments: dict[str, Any]) -> dict[str, str]:
    """Flatten arguments into ``{path: str}`` for the policy wire format.

    Top-level string values become ``"<key>"`` and ``"arguments.<key>"``
    aliases (the relay can't tell which path-shape the policy expects,
    so it sends both). Non-string values are coerced via ``str()`` so
    nested objects show up but the wire stays simple.

    Per-deployment policies decide which path forms they match on.
    """
    out: dict[str, str] = {}
    for key, value in arguments.items():
        if isinstance(value, str):
            out[key] = value
            out[f"arguments.{key}"] = value
        elif isinstance(value, (int, float, bool)):
            out[key] = str(value)
            out[f"arguments.{key}"] = str(value)
        elif value is None:
            continue
        else:
            # Best-effort stringification for dicts/lists. Policies that
            # need to inspect structured fields can flatten further on
            # the policy-server side.
            try:
                import json

                rendered = json.dumps(value, default=str)
            except (TypeError, ValueError):
                rendered = str(value)
            out[key] = rendered
            out[f"arguments.{key}"] = rendered
    return out


async def enforce_policy(
    client: PolicyClient,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Run the policy and return arguments to forward.

    Returns the (possibly redacted) arguments dict. Raises
    :class:`PolicyDenied` on reject.

    On :class:`PolicyClientError` (network / server failure):
    fails closed by default — raises :class:`PolicyDenied` with a
    synthetic reason. Set ``POLICY_FAIL_OPEN=true`` to fail open
    (forward unfiltered, log a warning).
    """
    req = EvaluateRequest(
        tool_name=tool_name,
        fields=_flatten_for_evaluation(arguments),
        trace_id=trace_id,
    )
    try:
        resp = await client.evaluate(req)
    except PolicyClientError as e:
        if _policy_fail_open():
            logger.warning(
                f"policy backend error for {tool_name}, failing open: {e}"
            )
            return arguments
        logger.error(f"policy backend error for {tool_name}, failing closed: {e}")
        raise PolicyDenied(reasons=["fail_closed:policy_backend_unavailable"])

    if resp.decision is Decision.PASS:
        return arguments
    if resp.decision is Decision.REJECT:
        raise PolicyDenied(reasons=resp.reasons, audit_id=resp.audit_id)
    if resp.decision is Decision.REDACT:
        return _apply_redactions(arguments, resp.redacted_fields or {})
    # Unknown decision — fail closed
    raise PolicyDenied(reasons=[f"unknown_decision:{resp.decision}"])


def _apply_redactions(
    arguments: dict[str, Any], redacted: dict[str, str]
) -> dict[str, Any]:
    """Swap redacted values back into the arguments dict.

    The policy server returns redactions keyed by the flattened path
    form it matched on (e.g. ``arguments.body`` or just ``body``).
    Map back to the top-level key the relay actually has.
    """
    out = dict(arguments)
    for path, value in redacted.items():
        key = path.split(".")[-1]
        if key in out:
            out[key] = value
    return out
