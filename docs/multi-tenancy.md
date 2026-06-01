# Multi-tenancy (principals)

The relay runs in one of two postures from the same codebase:

- **Dedicated (1:1)** — one agent, one relay, one toolset behind it. The
  relay has no notion of a caller; everything registered is callable by
  whoever reaches the endpoint. Strongest isolation (process-level
  blast-radius boundary), one relay per tenant. **This is the default and
  is unchanged by anything below.**
- **Shared (multi-tenant)** — many agents reach one relay endpoint. The
  relay authenticates the caller, resolves a **principal**, and gates the
  call to that principal's tool surface and resource scope.

Multi-tenancy is **purely additive**: a relay with no principals configured
behaves exactly as it does today (a 1:1 relay). You never have to choose
the shared mode, and turning it on never weakens the dedicated mode.

## The `principal`

A principal is an identity plus what it's allowed to do:

```
principal := { id, tokens[], servers[]?, scope-rules?, (later) credentials? }
```

| Field | Meaning |
|---|---|
| `id` | stable principal/tenant identifier (appears in logs, audit, metering) |
| `tokens` | bearer tokens that authenticate as this principal |
| `servers` | which registered servers/tools this principal may see+call (a per-principal allowlist; omitted ⇒ all) |
| `scope-rules` | resource constraints on this principal's calls (see [policy](policy.md)) |
| `credentials` | *(future)* per-principal backend credentials the relay brokers at call time |

Everything except `id`/`tokens` is optional; a principal with only an id and
token gets the full surface (useful for a trusted operator principal).

## Identity resolution

In shared mode the caller presents a bearer token. The relay maps
`token → principal`. Resolution is **fail-closed when principals are
configured**: a call with no/unknown token is rejected. When **no** principals
are configured the relay does not look at the token at all (1:1 mode).

```
Authorization: Bearer <token>   ─►  principal lookup  ─►  principal | 401
```

A token is matched by exact O(1) lookup (use long, high-entropy tokens); a
principal may carry several (rotation). The relay never logs token material —
only the resolved `principal.id`.

## What a principal gates

Two layers, evaluated in order on every `tools/call`, **after** identity:

1. **Tool surface.** If the principal declares `servers`, only those
   servers' tools are visible (`tools/list`) and callable; a call to a tool
   outside the set is rejected the same way an out-of-allowlist tool is
   today. This composes with the existing per-registration
   `tool_allowlist`/`tool_blocklist` (the registration bounds what *any*
   caller can reach; the principal narrows it further per tenant).
2. **Resource scope.** The principal is threaded into the policy backend
   (see [policy](policy.md)). Rules may carry a `principals:` selector so one
   shared rule file expresses per-tenant scope:

   ```yaml
   default: pass
   rules:
     - principals: ["tenant-a"]
       tools: ["dns-mcp__*"]
       require: { zone: { eq: "tenant-a.example" } }
     - principals: ["tenant-b"]
       tools: ["dns-mcp__*"]
       require: { zone: { eq: "tenant-b.example" } }
   ```

   A rule with no `principals:` selector applies to every principal
   (backward-compatible with single-tenant rule files).

## Configuration

Principals are loaded from a config file (`PRINCIPALS_FILE`, YAML/JSON),
mirroring the policy backend's pluggable, file-driven shape. Absent ⇒ 1:1
mode.

```yaml
principals:
  - id: tenant-a
    tokens: ["${TENANT_A_TOKEN}"]
    servers: ["dns-mcp", "compute-mcp"]      # optional narrowing
  - id: tenant-b
    tokens: ["${TENANT_B_TOKEN}"]
    servers: ["dns-mcp"]
  - id: operator
    tokens: ["${OPERATOR_TOKEN}"]
    # no servers / no scope rules ⇒ full surface
```

Concrete tenant values (ids, zones, tokens) live in deployment config, never
in the relay code — the same discipline the policy backend follows, so the
feature stays portable.

## Capability ladder

The principal primitive is the foundation for a tenant-aware gateway. In
dependency order:

1. **Pluggable policy** — generic pre-forward gate. *(shipped — see
   [policy](policy.md))*
2. **Principal identity** — caller token → principal. *(this doc)*
3. **Per-principal tool surface** — which tools a principal sees/calls.
4. **Principal-aware policy** — resource scope keyed by principal.
5. **Credential brokering** — the gateway injects per-principal backend
   credentials at call time, so one shared relay mediates real
   per-tenant credential boundaries instead of requiring one MCP instance
   per tenant.
6. **Per-principal audit + metering** — calls tagged with `principal.id`
   (the policy hook already annotates the active span), the substrate for
   per-tenant usage accounting/billing.

Steps 2–4 are the shared-mode core; 5–6 layer on without changing the model.

## Choosing a mode

| | Dedicated (1:1) | Shared (multi-tenant) |
|---|---|---|
| Isolation | process-level; hardest boundary | logical; enforced by identity + scope (+ brokered creds) |
| Cost | one relay per tenant | one relay for many tenants |
| Best for | regulated / sovereign / "guaranteed isolation" tenants | efficient fleets of cooperating tenants |

Both ship from one binary. Operators (or a platform layer) choose per
deployment; a tenant that needs a hard boundary gets its own relay, a fleet
that doesn't shares one.

## Security notes

- **Fail-closed on identity** when principals are configured: unknown token ⇒
  reject, never fall through to the full surface.
- **Defense in depth, not sole boundary.** Per-principal scope rules are the
  *gateway's* check; where the backend supports it, also scope the
  *credential* (a DNS token limited to the tenant's zone, a vault key limited
  to the tenant's collection). Credential brokering (step 5) makes that the
  gateway's job; until then, scope the backend MCP's own credential.
- **No token material in logs/spans** — only `principal.id`.
