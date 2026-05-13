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


class EvaluateResponse(BaseModel):
    decision: Decision
    reasons: list[str] = Field(default_factory=list)
    redacted_fields: Optional[dict[str, str]] = None
    audit_id: Optional[UUID] = None
    policy_matched: bool = True


class PolicyDenied(Exception):
    """Raised when a configured policy rejects a tool call.

    Carries the reasons returned by the backend so callers can pass
    them to clients in error responses. Reasons are opaque slugs —
    callers should not try to parse them for semantic meaning beyond
    distinguishing one rejection from another.
    """

    def __init__(self, reasons: list[str], audit_id: Optional[UUID] = None):
        self.reasons = reasons
        self.audit_id = audit_id
        super().__init__(self._format())

    def _format(self) -> str:
        if not self.reasons:
            return "rejected by policy"
        return f"rejected by policy: {', '.join(self.reasons)}"
