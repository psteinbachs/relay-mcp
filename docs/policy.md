# Content policy middleware

Optional. When configured, the relay evaluates a content-policy
backend before forwarding tool calls and either passes, rejects, or
swaps redacted fields into the arguments. Off by default — relay
behavior is unchanged unless `POLICY_BACKEND` is set.

## When to use it

Anywhere an agent writes content into a shared system through the
relay (project tracker, chat, ticketing, CRM, code review) and the
operator wants a centralized place to apply data-loss-prevention,
output redaction, or compliance filtering — without forking each
backend MCP.

## Architecture

```
client ──> relay-mcp (tools/call) ──> [policy client] ──> backend MCP
                                           │
                                  config: POLICY_BACKEND
                                           │
                                  ┌────────┴─────────┐
                                  │                  │
                              NoopPolicy      HttpPolicyClient ──>
                              (default)              policy service
                                                     (e.g. content-
                                                      policy-api)
```

The relay never holds rule content. It ships proposed tool-call
fields to the configured backend and acts on the returned decision.

## Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `POLICY_BACKEND` | `noop` | `noop` / `http` / `local` |
| `POLICY_API_URL` | — | HTTP backend base URL (required for `http`) |
| `POLICY_API_TOKEN` | — | Bearer token (required for `http`) |
| `POLICY_RULES_FILE` | — | Path to the rule set (required for `local`) |
| `POLICY_TIMEOUT_SECONDS` | `5.0` | Request timeout (`http` only) |
| `POLICY_FAIL_OPEN` | `false` | If `true`, transient backend errors pass; default fails closed |

## Wire protocol

Backends must implement `POST /v1/evaluate`:

Request:

```json
{
  "tool_name": "plane-mcp__create_work_item",
  "fields": {
    "body": "...",
    "arguments.body": "..."
  },
  "trace_id": "optional-id"
}
```

Response (HTTP 200):

```json
{
  "decision": "pass" | "reject" | "redact",
  "reasons": ["rule:abc", "judge:restricted"],
  "redacted_fields": {"arguments.body": "[redacted]"},
  "audit_id": "uuid",
  "policy_matched": true
}
```

Non-2xx is treated as backend failure; relay applies `POLICY_FAIL_OPEN`.

## Integration

The middleware lives in `mcp_relay.policy`. Wire it into the
tool-call handler at the same point the existing allow/block check
runs, just before forwarding to the backend MCP:

```python
from mcp_relay.policy import PolicyDenied, build_policy_client
from mcp_relay.policy.middleware import enforce_policy

# Build once at startup (e.g. in lifespan):
policy_client = build_policy_client()
```

Then in the call handler (both the SSE `tools/call` branch and the
legacy `/mcp/tools/call` endpoint), after the existing
`_is_tool_allowed` check and before `execute_tool_on_server`:

```python
try:
    arguments = await enforce_policy(
        policy_client,
        tool_name=f"{server_name}__{actual_tool}",
        arguments=arguments,
        trace_id=request.headers.get("x-trace-id"),  # optional
    )
except PolicyDenied as e:
    raise PolicyDenied(str(e))  # uses the existing relay PolicyDenied path
```

The relay already maps `PolicyDenied` to a distinct JSON-RPC error
code (see the existing `JSONRPC_POLICY_DENIED_CODE` handling), so
callers see a clean policy-denial error rather than a generic
500.

## Field flattening

The middleware turns the tool's `arguments` dict into a flat
`{path: str}` map for the wire format. Each top-level key gets sent
under two paths:

- bare key: `body`
- prefixed: `arguments.body`

Per-deployment policies decide which path form to match on. Nested
objects are JSON-serialized so policies can still inspect their
contents; policies that need structured matching should flatten
further on the policy-server side.

## Operational considerations

- **Latency.** Each gated tool call makes one synchronous HTTP
  request to the policy backend. For low-traffic agent workflows
  this is fine; for high-volume integrations, run the policy
  service close to the relay.
- **Fail-closed default.** When the policy backend is unreachable,
  the relay rejects writes by default. This is the safer choice for
  any integration whose whole point is content gating. Set
  `POLICY_FAIL_OPEN=true` only when you've thought through what an
  unfiltered call would mean.
- **Auditability.** The policy backend records every evaluation. To
  trace a single tool call across the relay and the policy log,
  forward a trace id via the `x-trace-id` header (or any header you
  pass through to `trace_id`).

## Local rules backend

`POLICY_BACKEND=local` evaluates calls against a rule file
(`POLICY_RULES_FILE`, YAML or JSON) entirely in-process — no external
service. It suits a single-tenant gateway that wants to constrain
*which resources* a few federated tools may touch (an infra MCP to a
set of resource ids, a DNS MCP to one zone, a vault MCP to one
collection) without running a separate policy server.

The rule schema is deployment-agnostic — all concrete values live in
the file, never in the relay:

```yaml
# Action when NO rule matches the called tool.
#   reject -> fail-closed allowlist (enumerate everything you permit)
#   pass   -> guard mode: constrain a few tools, let the rest through
#             (typical when the relay also serves benign tools)
default: pass

rules:
  - tools: ["infra-mcp__*"]        # fnmatch globs on "<server>__<tool>"
    require:                       # ALL predicates must hold, else reject
      resource_id: { in: [10, 11, 12] }
    reason: "infra-mcp is scoped to the project resources"

  - tools: ["dns-mcp__*"]
    require:
      zone: { eq: "example.com" }
```

Predicates, per field, AND-combined:

| Predicate | Meaning |
|---|---|
| `eq` / `ne` | string equality / inequality (values coerced to string) |
| `in` | membership in a list (values coerced to string) |
| `matches` | `re.search` against the field value |

Field names match the flattened argument paths the middleware
produces (`resource_id` or `arguments.resource_id` — either form
works). A required field that is **absent** from the call fails its
predicate, so a privileged tool invoked without its scoping argument
is rejected rather than allowed through.

Pair this with each registration's `tool_allowlist` for defense in
depth: the allowlist decides *which verbs* a federated server exposes;
the rules decide *which resources* those verbs may act on.

In multi-tenant (shared) mode a rule may also carry a `principals:` list,
so one rule file expresses per-tenant scope (tenant A's zone vs tenant
B's). A rule without `principals:` applies to every caller, so
single-tenant rule files are unchanged. See `docs/multi-tenancy.md`.

Validation is eager — `build_policy_client()` raises
`PolicyClientError` at startup on a missing file, a schema violation,
or an uncompilable `matches` regex, so a bad rule file fails the relay
fast instead of at first call.

## Extending

Subclass `PolicyClient` to implement other strategies (gRPC backend,
a different rules format, etc.). Add a branch to
`build_policy_client()` if your backend should be configurable
from environment variables.
