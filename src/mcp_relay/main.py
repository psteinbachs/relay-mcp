"""MCP Relay - Main FastAPI application."""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

import anyio
import httpx
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from sse_starlette import EventSourceResponse

from mcp_relay.db import Database
from mcp_relay.discovery import DiscoveryService
from mcp_relay.models import (
    AggregatorStats,
    MCPServer,
    MCPServerCreate,
    MCPServerUpdate,
    MCPTool,
    ServerHealth,
    ServerStatus,
    TransportType,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mcp-relay")

# Global state
db: Optional[Database] = None
tool_cache: dict[str, list[dict]] = {}  # server_name -> tools
cache_lock = asyncio.Lock()
discovery_service: Optional[DiscoveryService] = None

# MCP SSE session management
mcp_sessions: dict[UUID, "MCPSession"] = {}
sessions_lock = asyncio.Lock()


class MCPSession:
    """Represents an active MCP SSE session."""

    def __init__(self, session_id: UUID):
        self.session_id = session_id
        self.created_at = datetime.now(timezone.utc)
        # Channel for sending responses back to client via SSE
        self.response_send: MemoryObjectSendStream[dict[str, Any]]
        self.response_recv: MemoryObjectReceiveStream[dict[str, Any]]
        self.response_send, self.response_recv = anyio.create_memory_object_stream(32)
        self._closed = False

    async def send_response(self, response: dict[str, Any]):
        """Send a JSON-RPC response to the client."""
        if not self._closed:
            try:
                await self.response_send.send(response)
            except anyio.ClosedResourceError:
                self._closed = True

    async def close(self):
        """Close the session."""
        self._closed = True
        await self.response_send.aclose()


# Canary configuration
CANARY_ENABLED = os.getenv("CANARY_ENABLED", "true").lower() in ("true", "1", "yes")
CANARY_INTERVAL = int(os.getenv("CANARY_INTERVAL", "60"))  # seconds
CANARY_TIMEOUT = int(os.getenv("CANARY_TIMEOUT", "10"))  # seconds
CANARY_WEBHOOK_URL = os.getenv("CANARY_WEBHOOK_URL", "")  # alerts webhook
canary_task: Optional[asyncio.Task] = None
last_health_results: dict[str, ServerHealth] = {}  # server_name -> last result
previous_status: dict[str, ServerStatus] = {}  # track status changes for alerts


def init_telemetry():
    """Initialize OpenTelemetry if configured."""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {SERVICE_NAME: os.getenv("OTEL_SERVICE_NAME", "mcp-relay")}
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
        )
        trace.set_tracer_provider(provider)
        logger.info(f"OpenTelemetry initialized: {endpoint}")
        return FastAPIInstrumentor
    except ImportError as e:
        logger.warning(f"OpenTelemetry not available: {e}")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown."""
    global db, discovery_service, canary_task
    db = Database()
    await db.connect()
    await db.init_schema()

    # Initialize discovery service (loads sentence transformer model)
    discovery_service = DiscoveryService()

    # Initial tool discovery with retry (servers may not be ready immediately)
    asyncio.create_task(refresh_all_tools(retry_on_empty=True, max_retries=5))

    # Start canary background task if enabled
    if CANARY_ENABLED:
        canary_task = asyncio.create_task(canary_loop())
        logger.info(
            f"Canary monitoring started: interval={CANARY_INTERVAL}s, "
            f"timeout={CANARY_TIMEOUT}s, webhook={'configured' if CANARY_WEBHOOK_URL else 'none'}"
        )

    yield

    # Stop canary task
    if canary_task:
        canary_task.cancel()
        try:
            await canary_task
        except asyncio.CancelledError:
            pass

    await db.close()


app = FastAPI(
    title="MCP Relay",
    description="Minimal MCP server relay with REST API",
    version="0.1.0",
    lifespan=lifespan,
)

# Initialize OTel
otel_instrumentor = init_telemetry()
if otel_instrumentor:
    otel_instrumentor().instrument_app(app)


# =============================================================================
# Health & Stats
# =============================================================================


@app.get("/health")
async def health_check():
    """Basic health check."""
    return {"status": "healthy", "service": "mcp-relay"}


@app.get("/api/stats", response_model=AggregatorStats)
async def get_stats():
    """Get relay statistics."""
    servers = await db.list_servers()
    healthy = sum(1 for s in servers if s.get("status") == "healthy")
    total_tools = sum(s.get("tools_count", 0) for s in servers)

    return AggregatorStats(
        total_servers=len(servers),
        healthy_servers=healthy,
        total_tools=total_tools,
        servers=[_db_to_server(s) for s in servers],
    )


@app.get("/api/health/summary")
async def health_summary():
    """Get health summary of all servers (canary results).

    Returns aggregated health status from the background canary checks.
    """
    servers = await db.list_servers(enabled_only=True)
    total = len(servers)
    healthy = sum(1 for s in servers if s.get("status") == "healthy")
    unhealthy = sum(1 for s in servers if s.get("status") == "unhealthy")

    # Get detailed results from last canary run
    results = []
    for name, health in last_health_results.items():
        results.append(
            {
                "name": health.name,
                "status": health.status.value,
                "latency_ms": health.latency_ms,
                "tools_count": health.tools_count,
                "error": health.error,
                "checked_at": health.checked_at.isoformat()
                if health.checked_at
                else None,
            }
        )

    return {
        "status": "healthy"
        if unhealthy == 0
        else "degraded"
        if healthy > 0
        else "unhealthy",
        "total_servers": total,
        "healthy_servers": healthy,
        "unhealthy_servers": unhealthy,
        "canary_enabled": CANARY_ENABLED,
        "canary_interval": CANARY_INTERVAL,
        "servers": results,
    }


@app.get("/api/canary/config")
async def canary_config():
    """Get canary configuration."""
    return {
        "enabled": CANARY_ENABLED,
        "interval_seconds": CANARY_INTERVAL,
        "timeout_seconds": CANARY_TIMEOUT,
        "webhook_configured": bool(CANARY_WEBHOOK_URL),
    }


@app.post("/api/canary/run")
async def run_canary_now():
    """Trigger an immediate canary check cycle."""
    results = await run_canary_check()
    healthy = sum(1 for r in results if r.status == ServerStatus.HEALTHY)
    return {
        "checked": len(results),
        "healthy": healthy,
        "unhealthy": len(results) - healthy,
        "results": [
            {
                "name": r.name,
                "status": r.status.value,
                "latency_ms": r.latency_ms,
                "error": r.error,
            }
            for r in results
        ],
    }


# =============================================================================
# Server Management API
# =============================================================================


@app.get("/api/servers", response_model=list[MCPServer])
async def list_servers(enabled_only: bool = Query(False)):
    """List all registered MCP servers."""
    servers = await db.list_servers(enabled_only=enabled_only)
    return [_db_to_server(s) for s in servers]


@app.post("/api/servers", response_model=MCPServer, status_code=201)
async def register_server(body: MCPServerCreate):
    """Register a new MCP server."""
    existing = await db.get_server(body.name)
    if existing:
        raise HTTPException(
            status_code=409, detail=f"Server '{body.name}' already exists"
        )

    server = await db.create_server(
        name=body.name,
        url=body.url,
        transport=body.transport.value,
        description=body.description,
        enabled=body.enabled,
    )
    logger.info(f"Registered server: {body.name} -> {body.url}")

    # Trigger async tool discovery
    asyncio.create_task(discover_server_tools(body.name))

    return _db_to_server(server)


@app.get("/api/servers/{name}", response_model=MCPServer)
async def get_server(name: str):
    """Get a specific server."""
    server = await db.get_server(name)
    if not server:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    return _db_to_server(server)


@app.patch("/api/servers/{name}", response_model=MCPServer)
async def update_server(name: str, body: MCPServerUpdate):
    """Update a server registration."""
    server = await db.update_server(
        name=name,
        url=body.url,
        transport=body.transport.value if body.transport else None,
        description=body.description,
        enabled=body.enabled,
    )
    if not server:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

    # Re-discover tools if URL changed
    if body.url:
        asyncio.create_task(discover_server_tools(name))

    return _db_to_server(server)


@app.delete("/api/servers/{name}")
async def unregister_server(name: str):
    """Unregister a server."""
    deleted = await db.delete_server(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

    # Clear tool cache
    async with cache_lock:
        tool_cache.pop(name, None)

    logger.info(f"Unregistered server: {name}")
    return {"deleted": True, "name": name}


@app.get("/api/servers/{name}/health", response_model=ServerHealth)
async def check_server_health(name: str):
    """Check health of a specific server."""
    server = await db.get_server(name)
    if not server:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

    result = await probe_server(server)
    return result


@app.post("/api/servers/{name}/refresh")
async def refresh_server_tools(name: str):
    """Force refresh tools from a server."""
    server = await db.get_server(name)
    if not server:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

    tools = await discover_server_tools(name)
    return {"name": name, "tools_count": len(tools)}


# =============================================================================
# Tools API
# =============================================================================


@app.get("/api/tools", response_model=list[MCPTool])
async def list_tools(server: Optional[str] = Query(None)):
    """List all aggregated tools, optionally filtered by server."""
    async with cache_lock:
        if server:
            tools = tool_cache.get(server, [])
            return [
                MCPTool(
                    name=t["name"],
                    description=t.get("description"),
                    server=server,
                    input_schema=t.get("inputSchema"),
                )
                for t in tools
            ]

        all_tools = []
        for srv_name, tools in tool_cache.items():
            for t in tools:
                all_tools.append(
                    MCPTool(
                        name=t["name"],
                        description=t.get("description"),
                        server=srv_name,
                        input_schema=t.get("inputSchema"),
                    )
                )
        return all_tools


@app.post("/api/tools/refresh")
async def refresh_all():
    """Force refresh tools from all servers."""
    await refresh_all_tools()
    async with cache_lock:
        total = sum(len(tools) for tools in tool_cache.values())
    return {"refreshed": True, "total_tools": total}


@app.get("/api/tools/debug")
async def debug_tools():
    """Debug endpoint to check tool repository state."""
    async with cache_lock:
        all_tools = []
        for srv_name, tools in tool_cache.items():
            for t in tools:
                all_tools.append(
                    {
                        "id": f"{srv_name}__{t['name']}",
                        "name": f"{srv_name}__{t['name']}",
                    }
                )

    return {
        "tool_count": len(all_tools),
        "sample_tools": all_tools[:5],
    }


# =============================================================================
# Tool-Gating Compatible API (semantic discovery + execution)
# =============================================================================


@app.post("/api/tools/discover")
async def discover_tools(request: Request):
    """Discover relevant tools using semantic search.

    Compatible with tool-gating API for ToolGateway client.

    Request body:
        query: Natural language query
        tags: Optional list of server names to filter
        limit: Max results (default 10)

    Returns:
        tools: List of matching tools with scores
        query_id: Unique ID for this query
        timestamp: When query was processed
    """
    import uuid
    from datetime import datetime

    body = await request.json()
    query = body.get("query", "")
    tags = body.get("tags")
    limit = body.get("limit", 10)

    if not query:
        raise HTTPException(status_code=400, detail="Query is required")

    # Get all tools from cache
    async with cache_lock:
        all_tools = []
        for srv_name, tools in tool_cache.items():
            for t in tools:
                all_tools.append(
                    {
                        "name": t["name"],
                        "description": t.get("description"),
                        "server": srv_name,
                        "inputSchema": t.get("inputSchema"),
                    }
                )

    # Run semantic search
    matches = discovery_service.search(query, all_tools, tags=tags, limit=limit)

    # Format response for ToolGateway client
    return {
        "tools": [
            {
                "tool_id": m.tool_id,
                "name": m.name,
                "description": m.description,
                "score": m.score,
                "estimated_tokens": m.estimated_tokens,
                "server": m.server,
                "matched_tags": m.matched_tags,
            }
            for m in matches
        ],
        "query_id": str(uuid.uuid4()),
        "timestamp": datetime.now().isoformat(),
    }


@app.post("/api/proxy/execute")
async def execute_tool(request: Request):
    """Execute a tool by ID.

    Compatible with tool-gating API for ToolGateway client.

    Request body:
        tool_id: Tool ID in format "server__tool"
        arguments: Tool arguments dict
    """
    body = await request.json()
    tool_id = body.get("tool_id", "")
    arguments = body.get("arguments", {})

    if "__" not in tool_id:
        raise HTTPException(
            status_code=400, detail="tool_id must be in format 'server__tool'"
        )

    server_name, tool_name = tool_id.split("__", 1)

    server = await db.get_server(server_name)
    if not server:
        raise HTTPException(status_code=404, detail=f"Server '{server_name}' not found")

    try:
        result = await execute_tool_on_server(server, tool_name, arguments)
        return {"result": result}
    except Exception as e:
        logger.error(f"Tool execution failed: {tool_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# MCP Protocol Endpoints (full MCP SSE transport for Claude Code etc.)
# =============================================================================


def _get_aggregated_tools() -> list[dict[str, Any]]:
    """Get all tools from cache in MCP format."""
    all_tools = []
    for srv_name, tools in tool_cache.items():
        for t in tools:
            all_tools.append(
                {
                    "name": f"{srv_name}__{t['name']}",
                    "description": t.get("description"),
                    "inputSchema": t.get("inputSchema", {"type": "object"}),
                }
            )
    return all_tools


async def _handle_mcp_request(
    session: MCPSession, request_data: dict[str, Any]
) -> dict[str, Any]:
    """Handle an MCP JSON-RPC request and return response."""
    method = request_data.get("method", "")
    params = request_data.get("params", {})
    request_id = request_data.get("id")

    logger.debug(f"MCP request: method={method}, id={request_id}")

    try:
        if method == "initialize":
            # Return server capabilities
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "mcp-relay",
                    "version": "0.2.0",
                },
            }
        elif method == "notifications/initialized":
            # Client acknowledged initialization - no response needed
            return None
        elif method == "tools/list":
            # Return aggregated tools
            async with cache_lock:
                tools = _get_aggregated_tools()
            result = {"tools": tools}
        elif method == "tools/call":
            # Execute a tool
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})

            if "__" not in tool_name:
                raise ValueError(
                    f"Tool name must be in format 'server__tool': {tool_name}"
                )

            server_name, actual_tool = tool_name.split("__", 1)
            server = await db.get_server(server_name)
            if not server:
                raise ValueError(f"Server '{server_name}' not found")

            tool_result = await execute_tool_on_server(server, actual_tool, arguments)
            result = tool_result
        elif method == "ping":
            result = {}
        else:
            # Unknown method
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}",
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }

    except Exception as e:
        logger.error(f"MCP request error: {method}: {e}")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32603,
                "message": str(e),
            },
        }


@app.get("/mcp/sse")
async def mcp_sse_endpoint(request: Request):
    """MCP SSE endpoint - establishes SSE connection and provides message endpoint.

    This implements the MCP SSE transport protocol:
    1. Client connects via GET to /mcp/sse
    2. Server sends 'endpoint' event with POST URL for messages
    3. Client sends JSON-RPC requests to that endpoint
    4. Server sends responses back via SSE 'message' events
    """
    session_id = uuid4()
    session = MCPSession(session_id)

    async with sessions_lock:
        mcp_sessions[session_id] = session

    logger.info(f"MCP SSE session started: {session_id}")

    async def event_generator():
        try:
            # Send endpoint event first - tells client where to POST messages
            endpoint_url = f"/mcp/message?session_id={session_id.hex}"
            yield {
                "event": "endpoint",
                "data": endpoint_url,
            }
            logger.debug(f"Sent endpoint event: {endpoint_url}")

            # Stream responses back to client
            async for response in session.response_recv:
                if response is None:
                    continue
                yield {
                    "event": "message",
                    "data": json.dumps(response),
                }

        except anyio.ClosedResourceError:
            logger.debug(f"SSE stream closed for session {session_id}")
        except Exception as e:
            logger.error(f"SSE stream error: {e}")
        finally:
            # Cleanup session
            async with sessions_lock:
                mcp_sessions.pop(session_id, None)
            await session.close()
            logger.info(f"MCP SSE session ended: {session_id}")

    return EventSourceResponse(event_generator())


@app.post("/mcp/message")
async def mcp_message_endpoint(request: Request, session_id: str = Query(...)):
    """MCP message endpoint - receives JSON-RPC requests from clients.

    This is paired with /mcp/sse - clients POST messages here and
    receive responses via the SSE stream.
    """
    try:
        sid = UUID(hex=session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session_id")

    async with sessions_lock:
        session = mcp_sessions.get(sid)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.debug(f"MCP message received: session={session_id}, body={body}")

    # Handle the request
    response = await _handle_mcp_request(session, body)

    # Send response via SSE stream
    if response is not None:
        await session.send_response(response)

    # Return 202 Accepted (response comes via SSE)
    return Response(status_code=202)


# =============================================================================
# Legacy MCP Proxy Endpoints (for direct tool calls, kept for compatibility)
# =============================================================================


@app.post("/mcp/tools/call")
async def call_tool(request: Request):
    """Proxy a tool call to the appropriate server (legacy endpoint)."""
    body = await request.json()
    tool_name = body.get("name", "")
    arguments = body.get("arguments", {})

    # Parse server__tool format
    if "__" not in tool_name:
        raise HTTPException(
            status_code=400, detail="Tool name must be in format 'server__tool'"
        )

    server_name, actual_tool = tool_name.split("__", 1)

    server = await db.get_server(server_name)
    if not server:
        raise HTTPException(status_code=404, detail=f"Server '{server_name}' not found")

    # Call the tool on the target server
    result = await execute_tool_on_server(server, actual_tool, arguments)
    return result


# =============================================================================
# Canary Functions (Background Health Monitoring)
# =============================================================================


async def canary_loop():
    """Background task that periodically checks all server health."""
    # Wait a bit for initial startup
    await asyncio.sleep(10)

    while True:
        try:
            await run_canary_check()
        except Exception as e:
            logger.error(f"Canary check cycle failed: {e}")

        await asyncio.sleep(CANARY_INTERVAL)


async def run_canary_check() -> list[ServerHealth]:
    """Run a single canary check cycle on all enabled servers."""
    global last_health_results, previous_status

    servers = await db.list_servers(enabled_only=True)
    logger.info(f"Canary check: probing {len(servers)} servers")
    start = time.time()

    results: list[ServerHealth] = []

    # Probe all servers concurrently
    async def probe_with_timeout(server: dict) -> ServerHealth:
        try:
            return await asyncio.wait_for(
                probe_server(server),
                timeout=CANARY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return ServerHealth(
                name=server["name"],
                status=ServerStatus.UNHEALTHY,
                latency_ms=CANARY_TIMEOUT * 1000,
                error="Timeout",
                checked_at=datetime.now(timezone.utc),
            )

    tasks = [probe_with_timeout(s) for s in servers]
    probe_results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in probe_results:
        if isinstance(result, Exception):
            logger.error(f"Canary probe exception: {result}")
            continue
        results.append(result)
        last_health_results[result.name] = result

        # Check for status change and alert
        prev = previous_status.get(result.name)
        if prev and prev != result.status:
            # Status changed - send alert
            await send_canary_alert(result, prev)

        previous_status[result.name] = result.status

        # Log result
        if result.status == ServerStatus.HEALTHY:
            logger.debug(
                f"[OK] {result.name}: {result.latency_ms:.0f}ms, "
                f"{result.tools_count} tools"
            )
        else:
            logger.warning(
                f"[FAIL] {result.name}: {result.error} ({result.latency_ms:.0f}ms)"
            )

    elapsed = (time.time() - start) * 1000
    healthy = sum(1 for r in results if r.status == ServerStatus.HEALTHY)
    logger.info(
        f"Canary check complete: {healthy}/{len(results)} healthy, {elapsed:.0f}ms"
    )

    return results


async def send_canary_alert(result: ServerHealth, previous: ServerStatus):
    """Send alert webhook when server status changes."""
    if not CANARY_WEBHOOK_URL:
        return

    severity = "error" if result.status == ServerStatus.UNHEALTHY else "info"
    action = "down" if result.status == ServerStatus.UNHEALTHY else "recovered"

    payload = {
        "condition_type": "mcp_canary",
        "severity": severity,
        "message": f"MCP server {result.name} is {action}",
        "context": {
            "server_name": result.name,
            "status": result.status.value,
            "previous_status": previous.value,
            "latency_ms": result.latency_ms,
            "tools_count": result.tools_count,
            "error": result.error,
            "checked_at": result.checked_at.isoformat() if result.checked_at else None,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(CANARY_WEBHOOK_URL, json=payload)
            if resp.status_code >= 400:
                logger.warning(f"Alert webhook returned {resp.status_code}")
            else:
                logger.info(f"Sent alert for {result.name}: {action}")
    except Exception as e:
        logger.error(f"Failed to send canary alert: {e}")


# =============================================================================
# Internal Functions
# =============================================================================


def _db_to_server(row: dict) -> MCPServer:
    """Convert database row to MCPServer model."""
    return MCPServer(
        id=row["id"],
        name=row["name"],
        url=row["url"],
        transport=TransportType(row["transport"]),
        description=row.get("description"),
        enabled=bool(row.get("enabled", 1)),
        status=ServerStatus(row.get("status", "unknown")),
        tools_count=row.get("tools_count", 0),
        last_seen=datetime.fromisoformat(row["last_seen"])
        if row.get("last_seen")
        else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"])
        if row.get("updated_at")
        else None,
    )


async def probe_server(server: dict) -> ServerHealth:
    """Probe a server for health status."""
    name = server["name"]
    url = server["url"]
    transport = server.get("transport", "sse")
    start = time.time()

    try:
        tools = await _fetch_tools_from_server(url, transport, timeout=10)
        latency_ms = (time.time() - start) * 1000

        await db.update_server_status(name, "healthy", len(tools))

        return ServerHealth(
            name=name,
            status=ServerStatus.HEALTHY,
            latency_ms=latency_ms,
            tools_count=len(tools),
            checked_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        latency_ms = (time.time() - start) * 1000
        await db.update_server_status(name, "unhealthy", 0)

        return ServerHealth(
            name=name,
            status=ServerStatus.UNHEALTHY,
            latency_ms=latency_ms,
            error=str(e),
            checked_at=datetime.now(timezone.utc),
        )


async def discover_server_tools(name: str) -> list[dict]:
    """Discover tools from a specific server."""
    server = await db.get_server(name)
    if not server or not server.get("enabled"):
        return []

    try:
        tools = await _fetch_tools_from_server(
            server["url"],
            server.get("transport", "sse"),
            timeout=30,
        )
        async with cache_lock:
            tool_cache[name] = tools

        await db.update_server_status(name, "healthy", len(tools))
        logger.info(f"Discovered {len(tools)} tools from {name}")
        return tools

    except Exception as e:
        logger.error(f"Failed to discover tools from {name}: {e}")
        await db.update_server_status(name, "unhealthy", 0)
        async with cache_lock:
            tool_cache.pop(name, None)
        return []


async def refresh_all_tools(retry_on_empty: bool = False, max_retries: int = 3):
    """Refresh tools from all enabled servers.

    Args:
        retry_on_empty: If True and no tools are found, retry with delay
        max_retries: Maximum number of retry attempts
    """
    for attempt in range(max_retries if retry_on_empty else 1):
        servers = await db.list_servers(enabled_only=True)
        logger.info(
            f"Refreshing tools from {len(servers)} servers (attempt {attempt + 1})"
        )

        tasks = [discover_server_tools(s["name"]) for s in servers]
        await asyncio.gather(*tasks, return_exceptions=True)

        async with cache_lock:
            total = sum(len(tools) for tools in tool_cache.values())
        logger.info(f"Total tools after refresh: {total}")

        if total > 0 or not retry_on_empty:
            return  # Success or not retrying

        # No tools found and retry enabled - wait and try again
        if attempt < max_retries - 1:
            delay = 5 * (attempt + 1)  # 5s, 10s, 15s backoff
            logger.warning(f"No tools discovered, waiting {delay}s before retry...")
            await asyncio.sleep(delay)


async def _fetch_tools_from_server(
    url: str, transport: str, timeout: int = 30
) -> list[dict]:
    """Fetch tools list from an MCP server."""
    if transport == "sse":
        return await _fetch_tools_sse(url, timeout)
    elif transport == "http":
        return await _fetch_tools_http(url, timeout)
    else:
        raise ValueError(f"Unsupported transport: {transport}")


async def _fetch_tools_sse(url: str, timeout: int) -> list[dict]:
    """Fetch tools from SSE server."""
    async with sse_client(url) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
            return [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.inputSchema,
                }
                for t in result.tools
            ]


async def _fetch_tools_http(url: str, timeout: int) -> list[dict]:
    """Fetch tools from streamable-http server."""
    try:
        async with streamablehttp_client(url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                return [
                    {
                        "name": t.name,
                        "description": t.description,
                        "inputSchema": t.inputSchema,
                    }
                    for t in result.tools
                ]
    except Exception as e:
        import traceback

        logger.error(f"streamablehttp_client failed for {url}: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise


def _snake_to_camel(name: str) -> str:
    """Convert snake_case to camelCase."""
    components = name.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


def _normalize_arguments(arguments: dict) -> dict:
    """Normalize argument keys from snake_case to camelCase.

    LLMs trained on Python often emit snake_case parameter names,
    but JavaScript-based MCP servers expect camelCase.
    """
    normalized = {}
    for key, value in arguments.items():
        # Convert snake_case to camelCase
        if "_" in key:
            camel_key = _snake_to_camel(key)
            normalized[camel_key] = value
        else:
            normalized[key] = value
    return normalized


async def execute_tool_on_server(server: dict, tool_name: str, arguments: dict) -> dict:
    """Execute a tool on a specific server."""
    url = server["url"]
    transport = server.get("transport", "sse")

    # Normalize argument keys (snake_case -> camelCase)
    arguments = _normalize_arguments(arguments)

    if transport == "sse":
        async with sse_client(url) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                # exclude_none=True strips 'annotations: null' which violates MCP spec
                return {
                    "content": [c.model_dump(exclude_none=True) for c in result.content]
                }
    elif transport == "http":
        async with streamablehttp_client(url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                # exclude_none=True strips 'annotations: null' which violates MCP spec
                return {
                    "content": [c.model_dump(exclude_none=True) for c in result.content]
                }
    else:
        raise ValueError(f"Unsupported transport: {transport}")


# =============================================================================
# Entry point
# =============================================================================


def main():
    """Run the MCP Relay server."""
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
