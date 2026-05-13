"""Content-policy client for relay-mcp.

Optional middleware that delegates content classification for proposed
tool calls to a pluggable backend. The default backend is a no-op
(every call passes), preserving full backward compatibility for
deployments that do not configure a policy.

Typical use: an operator wants to apply data-loss-prevention, output
redaction, or compliance filtering across all (or some) tool calls
routed through the relay, centrally managed in one place. Configure
``POLICY_BACKEND=http`` and point ``POLICY_API_URL`` at a
content-policy service implementing the v1 wire protocol.

Backends shipped:

- :class:`~mcp_relay.policy.client.NoopPolicy` (default)
- :class:`~mcp_relay.policy.client.HttpPolicyClient` (calls a remote
  policy service)

Third parties can subclass :class:`~mcp_relay.policy.client.PolicyClient`
to implement local-YAML, in-process, or other strategies without
forking the relay.
"""

from .client import (
    HttpPolicyClient,
    NoopPolicy,
    PolicyClient,
    PolicyClientError,
    build_policy_client,
)
from .models import Decision, EvaluateRequest, EvaluateResponse, PolicyDenied

__all__ = [
    "Decision",
    "EvaluateRequest",
    "EvaluateResponse",
    "HttpPolicyClient",
    "NoopPolicy",
    "PolicyClient",
    "PolicyClientError",
    "PolicyDenied",
    "build_policy_client",
]
