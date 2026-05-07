"""Pydantic models for MCP Relay API."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


def _validate_tool_list(v: Optional[list[str]]) -> Optional[list[str]]:
    """Normalize a tool allowlist/blocklist: strip whitespace,
    drop empties, dedupe in original order, reject non-strings.

    Empty list (after normalization) is preserved as an explicit
    choice — distinct from None (no filter set). Whitespace-only
    entries collapse to nothing and are dropped, NOT preserved as
    "" — empty tool names can't match any real tool, so they'd
    create silent dead filters.
    """
    if v is None:
        return None
    seen: set[str] = set()
    out: list[str] = []
    for entry in v:
        if not isinstance(entry, str):
            raise ValueError(
                f"tool list entries must be strings, got {type(entry).__name__}"
            )
        cleaned = entry.strip()
        if not cleaned:
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


class TransportType(str, Enum):
    """MCP transport types."""

    SSE = "sse"
    HTTP = "http"  # streamable-http
    STDIO = "stdio"


class ServerStatus(str, Enum):
    """Server health status."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class AuthConfig(BaseModel):
    """Authentication configuration for an MCP server."""

    type: str = Field(..., description="Auth type: 'basic', 'bearer', or 'header'")
    username: Optional[str] = Field(None, description="Username for basic auth")
    password: Optional[str] = Field(None, description="Password for basic auth")
    token: Optional[str] = Field(None, description="Token for bearer auth")
    header_name: Optional[str] = Field(None, description="Custom header name")
    header_value: Optional[str] = Field(None, description="Custom header value")


class MCPServerCreate(BaseModel):
    """Request to register a new MCP server."""

    name: str = Field(
        ..., min_length=1, max_length=100, description="Unique server name"
    )
    url: str = Field(..., description="Server URL (e.g., http://host:port/sse)")
    transport: TransportType = Field(
        default=TransportType.SSE, description="Transport type"
    )
    description: Optional[str] = Field(None, max_length=500)
    enabled: bool = Field(default=True)
    auth: Optional[AuthConfig] = Field(None, description="Authentication config")
    tool_allowlist: Optional[list[str]] = Field(
        None,
        description=(
            "If set, only these tool names (the server's local names, not "
            "the prefixed `server__tool` form) are exposed to clients. "
            "Use to whitelist a small set of safe tools from a permissive "
            "upstream MCP. Combine with tool_blocklist for defense-in-depth."
        ),
    )
    tool_blocklist: Optional[list[str]] = Field(
        None,
        description=(
            "If set, these tool names (local) are hidden from clients and "
            "tools/call invocations are rejected. Use to suppress dangerous "
            "tools while keeping the rest of the upstream surface."
        ),
    )

    _validate_allowlist = field_validator("tool_allowlist")(_validate_tool_list)
    _validate_blocklist = field_validator("tool_blocklist")(_validate_tool_list)


class MCPServer(BaseModel):
    """Registered MCP server."""

    id: int
    name: str
    url: str
    transport: TransportType
    description: Optional[str] = None
    enabled: bool = True
    status: ServerStatus = ServerStatus.UNKNOWN
    tools_count: int = 0
    last_seen: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    auth: Optional[AuthConfig] = None
    tool_allowlist: Optional[list[str]] = None
    tool_blocklist: Optional[list[str]] = None


class MCPServerUpdate(BaseModel):
    """Request to update an MCP server.

    Field semantics: omit a field to leave it unchanged. Pydantic +
    JSON cannot distinguish "field omitted" from "field explicitly
    null" — both arrive as ``None`` — so this API treats both
    identically as 'no change'. To explicitly CLEAR
    ``tool_allowlist`` / ``tool_blocklist`` (revert to no-filtering
    on that side), set the matching ``clear_*`` flag. Forcing the
    operator to opt in via a separate flag prevents a routine
    description-only PATCH from silently stripping safety filtering.
    """

    url: Optional[str] = None
    transport: Optional[TransportType] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    auth: Optional[AuthConfig] = None
    tool_allowlist: Optional[list[str]] = None
    tool_blocklist: Optional[list[str]] = None
    clear_tool_allowlist: bool = Field(
        default=False,
        description="Set true to clear an existing allowlist (revert to no-allowlist filtering).",
    )
    clear_tool_blocklist: bool = Field(
        default=False,
        description="Set true to clear an existing blocklist.",
    )

    _validate_allowlist = field_validator("tool_allowlist")(_validate_tool_list)
    _validate_blocklist = field_validator("tool_blocklist")(_validate_tool_list)


class MCPTool(BaseModel):
    """An MCP tool from an aggregated server."""

    name: str
    description: Optional[str] = None
    server: str
    input_schema: Optional[dict[str, Any]] = None


class ServerHealth(BaseModel):
    """Health check result for a server."""

    name: str
    status: ServerStatus
    latency_ms: Optional[float] = None
    tools_count: int = 0
    error: Optional[str] = None
    checked_at: datetime


class AggregatorStats(BaseModel):
    """Overall aggregator statistics."""

    total_servers: int
    healthy_servers: int
    total_tools: int
    servers: list[MCPServer]


class Resource(BaseModel):
    """An MCP Resource — read-only context exposed by a server.

    Resources let an agent pull a snapshot of state (capability
    catalogs, structure overviews, status pages) without invoking
    a tool. Addressed by URI, with the URI scheme typically matching
    the owning server. Field names match the MCP spec
    (resources/list response items) so this model is usable in both
    the HTTP API and the MCP-protocol path without remapping.
    """

    uri: str = Field(..., description="Resource URI (e.g. 'gateway://capabilities')")
    name: str = Field(..., description="Human-readable name")
    description: Optional[str] = Field(None, description="What this resource exposes")
    mime_type: str = Field(
        default="application/json",
        alias="mimeType",
        description="MIME type of the resource content",
    )
    server: str = Field(
        ...,
        description=(
            "Server that owns this resource. 'gateway' for relay-synthesized "
            "resources (e.g. capability catalog); otherwise the registered "
            "upstream MCP server name."
        ),
    )

    model_config = {"populate_by_name": True}


class ResourceListResponse(BaseModel):
    """Response shape for GET /api/resources."""

    resources: list[Resource]
