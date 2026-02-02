"""Pydantic models for MCP Relay API."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


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


class MCPServerUpdate(BaseModel):
    """Request to update an MCP server."""

    url: Optional[str] = None
    transport: Optional[TransportType] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    auth: Optional[AuthConfig] = None


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
