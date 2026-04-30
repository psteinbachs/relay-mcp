# relay-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Minimal MCP server relay with REST API for dynamic server registration.

## Features

- REST API for registering/unregistering MCP servers
- Relays tools from multiple MCP servers into single endpoint
- Per-server tool allow/block lists (hide and reject specific tools)
- SQLite (default) or PostgreSQL backend
- Health checks for registered servers
- OpenTelemetry instrumentation

## API Endpoints

### Servers
- `GET /api/servers` - List registered servers
- `POST /api/servers` - Register a new server
- `DELETE /api/servers/{name}` - Unregister a server
- `GET /api/servers/{name}/health` - Check server health

### Tools
- `GET /api/tools` - List all aggregated tools
- `GET /api/tools?server={name}` - List tools from specific server

### MCP
- `GET /mcp/sse` - SSE endpoint for MCP clients
- `POST /mcp/message` - Message endpoint for MCP clients

## Configuration

Environment variables:
- `DATABASE_URL` - Database connection (default: `sqlite:///data/mcp_relay.db`)
- `HOST` - Listen host (default: `0.0.0.0`)
- `PORT` - Listen port (default: `8000`)
- `OTEL_EXPORTER_OTLP_ENDPOINT` - OTel collector endpoint
- `OTEL_SERVICE_NAME` - Service name for traces (default: `relay-mcp`)

## Server Registration

```bash
# Register an SSE server
curl -X POST http://localhost:8000/api/servers \
  -H "Content-Type: application/json" \
  -d '{"name": "docker", "url": "http://docker-mcp:8000/sse", "transport": "sse"}'

# Register a streamable-http server  
curl -X POST http://localhost:8000/api/servers \
  -H "Content-Type: application/json" \
  -d '{"name": "netbox", "url": "http://netbox-mcp:8000/mcp", "transport": "http"}'
```

## Tool Allow/Block Lists

Per-registration filters constrain which tools an upstream MCP exposes
through this relay. Use cases: scope-down a permissive upstream that
ships dangerous tools (send-email, drop-database) you don't want
clients to invoke; whitelist a known-safe subset of a chatty backend.

Both fields are optional and independent. Tool names are the
**local** names from the upstream MCP (without the `server__` prefix
the relay adds when aggregating).

```bash
# Blocklist: hide and reject specific tools, expose the rest.
curl -X POST http://localhost:8000/api/servers \
  -H "Content-Type: application/json" \
  -d '{
    "name": "fastmail",
    "url": "http://fastmail-mcp:8000/sse",
    "transport": "sse",
    "tool_blocklist": ["send_email", "reply_email", "send_draft"]
  }'

# Allowlist: only the listed tools are visible.
curl -X POST http://localhost:8000/api/servers \
  -H "Content-Type: application/json" \
  -d '{
    "name": "github",
    "url": "http://github-mcp:8000/sse",
    "transport": "sse",
    "tool_allowlist": ["search_issues", "list_pull_requests", "get_file"]
  }'
```

Filters apply at every aggregation surface (`/api/tools`, MCP
`tools/list`, `/api/tools/discover`) and every call surface
(`/mcp/tools/call`, `/api/proxy/execute`, MCP `tools/call`). Calls to
filtered tools are rejected with HTTP 403 / JSON-RPC error.

To update or clear filters on an existing registration:

```bash
# Replace the blocklist
curl -X PATCH http://localhost:8000/api/servers/fastmail \
  -H "Content-Type: application/json" \
  -d '{"tool_blocklist": ["send_email", "send_draft"]}'

# Clear an existing allowlist (revert to no-allowlist filtering).
# Use the explicit clear_* flag, NOT null on tool_allowlist —
# JSON null and "field omitted" are indistinguishable, and
# silent dropping of a safety filter on a routine update would
# be the wrong default.
curl -X PATCH http://localhost:8000/api/servers/fastmail \
  -H "Content-Type: application/json" \
  -d '{"clear_tool_allowlist": true}'
```
