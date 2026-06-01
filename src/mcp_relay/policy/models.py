"""Wire-format schemas for policy evaluation.

These mirror the v1 protocol of a content-policy service:

- POST /v1/evaluate {tool_name, fields, trace_id?} ->
  {decision, reasons, redacted_fields?, audit_id, policy_matched}

The relay deliberately defines these locally rather than importing
from a particular policy-server package, so any HTTP service that
honors the wire format works as a backend.
"""

from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class Decision(str, Enum):
    PASS = "pass"
    REJECT = "reject"
    REDACT = "redact"


class EvaluateRequest(BaseModel):
    tool_name: str
    fields: dict[str, str] = Field(default_factory=dict)
    trace_id: Optional[str] = None
    # Resolved caller identity in multi-tenant (shared) mode; None in 1:1
    # mode. Backends may scope decisions per principal.
    principal: Optional[str] = None


class EvaluateResponse(BaseModel):
    decision: Decision
    reasons: list[str] = Field(default_factory=list)
    redacted_fields: Optional[dict[str, str]] = None
    audit_id: Optional[UUID] = None
    policy_matched: bool = True


class PolicyDenied(Exception):
    """Raised when a tool call is rejected by policy.

    Accepts either a plain string message (legacy callers — e.g. the
    per-server allow/block-list check) or a structured ``reasons``
    list with an optional ``audit_id`` (content-policy middleware).
    Both forms surface ``.reasons`` for the JSON-RPC error mapper.

    Mapped to JSON-RPC error code :data:`JSONRPC_POLICY_DENIED_CODE`
    (server-defined, in the implementation-defined -32000..-32099
    range per JSON-RPC 2.0 §5.1) so MCP clients can distinguish a
    policy denial from a generic internal error.
    """

    def __init__(self, reasons=None, audit_id: Optional[UUID] = None):
        if isinstance(reasons, str):
            self.reasons = [reasons]
        elif reasons is None:
            self.reasons = []
        else:
            self.reasons = list(reasons)
        self.audit_id = audit_id
        super().__init__(self._format())

    def _format(self) -> str:
        if not self.reasons:
            return "rejected by policy"
        return f"rejected by policy: {', '.join(self.reasons)}"


# JSON-RPC error code for "tool blocked by policy". Implementation-
# defined per JSON-RPC 2.0 §5.1 (servers may use any code in the
# -32000..-32099 range).
JSONRPC_POLICY_DENIED_CODE = -32001
