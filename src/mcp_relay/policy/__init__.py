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
- :class:`~mcp_relay.policy.local.LocalRulesPolicy` (in-process rules
  from a config file; no external service)

Third parties can subclass :class:`~mcp_relay.policy.client.PolicyClient`
to implement other strategies without forking the relay.
"""

from .client import (
    HttpPolicyClient,
    NoopPolicy,
    PolicyClient,
    PolicyClientError,
    build_policy_client,
)
from .local import LocalRulesPolicy, RuleSet, load_ruleset
from .models import (
    Decision,
    EvaluateRequest,
    EvaluateResponse,
    JSONRPC_POLICY_DENIED_CODE,
    PolicyDenied,
)

__all__ = [
    "Decision",
    "EvaluateRequest",
    "EvaluateResponse",
    "HttpPolicyClient",
    "JSONRPC_POLICY_DENIED_CODE",
    "LocalRulesPolicy",
    "NoopPolicy",
    "PolicyClient",
    "PolicyClientError",
    "PolicyDenied",
    "RuleSet",
    "build_policy_client",
    "load_ruleset",
]
