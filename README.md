# relay-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Minimal MCP server relay with REST API for dynamic server registration.

## Features

- REST API for registering/unregistering MCP servers
- Relays tools from multiple MCP servers into single endpoint
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
